"""投喂入口：把任意内容变成一条 DB 型文档（不写 `reports/`）。

链路：

    bytes/text/url
        → kb_extract.extract  （正文、标题、缩略图、音视频转写）
        → kb_blob.put          （原始件内容寻址；>10MB 主存 COS 加密副本）
        → kb_store.create_document（一行记录，uid 作为对外身份）

日期来自 `published_at`（缺省=今天），**不再从目录名推导**；同一份内容
（sha1 相同）重复投喂只留一条记录。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime

from app.services import kb_blob, kb_extract, kb_store

#: 单次投喂的原始字节上限（超过直接拒绝，避免把内存打爆）。
MAX_BYTES = int(os.environ.get("KB_INGEST_MAX_BYTES", str(512 * 1024 * 1024)))
#: 抓 URL 时的响应上限。
MAX_FETCH_BYTES = int(os.environ.get("KB_INGEST_MAX_FETCH_BYTES", str(20 * 1024 * 1024)))
FETCH_TIMEOUT = float(os.environ.get("KB_INGEST_FETCH_TIMEOUT", "15"))

_URL_RE = re.compile(r"^https?://\S+$", re.I)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _clean_title(value: str) -> str:
    return " ".join((value or "").split())[:200]


def _fallback_title(name: str, mime: str) -> str:
    base = os.path.basename(name or "").strip()
    if base:
        return base.rsplit(".", 1)[0][:200] or base[:200]
    kind = (mime or "").split("/", 1)[0]
    return {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, "未命名文档")


def _extract_summary(text: str, limit: int = 180) -> str:
    body = " ".join((text or "").split())
    return body[:limit]


def ingest_bytes(
    data: bytes,
    *,
    name: str = "",
    mime: str = "",
    title: str = "",
    published_at: str = "",
    source: str = "upload",
    category: str = "",
    series: str = "",
    tags: list[str] | None = None,
    transcribe: bool = True,
    max_media_seconds: int = 1200,
    source_url: str = "",
) -> dict:
    """投喂一段字节（上传文件 / 剪藏正文 / 音视频）。"""
    if not data:
        return {"ok": False, "error": "内容为空"}
    if len(data) > MAX_BYTES:
        return {
            "ok": False,
            "error": f"文件过大：{len(data)} 字节，上限 {MAX_BYTES} 字节",
        }

    mime = (mime or "").strip().lower() or kb_extract.detect_mime(name or "", data)
    try:
        extracted = kb_extract.extract(
            data,
            mime,
            name or "",
            transcribe=transcribe,
            max_media_seconds=max_media_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 抽取失败不该让投喂整单失败
        extracted = {
            "text": "",
            "title": "",
            "kind": "other",
            "transcript": "",
            "duration": 0.0,
            "thumbnail": None,
            "thumbnail_mime": "",
            "notes": f"extract 异常：{type(exc).__name__}: {exc}"[:300],
        }

    # 原始字节进 blob（>10MB 主存 COS 加密副本），缩略图也进 blob。
    blob = kb_blob.put(data, mime, name)
    thumb_sha = ""
    if extracted.get("thumbnail"):
        try:
            thumb = kb_blob.put(extracted["thumbnail"], "image/png", f"{name or 'doc'}.thumb.png")
            thumb_sha = thumb["sha"]
        except Exception:  # noqa: BLE001 - 缩略图失败不影响正文
            thumb_sha = ""

    body = (extracted.get("text") or "").strip()
    transcript = (extracted.get("transcript") or "").strip()
    if transcript and not body:
        body = transcript
    elif transcript and body and transcript not in body:
        # 音视频：正文就是转写；其它格式的 transcript 为空，不会走到这里。
        body = f"{body}\n\n[转写]\n{transcript}"

    resolved_title = _clean_title(title) or _clean_title(extracted.get("title") or "") or _fallback_title(name, mime)

    doc = kb_store.create_document(
        title=resolved_title,
        body=body[:20000],
        summary=_extract_summary(body),
        mime=mime,
        blob_sha=blob["sha"],
        thumb_sha=thumb_sha,
        size=len(data),
        published_at=(published_at or "").strip() or _today(),
        source=source or "upload",
        category=category,
        series=series,
        tags=tags or [],
        storage_tier=blob.get("tier", "local"),
        content_hash=hashlib.sha1(data).hexdigest(),
        source_url=source_url,
    )
    return {
        "ok": True,
        "document": doc,
        "blob": blob,
        "extract": {
            "kind": extracted.get("kind", ""),
            "chars": len(body),
            "transcript_chars": len(transcript),
            "duration": extracted.get("duration", 0.0),
            "thumbnail": bool(thumb_sha),
            "notes": extracted.get("notes", ""),
        },
    }


def ingest_text(
    text: str,
    *,
    title: str = "",
    source: str = "paste",
    published_at: str = "",
    category: str = "",
    series: str = "",
    tags: list[str] | None = None,
    mime: str = "text/markdown",
    name: str = "",
) -> dict:
    """投喂一段纯文本 / Markdown / HTML 字符串（不带文件）。"""
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "error": "文本为空"}
    looks_html = raw.lstrip().lower().startswith(("<!doctype html", "<html", "<body", "<article"))
    resolved_mime = "text/html" if looks_html else (mime or "text/markdown")
    return ingest_bytes(
        raw.encode("utf-8"),
        name=name or ("paste.html" if looks_html else "paste.md"),
        mime=resolved_mime,
        title=title,
        published_at=published_at,
        source=source,
        category=category,
        series=series,
        tags=tags,
        transcribe=False,
    )


def ingest_url(
    url: str,
    *,
    title: str = "",
    published_at: str = "",
    category: str = "",
    series: str = "",
    tags: list[str] | None = None,
    source: str = "url",
) -> dict:
    """抓一个网页并投喂（抓正文交给提取器，失败返回可读错误）。"""
    target = (url or "").strip()
    if not _URL_RE.match(target):
        return {"ok": False, "error": "不是合法的 http(s) 链接"}
    try:
        import requests

        resp = requests.get(
            target,
            timeout=FETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - 网络错误直接回给前端
        return {"ok": False, "error": f"抓取失败：{type(exc).__name__}: {exc}"[:300]}

    with resp:
        if resp.status_code >= 400:
            return {"ok": False, "error": f"抓取失败：HTTP {resp.status_code}"}
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_FETCH_BYTES:
                return {"ok": False, "error": f"页面过大：超过 {MAX_FETCH_BYTES} 字节"}
            chunks.append(chunk)
        data = b"".join(chunks)
        mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower() or "text/html"

    if mime.startswith("text/") or mime in ("application/xhtml+xml", "application/xml"):
        pass
    elif mime in ("application/pdf",) or mime.startswith("image/") or mime.startswith("audio/") or mime.startswith("video/"):
        pass
    else:
        return {"ok": False, "error": f"不支持的内容类型：{mime}"}

    return ingest_bytes(
        data,
        name=os.path.basename(target.split("?", 1)[0]) or "webpage",
        mime=mime,
        title=title,
        published_at=published_at,
        source=source,
        category=category,
        series=series,
        tags=tags,
        transcribe=mime.startswith(("audio/", "video/")),
    )
