"""多格式正文提取（自包含）：把上传文件变成「可检索文本 + 标题 + 缩略图 + 转写」。

设计要点：
- 单入口 `extract()` 返回定长 dict，键永远齐全，调用方不用按格式分支；
- 每种能力都允许缺失：外部 CLI（pdftotext / pdftoppm / ffmpeg / ffprobe / tesseract）、
  Python 库（python-docx / openpyxl / python-pptx / Pillow）、语音模型（sherpa-onnx
  SenseVoice）任意一个缺席都只写进 `notes`，不抛异常——上传接口不能因为一个坏文件 500；
- 临时文件统一在 `tempfile.mkdtemp(prefix='kb-extract-')` 下，finally 里整目录删除；
- 语音识别器是进程级单例：240MB 的 int8 ONNX 加载要 1–2 秒，不能每次调用都重建；加载
  失败后记住原因、不再重试，转写降级为空串。
- 正文上限 20000 字符，与 `kb_store._BODY_LIMIT` 保持一致，避免索引里出现超大 body。

支持的 kind：html / text / pdf / docx / xlsx / pptx / image / audio / video / other。
"""

from __future__ import annotations

import html as html_lib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["EXTRACT_VERSION", "detect_mime", "extract"]

EXTRACT_VERSION = 1

MAX_TEXT = 20000  # 正文上限（与 kb_store._BODY_LIMIT 一致）
MAX_BYTES = 200 * 1024 * 1024  # 大输入保护：超过 200MB 直接跳过抽取
MAX_TITLE = 300
MAX_TITLE_LINE = 120  # 纯文本首行只有短到像个标题时才当 title
THUMB_MAX_SIDE = 480
XLSX_MAX_ROWS = 2000
TMP_PREFIX = "kb-extract-"
PREVIEW_PREFIX = "kb-preview-"  # 每次 LibreOffice 预览转换独占的临时目录
CLI_TIMEOUT = 120  # 单个外部命令的默认超时（秒）
PREVIEW_TIMEOUT = 120  # soffice 首次启动要解压/建 profile，给足余量

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

_OCTET = "application/octet-stream"

# 进程级缓存：tesseract 语言列表只问一次；语音识别器只建一次。
_LANG_LOCK = threading.Lock()
_LANG_CACHE: dict[str, set[str]] = {}
_ASR_LOCK = threading.Lock()
# 识别器单例被所有请求共用：解码串行化，避免并发上传时抢 2 个线程/共享内部状态。
_ASR_DECODE_LOCK = threading.Lock()
_ASR: Any = None
_ASR_ERROR = ""
_DEFAULT_ASR_DIR = Path("/root/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx")

_EXT_MIME = {
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".docx": DOCX_MIME,
    ".xlsx": XLSX_MIME,
    ".pptx": PPTX_MIME,
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".avif": "image/avif",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".amr": "audio/amr",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}

# mime → 临时文件后缀（给 ffmpeg / tesseract 认容器用）
_MIME_EXT = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/amr": ".amr",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}

# 纯文本类（除 text/* 之外还有这些 application/*）
_TEXTUAL_MIMES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "text/csv",
}
_MARKDOWN_MIMES = {"text/markdown", "text/x-markdown"}
_HTML_MIMES = {"text/html", "application/xhtml+xml"}
_HTML_SUFFIXES = {".html", ".htm", ".xhtml"}

_IMAGE_MIMES = {m for e, m in _EXT_MIME.items() if m.startswith("image/")}


# ---------------------------------------------------------------- 结果容器


@dataclass
class _Part:
    """各格式处理器的中间结果；`notes` 是列表，最后拼成一句。"""

    text: str = ""
    title: str = ""
    transcript: str = ""
    duration: float = 0.0
    thumbnail: bytes | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class _Run:
    """一次外部命令的结果；`error` 非空即失败原因（缺失/超时/退出码）。"""

    ok: bool
    stdout: bytes = b""
    error: str = ""


# ---------------------------------------------------------------- 小工具


