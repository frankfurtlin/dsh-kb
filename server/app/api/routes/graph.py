"""知识图谱接口：节点（实体/主题）列表、节点详情、邻居、相关报告、体检、重建与浏览页。

P12 起「知识图谱」与「LLM Wiki」分成两层：本模块只提供图（节点/边/PPR 相关查询），
不产出任何长文；LLM 撰写的文章在 `routes/wiki.py`（下一阶段接入）。
"""

from __future__ import annotations

import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import ROUTE_PREFIX
from app.services import kb_graph

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


@router.get("/api/graph/nodes")
def graph_nodes(kind: str = "", state: str = "", q: str = "", limit: int = 50, offset: int = 0):
    return kb_graph.page_list(kind=kind, state=state, q=q, limit=min(max(limit, 1), 400),
                              offset=max(offset, 0))


@router.get("/api/graph/node/{slug:path}")
def graph_node(slug: str):
    page = kb_graph.page_detail(slug)
    if not page:
        return _error(404, "节点不存在")
    return page


@router.get("/api/graph/neighbors/{slug:path}")
def graph_neighbors(slug: str, limit: int = 20):
    result = kb_graph.neighbors(slug, limit=min(max(limit, 1), 100))
    if not result.get("ok"):
        return _error(404, result.get("error", "节点不存在"))
    return result


@router.get("/api/graph/related/{slug:path}")
def graph_related(slug: str, limit: int = 20):
    result = kb_graph.related_reports(slug, limit=min(max(limit, 1), 100))
    if not result.get("ok"):
        return _error(404, result.get("error", "节点不存在"))
    return result


@router.get("/api/graph/stats")
def graph_stats():
    return kb_graph.graph_stats()


@router.get("/api/graph/lint")
def graph_lint():
    return kb_graph.lint()


@router.post("/api/graph/compile")
def graph_compile(body: CompileBody | None = None):
    try:
        result = kb_graph.build(force=bool(body.force) if body else False)
    except Exception as exc:  # 编译失败回结构化错误
        return _error(500, f"{type(exc).__name__}: {exc}")
    return {"ok": True, **result}


# ---------- 可直接浏览的节点页 ----------

_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _inline(text: str, prefix: str = "") -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)

    def link(match: re.Match) -> str:
        target = match.group(1).strip()
        label = html.escape((match.group(2) or target).strip())
        if target.startswith("report:"):
            path = target[len("report:"):]
            return f'<a href="{html.escape(prefix + "/reports/" + path)}">{label}</a>'
        return f'<a href="{html.escape(prefix + "/graph/" + target)}">{label}</a>'

    return _LINK_RE.sub(link, text)


def render_markdown(markdown: str, prefix: str = "") -> str:
    """极简 markdown 渲染（够节点页用：标题/列表/引用/段落/链接/粗体/行内代码）。"""
    out: list[str] = []
    in_ul = False
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("---") and not out:
            continue
        if line.startswith("# "):
            html_line = f"<h1>{_inline(line[2:], prefix)}</h1>"
        elif line.startswith("## "):
            html_line = f"<h2>{_inline(line[3:], prefix)}</h2>"
        elif line.startswith("### "):
            html_line = f"<h3>{_inline(line[4:], prefix)}</h3>"
        elif line.startswith(">"):
            html_line = f"<blockquote>{_inline(line[1:].strip(), prefix)}</blockquote>"
        elif re.match(r"^\s*[-*]\s+", line):
            item = _inline(re.sub(r"^\s*[-*]\s+", "", line), prefix)
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            html_line = f"<li>{item}</li>"
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not line.strip():
                continue
            html_line = f"<p>{_inline(line, prefix)}</p>"
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
.muted { color: #6b7280; font-size: 12px; }
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
<title>{html.escape(title)} · 知识图谱</title><style>{_PAGE_CSS}</style></head>
<body><div class="nav"><a href="{prefix}/graph/">知识图谱索引</a> · <a href="{prefix}/">返回知识库</a></div>
{body}</body></html>"""


@router.get("/graph/", response_class=HTMLResponse)
@router.get("/graph", response_class=HTMLResponse)
def graph_index_html(request: Request):
    pages = kb_graph.page_list(limit=400)["items"]
    stats = kb_graph.graph_stats()

    def _list(kind: str) -> str:
        entries = [item for item in pages if item["kind"] == kind]
        return "".join(
            f'<li><a href="{_prefix(request)}/graph/{item["slug"]}">{html.escape(item["title"])}</a>'
            f' <span class="muted">{html.escape(item["entity_type"] or "")} · {item["report_count"]} 篇</span></li>'
            for item in entries
        ) or "<li>暂无节点</li>"

    body = (
        f"<h1>知识图谱</h1><blockquote>{stats.get('entities', 0)} 个实体节点 · "
        f"{stats.get('topics', 0)} 个主题节点 · {stats['sources']} 条报告引用 · "
        f"{stats['links']} 条互链 · {stats.get('edges', 0)} 条共现边</blockquote>"
        f"<h2>实体节点（按提及报告数排序）</h2><ul>{_list('entity')}</ul>"
        f"<h2>主题节点（按标签覆盖报告数排序）</h2><ul>{_list('topic')}</ul>"
        "<p class=\"muted\">节点与共现边由 <code>reports/</code> 的 AI 实体与标签确定性编译；"
        "重新编译见 <code>POST /api/graph/compile</code>。"
        "长文阅读与 AI 综述在「LLM Wiki」入口（下一阶段）。</p>"
    )
    return HTMLResponse(_shell("知识图谱索引", body, _prefix(request)))


@router.get("/graph/{slug:path}", response_class=HTMLResponse)
def graph_node_html(slug: str, request: Request):
    page = kb_graph.page_detail(slug)
    if not page:
        return HTMLResponse(_shell("未找到", "<h1>未找到该节点</h1>", _prefix(request)), status_code=404)
    body = render_markdown(page["body_md"], _prefix(request))
    return HTMLResponse(_shell(page["title"], body, _prefix(request)))
