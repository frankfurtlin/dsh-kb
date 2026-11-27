"""多格式正文提取的单元测试（不联网、不依赖仓库外素材，真实语音用例除外）。

覆盖范围：
- `detect_mime`：扩展名优先、内容嗅探（PDF/PNG/OOXML zip/MP4/HTML）、未知兜底；
- html（标题 + 可见正文，脚本/样式/noscript/head 不混入）、md/txt（含 20000 截断）；
- docx / xlsx / pptx：用 python-docx、openpyxl、python-pptx 现场造文件再抽；
- office 首屏预览：LibreOffice 转 PDF 后取第一页 PNG（没装 soffice 就整组 skip），
  以及 soffice 缺失时降级为「thumbnail=None + notes 说明」；
- png：缩略图最长边 480，坏图不抛只降级；
- 音视频：ffmpeg 现场造 1 秒静音 mp4 / mp3，`transcribe=False` 只取时长与缩略图；
- 未知类型 / 空文件 / 超过 200MB 保护的兜底；
- 真实转写：`/root/workspace/experiment/videos/tts/s2.mp3`（「因为每一次交接，都会丢掉
  上下文。」），文件不存在就整条 skip。

每个用例都过 `_assert_shape()`：返回 dict 的键必须齐全、类型必须正确。上传接口依赖这个
契约，只断言某个字段容易出现「加字段不改调用方」的静默回归。

OCR 不做断言：它在 `KB_OCR=1` 且 tesseract 有 chi_sim 时才跑，识别质量依赖字体渲染，
在本机 CI 上不稳定；这里只断言「默认不 OCR、正文留空」这一确定行为。
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import kb_extract

DOCX_MIME = kb_extract.DOCX_MIME
XLSX_MIME = kb_extract.XLSX_MIME
PPTX_MIME = kb_extract.PPTX_MIME

KINDS = {"html", "text", "pdf", "docx", "xlsx", "pptx", "image", "audio", "video", "other"}
KEYS = {
    "text",
    "title",
    "kind",
    "transcript",
    "duration",
    "thumbnail",
    "thumbnail_mime",
    "notes",
}
STRING_KEYS = ("text", "title", "kind", "transcript", "thumbnail_mime", "notes")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
REAL_AUDIO = Path("/root/workspace/experiment/videos/tts/s2.mp3")

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="需要 ffmpeg / ffprobe 生成音视频素材"
)

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
needs_soffice = pytest.mark.skipif(
    not SOFFICE, reason="需要 LibreOffice（soffice）渲染 Office 首屏预览"
)


# ---------------------------------------------------------------- 契约断言


def _assert_shape(result: dict) -> dict:
    """返回 dict 的键齐全 + 类型正确（所有取值断言都建立在这个契约上）。"""
    assert isinstance(result, dict), f"extract 必须返回 dict，实际 {type(result)!r}"
    assert set(result) == KEYS, f"键不一致：缺 {KEYS - set(result)}，多 {set(result) - KEYS}"
    for key in STRING_KEYS:
        assert isinstance(result[key], str), f"{key} 应为 str，实际 {type(result[key])!r}"
    assert isinstance(result["duration"], float), (
        f"duration 应为 float，实际 {type(result['duration'])!r}"
    )
    assert result["duration"] >= 0.0
    assert result["kind"] in KINDS, f"kind 越界：{result['kind']!r}"
    assert result["thumbnail"] is None or isinstance(result["thumbnail"], bytes)
    if result["thumbnail"] is None:
        assert result["thumbnail_mime"] == ""
    else:
        assert result["thumbnail_mime"] == "image/png"
        assert result["thumbnail"].startswith(PNG_MAGIC), "缩略图必须是 PNG"
    return result


def _png_size(blob: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as image:
        return image.size


def _assert_preview(result: dict) -> None:
    """Office 预览的环境无关断言：有缩略图就必须合规，没有就必须写明原因。"""
    if result["thumbnail"] is None:
        assert "preview:" in result["notes"], "缺缩略图却没写原因"
        return
    assert max(_png_size(result["thumbnail"])) <= 480
    assert "preview: libreoffice" in result["notes"]


# ---------------------------------------------------------------- 素材夹具


def _ffmpeg(*args: str) -> None:
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-y", *args], capture_output=True, timeout=120
    )
    if proc.returncode != 0:
        pytest.skip(f"ffmpeg 造素材失败：{proc.stderr.decode('utf-8', 'replace')[:200]}")


@pytest.fixture(scope="module")
def media_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """1 秒静音 mp4（含视频轨 + 音频轨）与 1 秒静音 mp3。"""
    out = tmp_path_factory.mktemp("kb-extract-media")
    video = out / "silent.mp4"
    audio = out / "silent.mp3"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=10:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=16000:cl=mono",
        "-t",
        "1",
        "-c:v",
        "mpeg4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(video),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=16000:cl=mono",
        "-t",
        "1",
        "-c:a",
        "libmp3lame",
        str(audio),
    )
    return {"video": video, "audio": audio}


# ---------------------------------------------------------------- detect_mime


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.html", "text/html"),
        ("REPORT.HTM", "text/html"),
        ("笔记.md", "text/markdown"),
        ("notes.txt", "text/plain"),
        ("a.pdf", "application/pdf"),
        ("a.docx", DOCX_MIME),
        ("a.xlsx", XLSX_MIME),
        ("a.pptx", PPTX_MIME),
        ("photo.PNG", "image/png"),
        ("voice.mp3", "audio/mpeg"),
        ("clip.mp4", "video/mp4"),
        ("clip.wav", "audio/wav"),
    ],
)
def test_detect_mime_by_extension(name: str, expected: str) -> None:
    """扩展名（大小写不敏感）优先，不读内容。"""
    assert kb_extract.detect_mime(name, b"") == expected


def test_detect_mime_sniffs_known_magic() -> None:
    """没有扩展名时按魔数嗅探。"""
    assert kb_extract.detect_mime("", b"%PDF-1.7\n1 0 obj") == "application/pdf"
    assert kb_extract.detect_mime("blob", PNG_MAGIC + b"rest") == "image/png"
    assert kb_extract.detect_mime("blob", b"\xff\xd8\xff\xe0JFIF") == "image/jpeg"
    assert kb_extract.detect_mime("blob", b"GIF89a....") == "image/gif"
    assert kb_extract.detect_mime("blob", b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"
    assert kb_extract.detect_mime("blob", b"ID3\x04\x00") == "audio/mpeg"
    # OOXML 是 zip，靠包内顶层目录名区分三种 office 格式
    assert kb_extract.detect_mime("", b"PK\x03\x04....word/document.xml") == DOCX_MIME
    assert kb_extract.detect_mime("", b"PK\x03\x04....xl/workbook.xml") == XLSX_MIME
    assert kb_extract.detect_mime("", b"PK\x03\x04....ppt/presentation.xml") == PPTX_MIME
    # MP4 家族：ftyp 在 offset 4
    assert kb_extract.detect_mime("", b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00") == "video/mp4"
    assert kb_extract.detect_mime("", b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00") == "audio/mp4"
    assert kb_extract.detect_mime("", b"<!DOCTYPE html><html><body>x</body>") == "text/html"


def test_detect_mime_unknown_falls_back_to_octet_stream() -> None:
    """不认识的内容/扩展名一律 octet-stream，绝不猜成文本。"""
    assert kb_extract.detect_mime("", b"\x00\x01\x02\x03\xff\xfe") == "application/octet-stream"
    assert kb_extract.detect_mime("kb.sqlite", b"SQLite format 3\x00") == "application/octet-stream"
    assert kb_extract.detect_mime("", b"hello world") == "application/octet-stream"
    assert kb_extract.detect_mime("", b"") == "application/octet-stream"


# ---------------------------------------------------------------- html / text


def test_extract_html_title_and_visible_text() -> None:
    """<title> 单独作为 title；正文只留可见文本，脚本/样式/noscript/head 不混入。"""
    page = """<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><title>A股市场全景 &amp; 策略</title>