def _short(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_inline(text: Any) -> str:
    """折叠所有空白为单个空格（标题/单元格用）。接受任意值，非字符串先 str()。"""
    raw = "" if text is None else str(text)
    return " ".join(raw.replace("\xa0", " ").split())


def _decode(data: bytes, charset: str | None = None) -> str:
    """先按声明编码，再 utf-8(BOM)，再 gb18030，最后 utf-8 replace 兜底。"""
    candidates: list[str] = []
    if charset:
        candidates.append(charset)
    candidates += ["utf-8-sig", "gb18030"]
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _tidy(text: str) -> str:
    """规范化换行/行内空白：保留段落空行，多余空行压成一个。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    out: list[str] = []
    blank = False
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw).strip()
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _first_line_title(text: str) -> str:
    for raw in (text or "").split("\n"):
        line = _clean_inline(raw)
        if line:
            return line if len(line) <= MAX_TITLE_LINE else ""
    return ""


# ---------------------------------------------------------------- mime 嗅探


def detect_mime(name: str, data: bytes) -> str:
    """按扩展名 → 内容嗅探的顺序判断 mime；都不认识返回 octet-stream。"""
    ext = os.path.splitext((name or "").strip())[1].lower()
    known = _EXT_MIME.get(ext)
    if known:
        return known
    return _sniff(data or b"") or _OCTET


def _sniff(data: bytes) -> str:
    if not data:
        return ""
    head = data[:65536]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:2] == b"BM":
        return "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head[:4] == b"RIFF":
        if head[8:12] == b"WAVE":
            return "audio/wav"
        if head[8:12] == b"WEBP":
            return "image/webp"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:3] == b"ID3":
        return "audio/mpeg"
    if head[:2] in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head[:5] == b"#!AMR":
        return "audio/amr"
    if head[:4] == b"PK\x03\x04":
        # OOXML 本质是 zip：靠包内顶层目录名区分
        if b"word/" in head:
            return DOCX_MIME
        if b"xl/" in head:
            return XLSX_MIME
        if b"ppt/" in head:
            return PPTX_MIME
        return ""
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
            return "image/heic"
        return "video/mp4"
    low = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html"):
        return "text/html"
    return ""


def _kind_of(mime: str, name: str = "") -> str:
    """mime → 处理器 kind（也是对外返回的 kind 枚举值）。"""
    mime = (mime or "").strip().lower()
    ext = os.path.splitext(name or "")[1].lower()
    if mime in _HTML_MIMES or (not mime and ext in _HTML_SUFFIXES):
        return "html"
    if mime == "application/pdf":
        return "pdf"
    if mime == DOCX_MIME:
        return "docx"
    if mime == XLSX_MIME:
        return "xlsx"
    if mime == PPTX_MIME:
        return "pptx"
    if mime in _TEXTUAL_MIMES or mime.startswith("text/"):
        return "text"
    if mime.startswith("image/") or mime in _IMAGE_MIMES:
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "other"


# ---------------------------------------------------------------- 外部命令


def _run(cmd: list[str], timeout: float = CLI_TIMEOUT) -> _Run:
    """跑一个外部命令；缺失/超时/非零退出都折成 `_Run.error`，不抛。"""
    exe = shutil.which(cmd[0])
    if not exe:
        return _Run(False, b"", f"{cmd[0]} 缺失")
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return _Run(False, b"", f"{cmd[0]} 超时（>{int(timeout)}s）")
    except OSError as exc:
        return _Run(False, b"", f"{cmd[0]} 无法执行：{_short(exc, 80)}")
    if proc.returncode != 0:
        detail = _short(proc.stderr.decode("utf-8", "replace"))
        message = f"{cmd[0]} 退出码 {proc.returncode}"
        return _Run(False, proc.stdout, f"{message}：{detail}" if detail else message)
    return _Run(True, proc.stdout, "")


def _suffix(name: str, mime: str) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        return ext
    return _MIME_EXT.get((mime or "").lower(), ".bin")


def _write(directory: Path, filename: str, data: bytes) -> Path:
    path = directory / filename
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------- html / text

# head 也丢掉：<title>/<meta> 属于元信息，不该混进可见正文（标题单独抽）。
_DROP_BLOCKS_RE = re.compile(
    r"<(script|style|noscript|template|head|svg)\b[^>]*>.*?</\1\s*>", re.I | re.S
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_PARA_BREAK_RE = re.compile(
    r"</(?:p|div|h[1-6]|section|article|blockquote|pre|ul|ol|table|tr|dl|figure)\s*>",
    re.I,
)
_LINE_BREAK_RE = re.compile(r"<(?:br|/li|/td|/th|/dt|/dd)\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*([a-zA-Z0-9_\-]+)", re.I
)
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.M)


def _html_part(data: bytes) -> _Part:
    charset_match = _META_CHARSET_RE.search(data[:4096])
    charset = (
        charset_match.group(1).decode("ascii", "ignore") if charset_match else None
    )
    raw = _decode(data, charset)

    title = ""
    title_match = _TITLE_RE.search(raw)
    if title_match:
        title = _clean_inline(
            html_lib.unescape(_TAG_RE.sub("", title_match.group(1)))
        )

    body = _COMMENT_RE.sub(" ", raw)
    body = _DROP_BLOCKS_RE.sub(" ", body)
    body = _PARA_BREAK_RE.sub("\n\n", body)
    body = _LINE_BREAK_RE.sub("\n", body)
    body = _TAG_RE.sub("", body)
    body = html_lib.unescape(body)
    return _Part(text=_tidy(body), title=title)


def _text_part(data: bytes, mime: str) -> _Part:
    text = _decode(data)
    if (mime or "").lower() in _MARKDOWN_MIMES:
        heading = _MD_HEADING_RE.search(text)
        title = _clean_inline(heading.group(1)) if heading else _first_line_title(text)
    else:
        title = _first_line_title(text)
    return _Part(text=text, title=title)


# ---------------------------------------------------------------- pdf


def _pdf_part(data: bytes, workdir: Path) -> _Part:
    part = _Part()
    pdf = _write(workdir, "input.pdf", data)

    text_out = workdir / "text.txt"
    run = _run(["pdftotext", "-layout", str(pdf), str(text_out)])
    if run.ok and text_out.exists():
        part.text = _tidy(_decode(text_out.read_bytes()))
        if not part.text:
            part.notes.append("pdf: 未提取到文本（可能是扫描件）")
    else:
        part.notes.append(f"pdf: {run.error or 'pdftotext 未产出文本'}")

    thumb, note = _pdf_thumbnail(pdf, workdir)
    part.thumbnail = thumb
    if note:
        part.notes.append(note)
    if part.text:
        part.title = _first_line_title(part.text)
    return part


def _pdf_thumbnail(pdf: Path, workdir: Path) -> tuple[bytes | None, str]:
    prefix = workdir / "page"
    run = _run(
        [
            "pdftoppm",
            "-png",
            "-f",
            "1",
            "-l",
            "1",
            "-scale-to",
            str(THUMB_MAX_SIDE),
            str(pdf),
            str(prefix),
        ]
    )
    if not run.ok:
        return None, f"pdf: {run.error or 'pdftoppm 未生成缩略图'}"
    pages = sorted(workdir.glob("page*.png"))
    if not pages:
        return None, "pdf: 首屏渲染未产出图片"
    try:
        return pages[0].read_bytes(), ""
    except OSError as exc:
        return None, f"pdf: 缩略图读取失败（{_short(exc, 60)}）"


# ---------------------------------------------------------------- office

_SOFFICE_NAMES = ("soffice", "libreoffice")


def _soffice() -> str:
    """返回可用的 LibreOffice 可执行文件路径；没装返回空串（测试 monkeypatch 这里）。"""
    for name in _SOFFICE_NAMES:
        exe = shutil.which(name)
        if exe:
            return exe
    return ""


def _kill_office(proc: subprocess.Popen[bytes]) -> None:
    """超时后杀掉整个进程组。

    `soffice` 脚本 exec 的是 `oosplash`，真正干活的是它 fork 出来的 `soffice.bin`；
    只杀直接子进程会留下孤儿继续往 profile 里写文件，临时目录就删不干净。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - 已 SIGKILL，理论上不会等到
        pass


def _office_preview(data: bytes, suffix: str) -> tuple[bytes | None, str]:
    """Office 文件首屏预览：soffice 转 PDF → pdftoppm 渲染第 1 页。

    返回 `(PNG bytes, note)`；成功时 note 是 `preview: libreoffice`，失败时是
    `preview: <原因>`。任何异常都在内部折成 note，绝不外抛。
    """
    exe = _soffice()
    if not exe:
        return None, "preview: soffice 不可用"
    workdir = Path(tempfile.mkdtemp(prefix=PREVIEW_PREFIX))
    try:
        source = _write(workdir, f"input{suffix}", data)
        # soffice 并发转换会抢同一个用户配置目录，给每次转换一个独占的 UserInstallation
        profile = workdir / "profile"
        profile.mkdir()
        # 不用 _run()：它的 timeout 只杀直接子进程，会留下 soffice.bin 孤儿；
        # 这里让 soffice 独占一个会话，超时时按进程组清理。
        try:
            proc = subprocess.Popen(
                [
                    exe,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--norestore",
                    "--nolockcheck",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(workdir),
                    str(source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return None, f"preview: soffice 无法执行（{_short(exc, 80)}）"
        try:
            _, stderr = proc.communicate(timeout=PREVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_office(proc)
            return None, f"preview: soffice 超时（>{int(PREVIEW_TIMEOUT)}s）"
        if proc.returncode != 0:
            detail = _short((stderr or b"").decode("utf-8", "replace"))
            message = f"preview: soffice 退出码 {proc.returncode}"
            return None, f"{message}：{detail}" if detail else message
        pdf = workdir / "input.pdf"
        if not pdf.exists():
            found = sorted(workdir.glob("*.pdf"))
            if not found:
                return None, "preview: soffice 未产出 PDF"
            pdf = found[0]
        thumb, note = _pdf_thumbnail(pdf, workdir)
        if thumb is None:
            return None, f"preview: {note.removeprefix('pdf: ') or '首屏渲染失败'}"
        return thumb, "preview: libreoffice"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _docx_part(data: bytes) -> _Part:
    try:
        from docx import Document
    except Exception as exc:  # pragma: no cover - 依赖缺失
        return _Part(notes=[f"docx: python-docx 不可用（{type(exc).__name__}）"])
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:
        return _Part(notes=[f"docx: 解析失败（{_short(exc, 100)}）"])

    blocks = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [_clean_inline(cell.text) for cell in row.cells]
            line = " | ".join(cell for cell in cells if cell)
            if line:
                blocks.append(line)

    try:
        doc_title = _clean_inline(document.core_properties.title or "")
    except Exception:
        doc_title = ""
    title = doc_title or (blocks[0][:MAX_TITLE_LINE] if blocks else "")
    part = _Part(text="\n".join(blocks), title=title)
    part.thumbnail, note = _office_preview(data, ".docx")
    if note:
        part.notes.append(note)
    return part


def _xlsx_part(data: bytes) -> _Part:
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # pragma: no cover - 依赖缺失
        return _Part(notes=[f"xlsx: openpyxl 不可用（{type(exc).__name__}）"])
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        return _Part(notes=[f"xlsx: 解析失败（{_short(exc, 100)}）"])

    lines: list[str] = []
    truncated = False
    try:
        try:
            sheet_title = _clean_inline(
                (workbook.properties.title if workbook.properties else "") or ""
            )
        except Exception:
            sheet_title = ""
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows(values_only=True):
                if len(lines) >= XLSX_MAX_ROWS:
                    truncated = True
                    break
                cells = ["" if value is None else _clean_inline(value) for value in row]
                if not any(cells):
                    continue
                line = " | ".join(cells).strip(" |")
                if line:
                    lines.append(line)
            if truncated:
                break
    finally:
        try:
            workbook.close()
        except Exception:
            pass

    notes = ["xlsx: 超过 2000 行，已截断"] if truncated else []
    if not lines:
        notes.append("xlsx: 未提取到有效单元格")
    part = _Part(text="\n".join(lines), title=sheet_title, notes=notes)
    part.thumbnail, note = _office_preview(data, ".xlsx")
    if note:
        part.notes.append(note)
    return part


def _pptx_part(data: bytes) -> _Part:
    try:
        from pptx import Presentation
    except Exception as exc:  # pragma: no cover - 依赖缺失
        return _Part(notes=[f"pptx: python-pptx 不可用（{type(exc).__name__}）"])
    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as exc:
        return _Part(notes=[f"pptx: 解析失败（{_short(exc, 100)}）"])

    pages: list[str] = []
    for slide in deck.slides:
        chunks: list[str] = []
        for shape in slide.shapes:
            try:
                if shape.has_text_frame:
                    value = shape.text_frame.text.strip()
                    if value:
                        chunks.append(value)
            except Exception:
                continue
            try:
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [_clean_inline(cell.text) for cell in row.cells]
                        line = " | ".join(cell for cell in cells if cell)
                        if line:
                            chunks.append(line)
            except Exception:
                continue
        if chunks:
            pages.append("\n".join(chunks))

    text = "\n\n".join(pages)
    try:
        deck_title = _clean_inline(deck.core_properties.title or "")
    except Exception:
        deck_title = ""
    title = deck_title or _first_line_title(text)
    notes = [] if pages else ["pptx: 未提取到页面文本"]
    part = _Part(text=text, title=title, notes=notes)
    part.thumbnail, note = _office_preview(data, ".pptx")
    if note:
        part.notes.append(note)
    return part


# ---------------------------------------------------------------- image


def _pil() -> Any:
    try:
        from PIL import Image, ImageOps

        return Image, ImageOps
    except Exception:  # pragma: no cover - 依赖缺失
        return None


def _png_thumbnail(image: Any, max_side: int = THUMB_MAX_SIDE) -> bytes | None:
    """把已打开的图像缩成最长边 max_side 的 PNG bytes。"""
    pil = _pil()
    if pil is None:
        return None
    _, image_ops = pil
    shot = image_ops.exif_transpose(image)
    try:
        if shot.mode not in ("RGB", "RGBA", "L"):
            shot = shot.convert("RGBA" if "A" in shot.mode else "RGB")
        shot.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        shot.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


def _tesseract_langs() -> set[str]:
    """`tesseract --list-langs`（进程内缓存；失败缓存空集合）。"""
    with _LANG_LOCK:
        if "langs" in _LANG_CACHE:
            return _LANG_CACHE["langs"]
        langs: set[str] = set()
        run = _run(["tesseract", "--list-langs"], timeout=30)
        if run.ok:
            for line in run.stdout.decode("utf-8", "replace").splitlines():
                token = line.strip()
                if token and " " not in token and ":" not in token:
                    langs.add(token)
        _LANG_CACHE["langs"] = langs
        return langs


def _ocr_enabled() -> bool:
    value = (os.environ.get("KB_OCR") or "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _ocr_image(data: bytes, mime: str, name: str, workdir: Path) -> tuple[str, str]:
    """tesseract chi_sim OCR；返回 (文本, 备注)。只在 KB_OCR 打开时调用。"""
    langs = _tesseract_langs()
    if "chi_sim" not in langs:
        return "", "image: OCR 不可用（tesseract 缺少 chi_sim）"
    source = _write(workdir, "ocr" + _suffix(name, mime), data)
    run = _run(["tesseract", str(source), "stdout", "-l", "chi_sim"], timeout=180)
    if not run.ok:
        return "", f"image: {run.error or 'OCR 失败'}"
    return _tidy(_decode(run.stdout)), ""


def _image_part(data: bytes, mime: str, name: str, workdir: Path) -> _Part:
    pil = _pil()
    if pil is None:
        return _Part(notes=["image: Pillow 不可用"])
    image_module, _ = pil
    try:
        with image_module.open(io.BytesIO(data)) as image:
            image.load()  # 立刻解码，让截断文件/解压炸弹在这里暴露
            thumbnail = _png_thumbnail(image)
    except Exception as exc:
        return _Part(notes=[f"image: 无法解码（{type(exc).__name__}: {_short(exc, 80)}）"])

    part = _Part(thumbnail=thumbnail)
    if thumbnail is None:
        part.notes.append("image: 缩略图生成失败")
    if not _ocr_enabled():
        part.notes.append("image: 未启用 OCR（KB_OCR=1 时用 tesseract chi_sim）")
        return part
    text, note = _ocr_image(data, mime, name, workdir)
    part.text = text
    if note:
        part.notes.append(note)
    if not text and not note:
        part.notes.append("image: OCR 未识别到文字")
    return part


# ---------------------------------------------------------------- 音视频 / ASR


def _asr_model_dir() -> Path:
    override = (os.environ.get("KB_ASR_MODEL_DIR") or "").strip()
    return Path(override) if override else _DEFAULT_ASR_DIR


def _recognizer() -> Any:
    """SenseVoice 识别器（模块级单例；失败不重试，返回 None）。"""
    global _ASR, _ASR_ERROR
    if _ASR is not None or _ASR_ERROR:
        return _ASR
    with _ASR_LOCK:
        if _ASR is not None or _ASR_ERROR:
            return _ASR
        try:
            import sherpa_onnx
        except Exception as exc:
            _ASR_ERROR = f"sherpa_onnx 不可用（{type(exc).__name__}）"
            return None
        model_dir = _asr_model_dir()
        model = model_dir / "model.int8.onnx"
        tokens = model_dir / "tokens.txt"
        if not model.exists() or not tokens.exists():
            _ASR_ERROR = "SenseVoice 模型未安装"
            return None
        try:
            _ASR = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model),
                tokens=str(tokens),
                num_threads=2,
                use_itn=True,
                language="auto",
                debug=False,
            )
        except Exception as exc:
            _ASR_ERROR = f"SenseVoice 加载失败（{_short(exc, 100)}）"
            return None
    return _ASR


def _transcribe_wav(wav_path: Path) -> tuple[str, str]:
    """16k 单声道 16-bit wav → 文本；返回 (文本, 备注)。"""
    recognizer = _recognizer()
    if recognizer is None:
        return "", f"asr: {_ASR_ERROR or '识别器不可用'}"
    try:
        import wave

        with wave.open(str(wav_path), "rb") as handle:
            if handle.getsampwidth() != 2:
                return "", "asr: 音频不是 16-bit PCM，跳过转写"
            sample_rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        samples = array("h", frames)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return "", "asr: 空音频，跳过转写"
        with _ASR_DECODE_LOCK:
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, [s / 32768.0 for s in samples])
            recognizer.decode_stream(stream)
            return (stream.result.text or "").strip(), ""
    except Exception as exc:
        return "", f"asr: 转写失败（{type(exc).__name__}: {_short(exc, 80)}）"


def _probe_duration(source: Path) -> tuple[float, str]:
    run = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(source),
        ],
        timeout=60,
    )
    if not run.ok:
        return 0.0, f"media: {run.error}"
    try:
        return max(0.0, float(run.stdout.decode("utf-8", "replace").strip())), ""
    except ValueError:
        return 0.0, "media: ffprobe 时长不可解析"


def _video_frame(
    source: Path, duration: float, workdir: Path
) -> tuple[bytes | None, str]:
    """在 min(3.0, duration*0.1) 秒处抽一帧 PNG，再统一缩到 480。"""
    stamp = min(3.0, duration * 0.1) if duration > 0 else 0.0
    frame = workdir / "frame.png"
    run = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{stamp:.3f}",
            "-frames:v",
            "1",
            str(frame),
        ]
    )
    if not run.ok or not frame.exists():
        return None, f"video: {run.error or '抽帧未产出图片'}"
    try:
        raw = frame.read_bytes()
    except OSError as exc:
        return None, f"video: 缩略图读取失败（{_short(exc, 60)}）"
    pil = _pil()
    if pil is None:
        return raw, ""  # 没有 Pillow 就交原始帧（已经是 PNG）
    image_module, _ = pil
    try:
        with image_module.open(io.BytesIO(raw)) as image:
            image.load()
            thumbnail = _png_thumbnail(image)
    except Exception as exc:
        return None, f"video: 缩略图处理失败（{type(exc).__name__}）"
    return (thumbnail, "") if thumbnail else (None, "video: 缩略图处理失败")


