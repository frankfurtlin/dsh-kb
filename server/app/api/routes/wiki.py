"""知识图谱 / LLM Wiki 接口：页表查询、页详情、体检、重建，以及可直接浏览的 HTML 页。"""

from __future__ import annotations

import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import ROUTE_PREFIX
from app.services import kb_wiki

router = APIRouter()


class CompileBody(BaseModel):
    force: bool = False


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


def _prefix(request: Request) -> str:
    """根据访问路径决定内链前缀：DSH iframe 用 /kb，直连后端用空。"""
    path = request.url.path
    if path.startswith(ROUTE_PREFIX + "/"):
        return ROUTE_PREFIX
    return ""


@router.get("/api/wiki/pages")
def wiki_pages(kind: str = "", state: str = "", q: str = "", limit: int = 50, offset: int = 0):
    return kb_wiki.page_list(kind=kind, state=state, q=q, limit=min(max(limit, 1), 200),
                             offset=max(offset, 0))


@router.get("/api/wiki/page/{slug:path}")
def wiki_page(slug: str):
    page = kb_wiki.page_detail(slug)
    if not page:
        return _error(404, "wiki 页不存在")
    return page


@router.get("/api/wiki/stats")
def wiki_stats():
    return kb_wiki.stats()


@router.get("/api/wiki/lint")
def wiki_lint():
    return kb_wiki.lint()


@router.post("/api/wiki/compile")
def wiki_compile(body: CompileBody | None = None):
    try:
        result = kb_wiki.build(force=bool(body.force) if body else False)
    except Exception as exc:  # 编译失败回结构化错误
        return _error(500, f"{type(exc).__name__}: {exc}")
    return {"ok": True, **result}


# ---------- 可直接浏览的 markdown 页 ----------

_WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)

    def link(match: re.Match) -> str:
        target = match.group(1).strip()
        label = html.escape((match.group(2) or target).strip())
        if target.startswith("report:"):
            path = target[len("report:"):]
            return f'<a href="{html.escape(prefix + "/reports/" + path)}">{label}</a>'
        return f'<a href="{html.escape(prefix + "/wiki/" + target)}">{label}</a>'

    return _WIKI_LINK_RE.sub(link, text)


def render_markdown(markdown: str, prefix: str = "") -> str:
    """极简 markdown 渲染（够 wiki 页用：标题/列表/引用/段落/链接/粗体/行内代码）。"""
    globals()["prefix"] = prefix
    out: list[str] = []
    in_list = in_ul = in_ol = False
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("---") and not out:
            continue  # 跳过 front matter 分隔符
        front = line
        if front.startswith("# "):
            html_line = f"<h1>{_inline(front[2:])}</h1>"
        elif front.startswith("## "):
            html_line = f"<h2>{_inline(front[3:])}</h2>"
        elif front.startswith("### "):
            html_line = f"<h3>{_inline(front[4:])}</h3>"
        elif front.startswith(">"):
            html_line = f"<blockquote>{_inline(front[1:].strip())}</blockquote>"
        elif re.match(r"^\s*[-*]\s+", front):
            item = _inline(re.sub(r"^\s*[-*]\s+", "", front))
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            html_line = f"<li>{item}</li>"
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not front.strip():
                continue
            html_line = f"<p>{_inline(front)}</p>"
        if in_ul and not html_line.startswith("<li>"):
            out.append("</ul>")
            in_ul = False
        out.append(html_line)
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


_PAGE_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 28px 32px 64px; max-width: 860px; font: 15px/1.75 -apple-system,
  "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #1f2430; background: #fff; }
a { color: #4f46e5; text-decoration: none; border-bottom: 1px solid rgba(79,70,229,.25); }
a:hover { border-bottom-color: currentColor; }
h1 { font-size: 26px; margin: 0 0 6px; }
h2 { font-size: 17px; margin: 26px 0 8px; }
blockquote { margin: 10px 0; padding: 8px 12px; border-left: 3px solid #c7d2fe; color: #4b5563;
  background: #f8f9ff; border-radius: 4px; }
ul { padding-left: 22px; }
li { margin: 3px 0; }
code { background: #eef0f6; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
.nav { margin-bottom: 18px; font-size: 13px; color: #6b7280; }
@media (prefers-color-scheme: dark) {
  body { color: #e5e7eb; background: #16171d; }
  blockquote { background: #1f2230; border-color: #4c51bf; color: #cbd5e1; }
  code { background: #262936; }
  a { color: #a5b4fc; border-bottom-color: rgba(165,180,252,.3); }
}
"""


def _shell(title: str, body: str, prefix: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)} · 知识库 Wiki</title><style>{_PAGE_CSS}</style></head>
<body><div class="nav"><a href="{prefix}/wiki/">Wiki 索引</a> · <a href="{prefix}/">返回知识库</a></div>
{body}</body></html>"""


@router.get("/wiki/", response_class=HTMLResponse)
@router.get("/wiki", response_class=HTMLResponse)
def wiki_index_html(request: Request):
    pages = kb_wiki.page_list(limit=400)["items"]
    stats = kb_wiki.stats()

    def _list(kind: str) -> str:
        entries = [item for item in pages if item["kind"] == kind]
        return "".join(
            f'<li><a href="{_prefix(request)}/wiki/{item["slug"]}">{html.escape(item["title"])}</a>'
            f' <span class="muted">{html.escape(item["entity_type"] or "")} · {item["report_count"]} 篇</span></li>'
            for item in entries
        ) or "<li>暂无页面</li>"

    body = (
        f"<h1>知识库 Wiki</h1><blockquote>{stats.get('entities', 0)} 个实体页 · "
        f"{stats.get('topics', 0)} 个主题页 · {stats['sources']} 条报告引用 · "
        f"{stats['links']} 条互链 · {stats.get('edges', 0)} 条共现边</blockquote>"
        f"<h2>实体页（按提及报告数排序）</h2><ul>{_list('entity')}</ul>"
        f"<h2>主题页（按标签覆盖报告数排序）</h2><ul>{_list('topic')}</ul>"
        "<p class=\"muted\">页面由 <code>reports/</code> 的 AI 实体与标签自动编译；"
        "重新编译见 <code>POST /api/wiki/compile</code>。</p>"
    )
    return HTMLResponse(_shell("Wiki 索引", body, _prefix(request)))


@router.get("/wiki/{slug:path}", response_class=HTMLResponse)
def wiki_page_html(slug: str, request: Request):
    page = kb_wiki.page_detail(slug)
    if not page:
        return HTMLResponse(_shell("未找到", "<h1>未找到该页</h1>", _prefix(request)), status_code=404)
    body = render_markdown(page["body_md"], _prefix(request))
    return HTMLResponse(_shell(page["title"], body, _prefix(request)))