<style>.chart{color:#c00}</style>
<script>var s = "<p>脚本里的假段落</p>";</script>
</head><body>
<h1>市场全景</h1>
<p>上证指数上涨 0.86%，成交额 1.42 万亿元。</p>
<p>半导体、通信设备领涨。</p>
<noscript>请开启 JavaScript</noscript>
</body></html>""".encode("utf-8")

    result = _assert_shape(kb_extract.extract(page, "text/html", "market.html"))

    assert result["kind"] == "html"
    assert result["title"] == "A股市场全景 & 策略"
    assert "上证指数上涨 0.86%" in result["text"]
    assert "半导体、通信设备领涨" in result["text"]
    assert "脚本里的假段落" not in result["text"]
    assert "请开启 JavaScript" not in result["text"]
    assert "color:#c00" not in result["text"]
    # 标题属于元信息，不该重复出现在正文里
    assert "A股市场全景" not in result["text"]
    assert result["transcript"] == ""
    assert result["thumbnail"] is None
    assert result["notes"] == ""


def test_extract_markdown_uses_first_heading_as_title() -> None:
    markdown = (
        "# 2026 年 1 月月度复盘\n\n"
        "## 收益\n\n本月组合收益 **+4.2%**，跑赢沪深 300 两个点。\n"
    ).encode("utf-8")

    result = _assert_shape(kb_extract.extract(markdown, "text/markdown", "复盘.md"))

    assert result["kind"] == "text"
    assert result["title"] == "2026 年 1 月月度复盘"
    assert "本月组合收益 **+4.2%**" in result["text"]


def test_extract_plain_text_decodes_and_truncates() -> None:
    """txt：首行当标题；正文截断到 20000 字符；非法 UTF-8 用 replace 兜底不抛。"""
    body = "\n".join(f"第 {i} 行：正文内容" for i in range(4000))
    raw = ("收盘速记\n" + body).encode("utf-8")
    assert len(raw.decode("utf-8")) > kb_extract.MAX_TEXT

    result = _assert_shape(kb_extract.extract(raw, "text/plain", "note.txt"))

    assert result["kind"] == "text"
    assert result["title"] == "收盘速记"
    assert len(result["text"]) == kb_extract.MAX_TEXT

    broken = _assert_shape(
        kb_extract.extract(b"GBK \xd6\xd0\xce\xc4 tail", "text/plain", "gbk.txt")
    )
    assert broken["kind"] == "text"
    assert broken["text"], "乱码也不能变成空正文"


# ---------------------------------------------------------------- office


def test_extract_docx_paragraphs_and_tables() -> None:
    from docx import Document

    document = Document()
    document.add_paragraph("2026 年三季度投资分析报告")
    document.add_paragraph("资产负债率 58.3%，经营现金流创五年新高。")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "指标"
    table.rows[0].cells[1].text = "数值"
    table.rows[1].cells[0].text = "资产负债率"
    table.rows[1].cells[1].text = "58.3%"
    buffer = io.BytesIO()
    document.save(buffer)

    result = _assert_shape(
        kb_extract.extract(buffer.getvalue(), DOCX_MIME, "投资分析.docx")
    )

    assert result["kind"] == "docx"
    assert result["title"] == "2026 年三季度投资分析报告"
    assert "经营现金流创五年新高" in result["text"]
    # 表格按行拼成「单元格 | 单元格」
    assert "资产负债率 | 58.3%" in result["text"]
    _assert_preview(result)


def test_extract_xlsx_rows_joined_across_sheets() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "行情"
    sheet.append(["日期", "收盘", "备注"])
    sheet.append(["2026-01-01", 3612.44, "上涨"])
    second = workbook.create_sheet("判断")
    second.append(["核心结论", "成立"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = _assert_shape(
        kb_extract.extract(buffer.getvalue(), XLSX_MIME, "行情.xlsx")
    )

    assert result["kind"] == "xlsx"
    assert "日期 | 收盘 | 备注" in result["text"]
    # 数字单元格也要能转成字符串（曾因 float 调 .replace 崩过）
    assert "2026-01-01 | 3612.44 | 上涨" in result["text"]
    # 多个 sheet 依次拼接
    assert "核心结论 | 成立" in result["text"]
    _assert_preview(result)


def test_extract_xlsx_row_cap() -> None:
    """最多 2000 行，超出部分丢弃并记 notes（行内容故意短，避免先撞 20000 字符上限）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    for index in range(kb_extract.XLSX_MAX_ROWS + 50):
        sheet.append([f"r{index}"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = _assert_shape(
        kb_extract.extract(buffer.getvalue(), XLSX_MIME, "long.xlsx")
    )

    assert len(result["text"].splitlines()) == kb_extract.XLSX_MAX_ROWS
    assert result["text"].splitlines()[-1] == f"r{kb_extract.XLSX_MAX_ROWS - 1}"
    assert "截断" in result["notes"]


def test_extract_pptx_text_frames_per_slide() -> None:
    from pptx import Presentation

    deck = Presentation()
    first = deck.slides.add_slide(deck.slide_layouts[0])
    first.shapes.title.text = "多格式正文提取"
    first.placeholders[1].text = "第一页的副标题正文"
    second = deck.slides.add_slide(deck.slide_layouts[1])
    second.shapes.title.text = "第二页标题"
    second.placeholders[1].text = "第二页正文要点"
    buffer = io.BytesIO()
    deck.save(buffer)

    result = _assert_shape(
        kb_extract.extract(buffer.getvalue(), PPTX_MIME, "简报.pptx")
    )

    assert result["kind"] == "pptx"
    assert result["title"] == "多格式正文提取"
    assert "第一页的副标题正文" in result["text"]
    assert "第二页正文要点" in result["text"]
    _assert_preview(result)


# ---------------------------------------------------------------- office 预览


def _save_office(document: object) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)  # type: ignore[attr-defined]
    return buffer.getvalue()


def _minimal_docx() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("季度报告封面")
    return _save_office(document)


def _minimal_pptx() -> bytes:
    from pptx import Presentation

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = "预览封面"
    return _save_office(deck)


def _minimal_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["指标", "数值"])
    return _save_office(workbook)


OFFICE_SAMPLES = pytest.mark.parametrize(
    ("filename", "mime", "builder"),
    [
        ("preview.docx", DOCX_MIME, _minimal_docx),
        ("preview.pptx", PPTX_MIME, _minimal_pptx),
        ("preview.xlsx", XLSX_MIME, _minimal_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)


@OFFICE_SAMPLES
@needs_soffice
def test_extract_office_preview_thumbnail(filename: str, mime: str, builder) -> None:
    """docx/pptx/xlsx：soffice 转 PDF 后取第一页，长边不超过 480 的 PNG。"""
    result = _assert_shape(kb_extract.extract(builder(), mime, filename))

    assert isinstance(result["thumbnail"], bytes), (
        f"未生成缩略图：notes={result['notes']!r}"
    )
    assert max(_png_size(result["thumbnail"])) <= 480
    assert "preview: libreoffice" in result["notes"]


@OFFICE_SAMPLES
def test_extract_office_preview_degrades_without_soffice(
    filename: str,
    mime: str,
    builder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """soffice 不可用：不抛异常、thumbnail 为 None、notes 写原因，正文照抽。"""
    monkeypatch.setattr(kb_extract, "_soffice", lambda: "")

    result = _assert_shape(kb_extract.extract(builder(), mime, filename))

    assert result["kind"] in {"docx", "pptx", "xlsx"}
    assert result["thumbnail"] is None
    assert "preview: soffice 不可用" in result["notes"]
    assert result["text"], "预览失败不该影响正文抽取"


# ---------------------------------------------------------------- image


def test_extract_png_thumbnail(monkeypatch: pytest.MonkeyPatch) -> None:
    """最长边 480 的 PNG 缩略图；默认不 OCR，正文留空。"""
    from PIL import Image

    monkeypatch.delenv("KB_OCR", raising=False)
    buffer = io.BytesIO()
    Image.new("RGB", (1600, 900), (20, 90, 160)).save(buffer, format="PNG")

    result = _assert_shape(kb_extract.extract(buffer.getvalue(), "image/png", "cover.png"))

    assert result["kind"] == "image"
    assert result["text"] == ""
    assert result["title"] == ""
    assert isinstance(result["thumbnail"], bytes)
    assert max(_png_size(result["thumbnail"])) == 480
    assert "OCR" in result["notes"]


def test_extract_broken_image_degrades() -> None:
    """坏图只记 notes，不抛异常。"""
    result = _assert_shape(
        kb_extract.extract(PNG_MAGIC + b"not really a png", "image/png", "bad.png")
    )

    assert result["kind"] == "image"
    assert result["text"] == ""
    assert result["thumbnail"] is None
    assert result["notes"].startswith("image:")


# ---------------------------------------------------------------- 音视频


@needs_ffmpeg
def test_extract_video_without_transcribe(media_files: dict[str, Path]) -> None:
    """transcribe=False：仍要拿到时长和抽帧缩略图，转写为空。"""
    payload = media_files["video"].read_bytes()

    result = _assert_shape(
        kb_extract.extract(payload, "video/mp4", "silent.mp4", transcribe=False)
    )

    assert result["kind"] == "video"
    assert result["duration"] > 0.0
    assert result["transcript"] == ""
    assert isinstance(result["thumbnail"], bytes)
    assert max(_png_size(result["thumbnail"])) <= 480


@needs_ffmpeg
def test_extract_audio_without_transcribe(media_files: dict[str, Path]) -> None:
    """音频没有视频帧，缩略图必须是 None。"""
    payload = media_files["audio"].read_bytes()

    result = _assert_shape(
        kb_extract.extract(payload, "audio/mpeg", "silent.mp3", transcribe=False)
    )

    assert result["kind"] == "audio"
    assert result["duration"] > 0.0
    assert result["transcript"] == ""
    assert result["thumbnail"] is None


@needs_ffmpeg
def test_transcribe_degrades_when_model_unavailable(
    media_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型/sherpa_onnx 不可用时：转写为空 + notes 说明，时长照旧，不抛异常。"""
    monkeypatch.setattr(kb_extract, "_ASR", None)
    monkeypatch.setattr(kb_extract, "_ASR_ERROR", "SenseVoice 模型未安装")
    payload = media_files["audio"].read_bytes()

    result = _assert_shape(kb_extract.extract(payload, "audio/mpeg", "silent.mp3"))

    assert result["kind"] == "audio"
    assert result["duration"] > 0.0
    assert result["transcript"] == ""
    assert "asr: SenseVoice 模型未安装" in result["notes"]


# ---------------------------------------------------------------- 兜底


def test_extract_unknown_type() -> None:
    result = _assert_shape(
        kb_extract.extract(b"SQLite format 3\x00\x00\x01", "application/x-sqlite3", "kb.sqlite")
    )

    assert result["kind"] == "other"
    assert result["text"] == ""
    assert result["title"] == ""
    assert result["notes"] == "未支持的类型"


def test_extract_empty_input() -> None:
    result = _assert_shape(kb_extract.extract(b"", "text/plain", "empty.txt"))

    assert result["text"] == ""
    assert result["duration"] == 0.0
    assert result["notes"] == "空文件，跳过抽取"


def test_extract_oversize_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 MAX_BYTES 不再解码（这里把上限压低，避免真造 200MB）。"""
    monkeypatch.setattr(kb_extract, "MAX_BYTES", 16)

    result = _assert_shape(kb_extract.extract(b"x" * 32, "text/plain", "big.txt"))

    assert result["text"] == ""
    assert result["notes"] == "文件过大，跳过抽取"


def test_extract_pdf_cli_failure_only_notes() -> None:
    """坏 PDF：pdftotext/pdftoppm 失败只落 notes，正文为空但不抛。"""
    result = _assert_shape(
        kb_extract.extract(b"%PDF-1.4\n1 0 obj\n<<>>\ntrailer\n", "application/pdf", "bad.pdf")
    )

    assert result["kind"] == "pdf"
    assert result["text"] == ""
    assert result["thumbnail"] is None
    assert "pdf:" in result["notes"]


# ---------------------------------------------------------------- 真实转写


@pytest.mark.skipif(not REAL_AUDIO.exists(), reason=f"缺少真实语音素材：{REAL_AUDIO}")
def test_transcribe_real_speech() -> None:
    """真实中文语音必须转出「上下文」二字（SenseVoice，CPU）。"""
    result = _assert_shape(
        kb_extract.extract(REAL_AUDIO.read_bytes(), "audio/mpeg", REAL_AUDIO.name)
    )

    assert result["kind"] == "audio"
    assert result["duration"] > 0.0
    assert result["transcript"], f"转写为空，notes={result['notes']!r}"
    assert "上下文" in result["transcript"]
    assert "asr:" not in result["notes"]