def _has_audio(source: Path) -> bool:
    """ffprobe 看有没有音频流（无音轨的视频不该报 ffmpeg 错误）。"""
    run = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(source),
        ],
        timeout=30,
    )
    return bool(run.ok and b"audio" in (run.stdout or b""))


def _media_part(
    data: bytes,
    mime: str,
    name: str,
    workdir: Path,
    *,
    transcribe: bool,
    max_media_seconds: int,
    is_video: bool,
) -> _Part:
    part = _Part()
    source = _write(workdir, "input" + _suffix(name, mime), data)

    duration, note = _probe_duration(source)
    part.duration = duration
    if note:
        part.notes.append(note)

    limit = max(0, int(max_media_seconds or 0))
    if transcribe and limit <= 0:
        part.notes.append("media: max_media_seconds=0，跳过转写")
    elif transcribe and not _has_audio(source):
        part.notes.append("media: 没有音轨，跳过转写")
    elif transcribe:
        wav = workdir / "audio.wav"
        run = _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-t",
                str(limit),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-acodec",
                "pcm_s16le",
                str(wav),
            ],
            timeout=max(60.0, min(600.0, float(limit) + 120.0)),
        )
        if run.ok and wav.exists():
            part.transcript, asr_note = _transcribe_wav(wav)
            if asr_note:
                part.notes.append(asr_note)
            if duration > limit > 0:
                part.notes.append(f"media: 仅转写前 {limit} 秒")
        else:
            part.notes.append(f"media: {run.error or 'ffmpeg 未产出音频'}")

    if is_video:
        frame, frame_note = _video_frame(source, duration, workdir)
        part.thumbnail = frame
        if frame_note:
            part.notes.append(frame_note)
    return part


