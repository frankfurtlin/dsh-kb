"""知识库页面与健康检查：`/`、`/kb/`、`/healthz`、`/kb/healthz`。"""

from __future__ import annotations

import time

import html as html_lib
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.config import APP_VERSION, ASSET_VERSION, TEMPLATES_DIR
from app.services import kb_ai, kb_embed, kb_store

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_STARTED_AT = time.time()


@router.get("/")
def kb_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_title": "知识库", "app_version": APP_VERSION, "asset_version": ASSET_VERSION},
        # 页面本身不缓存：静态资源用 ?v=<指纹> 走长缓存，页面取到的一定是新 HTML。
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/healthz")
def healthz():
    """轻量健康检查：进程活着 + 索引可读 + AI/向量能力状态（不触发模型加载）。"""
    try:
        stats = kb_store.overview()
        ok = True
        error = ""
    except Exception as exc:  # 索引损坏/被锁时给出可诊断信息，而不是 500 堆栈
        stats = {"total": 0, "unread": 0, "starred": 0, "public": 0}
        ok = False
        error = f"{type(exc).__name__}: {exc}"[:200]
    body = {
        "ok": ok,
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "reports": stats,
        "ai": {"configured": kb_ai.is_configured()},
        "embed": {
            "enabled": config.EMBED_ENABLED,
            "available": kb_embed.status().get("available", False),
        },
    }
    if error:
        body["error"] = error
    return JSONResponse(body, status_code=200 if ok else 503)


def _kb_api_base(request: Request) -> str:
    """页面可能挂在 `/` 或 `/kb` 前缀下，按实际路径推 API 前缀。"""
    path = request.url.path or "/"
    return "/kb/api" if path.startswith("/kb/") else "/api"


def _fmt_size(n: int) -> str:
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


@router.get("/doc/{uid}", response_class=HTMLResponse)
def kb_document_page(uid: str, request: Request):
    """DB 型文档的只读查看页（iframe 里「打开」落到这里）。"""
    from app.services import kb_blob

    item = kb_store.get_document((uid or "").strip())
    api = _kb_api_base(request)
    if item is None:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>文档不存在</title>"
            "<div style='font:15px -apple-system,system-ui;padding:48px;color:#666'>文档不存在或已删除。</div>",
            status_code=404,
        )

    mime = item.get("mime") or ""
    blob = item.get("blob_sha") or ""
    body = item.get("body") or ""
    title = item.get("title") or "未命名文档"
    meta_bits = []
    for label, value in (
        ("分类", item.get("category")),
        ("系列", item.get("series")),
        ("日期", item.get("published_at") or item.get("date")),
        ("来源", item.get("source")),
        ("类型", mime),
        ("大小", _fmt_size(int(item.get("size") or 0))),
    ):
        if value:
            meta_bits.append(f"<span class='chip'>{html_lib.escape(label)} · {html_lib.escape(str(value))}</span>")

    blob_url = f"{api}/kb/blob/{blob}" if blob else ""
    if blob_url and mime == "text/html":
        # 迁移进来的报告保持原始排版：沙箱 iframe 渲染原文（脚本被 CSP/sandbox 关掉）。
        # 公开报告的相对素材只在 /kb-public 前缀下能解析，直接嵌公开链接。
        frame_src = (
            f"/kb-public/reports/{quote(str(item.get('path') or ''))}"
            if item.get("public")
            else blob_url
        )
        viewer = f"<iframe class='doc-frame' sandbox src='{html_lib.escape(frame_src)}' title='{html_lib.escape(title)}'></iframe>"
    elif blob_url and mime.startswith("image/"):
        viewer = f"<img class='media' src='{blob_url}' alt='{html_lib.escape(title)}'>"
    elif blob_url and mime.startswith("video/"):
        viewer = f"<video class='media' controls preload='metadata' src='{blob_url}'></video>"
    elif blob_url and mime.startswith("audio/"):
        viewer = f"<audio controls src='{blob_url}'></audio>"
    elif blob_url and mime == "application/pdf":
        viewer = f"<iframe class='pdf' src='{blob_url}' title='PDF'></iframe>"
    else:
        viewer = "<pre class='text'>" + html_lib.escape(body or "（没有可展示的正文，可下载原文）") + "</pre>"

    download = (
        f"<a class='btn' href='{blob_url}?download=1'>下载原文</a>" if blob_url else ""
    )
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)}</title>
<style>
:root{{color-scheme:light dark}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 32px 64px;font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:#1c1c22;background:#faf9f7}}
@media (prefers-color-scheme:dark){{body{{background:#16161c;color:#ececf2}}}}
h1{{font-size:22px;line-height:1.35;margin:0 0 10px;max-width:860px}}
.meta{{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 20px}}
.chip{{font-size:12px;color:#6b7280;background:rgba(127,127,127,.1);border-radius:999px;padding:2px 10px}}
.text{{white-space:pre-wrap;word-break:break-word;max-width:880px;font:14px/1.75 inherit;margin:0}}
.media{{max-width:100%;border-radius:10px;display:block}}
.pdf{{width:100%;height:78vh;border:1px solid rgba(127,127,127,.25);border-radius:10px}}
.doc-frame{{width:100%;height:82vh;border:1px solid rgba(127,127,127,.25);border-radius:10px;background:#fff}}
.btn{{display:inline-block;margin-top:24px;font-size:13px;text-decoration:none;color:inherit;border:1px solid rgba(127,127,127,.35);border-radius:8px;padding:6px 14px}}
.btn:hover{{background:rgba(127,127,127,.12)}}
</style></head>
<body>
<h1>{html_lib.escape(title)}</h1>
<div class="meta">{''.join(meta_bits)}</div>
{viewer}
{download}
</body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})
