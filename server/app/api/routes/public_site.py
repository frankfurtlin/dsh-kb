"""公开报告站点：`/kb-public/...` 下的匿名只读访问。

这个前缀**不经过 DSH 鉴权**：它的入口是 Caddy 在 hermes 域名上的一条独立反代
（`/kb-public/* → 127.0.0.1:8816`），只由本模块提供内容，因此用户可以把单篇
报告公开出去，让没有 DSH 账号的人直接打开链接。

这里只做三件事：
- `/kb-public/`：公开报告目录页（只有已公开的条目）；
- `/kb-public/reports/<rel>`：命中 `kb_public.is_public()` 的报告页，内链改写
  到 `/kb-public/...` 前缀，缓存 `no-cache`；
- `/kb-public/reports/<rel>`（非 HTML）：命中 `kb_public.allowed_asset()` 的
  公开报告附件才返回，其它一律 404。

其余路径（含 `/api/*`）不会在这个路由器里注册，所以不会被匿名访问到。
"""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.services import kb_public, report_html

router = APIRouter()

_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;background:#f6f6f8;color:#1c1c22;font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
@media (prefers-color-scheme:dark){body{background:#16161c;color:#ececf2}.card{background:#1f1f28;border-color:#2c2c38}a{color:#9d8cff}}
.wrap{max-width:760px;margin:0 auto;padding:40px 20px 64px}
h1{font-size:22px;margin:0 0 6px}
.sub{color:#8a8a96;font-size:13px;margin:0 0 28px}
.card{display:block;background:#fff;border:1px solid #e4e4ea;border-radius:12px;padding:16px 18px;margin:0 0 12px;text-decoration:none;color:inherit}
.card:hover{border-color:#9d8cff}
.meta{font-size:12px;color:#8a8a96;margin-bottom:4px}
.title{font-size:16px;font-weight:600;margin:0;color:inherit}
.summary{font-size:13px;color:#5f5f6b;margin:6px 0 0}
.empty{color:#8a8a96;border:1px dashed #d8d8e0;border-radius:12px;padding:24px;text-align:center}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:18px}
.head h1{margin:0;flex:1 1 auto}
.brand{font-size:12px;color:#8a8a96;letter-spacing:.12em;text-transform:uppercase}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}
.chip{display:inline-block;padding:4px 12px;border:1px solid #e4e4ea;border-radius:999px;color:#5f5f6b;text-decoration:none;font-size:12.5px;background:#fff}
.chip.on{border-color:#9d8cff;color:#6b5fc4;background:rgba(157,140,255,.12)}
@media (prefers-color-scheme:dark){.chip{background:#1f1f28;border-color:#2c2c38;color:#c8c8d4}.chip.on{color:#b9adff;border-color:#6b5fc4}}
.meta .read{color:#8a8a96}
footer a{color:inherit}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<meta name=\"robots\" content=\"noindex, nofollow\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class=\"wrap\">{body}"
        "<footer>此页由 dsh-kb 知识库插件生成，仅列出作者主动公开的报告。</footer>"
        "</div></body></html>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


@router.api_route("/kb-public", methods=["GET", "HEAD"])
def public_root():
    return Response(status_code=307, headers={"Location": f"{kb_public.PUBLIC_PREFIX}/"})


@router.api_route("/kb-public/", methods=["GET", "HEAD"])
def public_index(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200, media_type="text/html; charset=utf-8")
    items = kb_public.public_reports()
    if not items:
        body = (
            "<h1>公开知识库</h1>"
            "<p class=\"sub\">作者还没有公开任何报告。</p>"
            "<div class=\"empty\">在 DSH 的「知识库」面板里打开一篇报告，点「公开」后即可生成外链。</div>"
        )
        return HTMLResponse(_page("公开知识库", body), headers={"Cache-Control": "no-cache"})
    # P2-1：分类筛选（?cat=）与阅读时长，让公开页像一个能逛的目录而不是一列链接。
    cat = (request.query_params.get("cat") or "").strip()
    categories = sorted({str(item.get("category") or "其他") for item in items})
    filtered = [item for item in items if not cat or str(item.get("category") or "其他") == cat]
    chips = [f'<a class="chip{"" if cat else " on"}" href="{kb_public.PUBLIC_PREFIX}/">全部 {len(items)}</a>']
    for name in categories:
        count = sum(1 for item in items if str(item.get("category") or "其他") == name)
        active = " on" if cat == name else ""
        chips.append(
            f'<a class="chip{active}" href="{kb_public.PUBLIC_PREFIX}/?cat={quote(name)}">{_esc(name)} {count}</a>'
        )
    latest = (items[0].get("date") or "") if items else ""
    cards = []
    for item in filtered:
        rel = quote(item["path"])
        summary = item.get("summary_ai") or item.get("summary") or ""
        size_kb = max(1, int(item.get("size") or 0) // 1024)
        minutes = max(1, round(size_kb / 12))  # 中文阅读约 12KB/分钟（含 HTML 标记）
        cards.append(
            f'<a class="card" href="{kb_public.PUBLIC_REPORTS_PREFIX}/{rel}">'
            f'<div class="meta">{_esc(item.get("date"))} · {_esc(item.get("category"))}'
            f'<span class="read"> · 约 {minutes} 分钟</span></div>'
            f'<p class="title">{_esc(item.get("title"))}</p>'
            f'<p class="summary">{_esc(summary[:140])}{"…" if len(summary) > 140 else ""}</p>'
            "</a>"
        )
    if not cards:
        cards.append('<div class="empty">这个分类下还没有公开报告。</div>')
    head = (
        '<div class="head"><span class="brand">公开知识库</span>'
        f'<h1>{_esc(cat or "全部公开报告")}</h1></div>'
        f'<p class="sub">共 {len(filtered)} 篇 · 最近更新 {_esc(latest)} · 只读外链，无需账号</p>'
        f'<div class="chips">{"".join(chips)}</div>'
    )
    footer = (
        '<footer>此页由 <strong>dsh-kb</strong> 知识库插件生成，仅列出作者主动公开的报告；'
        f'最近同步 {_esc(latest)}。</footer>'
    )
    page = _page(cat or "公开知识库", head + "".join(cards))
    page = page.replace(
        "<footer>此页由 dsh-kb 知识库插件生成，仅列出作者主动公开的报告。</footer>", footer
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


def _serve(rel: str, head: bool):
    if rel in ("", "/"):
        raise HTTPException(status_code=404, detail="报告不存在")
    path = report_html.safe_path(rel)
    if path is not None and path.is_dir():
        index = path / "index.html"
        if index.is_file():
            path = index
            rel = rel.rstrip("/") + "/index.html"
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="报告不存在")

    if path.suffix.lower() in (".html", ".htm"):
        if not kb_public.is_public(rel):
            raise HTTPException(status_code=404, detail="报告不存在")
        if head:
            return Response(status_code=200, media_type="text/html; charset=utf-8")
        text = path.read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(
            report_html.rewrite_html(text, prefix=kb_public.PUBLIC_PREFIX),
            headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex, nofollow"},
        )

    if not kb_public.allowed_asset(rel):
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(
        path,
        headers={"Cache-Control": "public, max-age=300", "X-Robots-Tag": "noindex"},
    )


@router.api_route("/kb-public/reports", methods=["GET", "HEAD"])
def public_reports_root(request: Request):
    return _serve("index.html", head=request.method == "HEAD")


@router.api_route("/kb-public/reports/{path:path}", methods=["GET", "HEAD"])
def public_report_file(path: str, request: Request):
    return _serve(path, head=request.method == "HEAD")