# ---------------------------------------------------------------- 主入口


def _empty_result(kind: str, notes: list[str] | None = None) -> dict:
    return {
        "text": "",
        "title": "",
        "kind": kind,
        "transcript": "",
        "duration": 0.0,
        "thumbnail": None,
        "thumbnail_mime": "",
        "notes": "; ".join(notes or []),
    }


def extract(
    data: bytes,
    mime: str,
    name: str,
    *,
    transcribe: bool = True,
    max_media_seconds: int = 1200,
) -> dict:
    """抽取正文/标题/缩略图/转写；任何失败都降级为 notes，不抛异常。

    返回 dict 的键恒定：text / title / kind / transcript / duration /
    thumbnail / thumbnail_mime / notes。
    """
    payload = data or b""
    resolved = (mime or "").strip().lower()
    if not resolved or resolved == _OCTET:
        resolved = detect_mime(name or "", payload)
    kind = _kind_of(resolved, name or "")

    if not payload:
        return _empty_result(kind, ["空文件，跳过抽取"])
    if len(payload) > MAX_BYTES:
        return _empty_result(kind, ["文件过大，跳过抽取"])

    workdir: Path | None = None
    part: _Part
    try:
        if kind == "html":
            part = _html_part(payload)
        elif kind == "text":
            part = _text_part(payload, resolved)
        elif kind in ("pdf", "image", "audio", "video"):
            workdir = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
            if kind == "pdf":
                part = _pdf_part(payload, workdir)
            elif kind == "image":
                part = _image_part(payload, resolved, name or "", workdir)
            else:
                part = _media_part(
                    payload,
                    resolved,
                    name or "",
                    workdir,
                    transcribe=transcribe,
                    max_media_seconds=max_media_seconds,
                    is_video=kind == "video",
                )
        elif kind == "docx":
            part = _docx_part(payload)
        elif kind == "xlsx":
            part = _xlsx_part(payload)
        elif kind == "pptx":
            part = _pptx_part(payload)
        else:
            part = _Part(notes=["未支持的类型"])
    except Exception as exc:  # 兜底：处理器内部任何意外都不外泄
        part = _Part(notes=[f"{kind}: 抽取异常（{type(exc).__name__}: {_short(exc, 100)}）"])
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    thumbnail = part.thumbnail if isinstance(part.thumbnail, bytes) else None
    return {
        "text": _tidy(part.text)[:MAX_TEXT],
        "title": _clean_inline(part.title)[:MAX_TITLE],
        "kind": kind,
        "transcript": (part.transcript or "").strip(),
        "duration": round(float(part.duration or 0.0), 3),
        "thumbnail": thumbnail,
        "thumbnail_mime": "image/png" if thumbnail else "",
        "notes": "; ".join(part.notes),
    }
