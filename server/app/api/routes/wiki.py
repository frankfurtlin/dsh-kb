"""LLM Wiki 接口（P12 与知识图谱拆分）：AI 撰写的长文页。

当前为占位实现：文章生成在下一阶段接入，接口先返回空集与明确的 501，
避免前端在「LLM Wiki」入口里看到图谱节点而误以为是 wiki 文章。
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import ROUTE_PREFIX
from app.services import kb_wiki

router = APIRouter()


def _prefix(request: Request) -> str:
    path = request.url.path
    if path.startswith(ROUTE_PREFIX + "/"):
        return ROUTE_PREFIX
    return ""


@router.get("/api/wiki/articles")
def wiki_articles(kind: str = "", q: str = "", limit: int = 50, offset: int = 0):
    return kb_wiki.article_list(kind=kind, q=q, limit=min(max(limit, 1), 200), offset=max(offset, 0))


@router.get("/api/wiki/article/{slug:path}")
def wiki_article(slug: str):
    article = kb_wiki.article_detail(slug)
    if not article:
        return JSONResponse(status_code=404, content={"ok": False, "error": "wiki 文章不存在"})
    return article


@router.get("/api/wiki/stats")
def wiki_stats():
    return kb_wiki.stats()


@router.get("/api/wiki/lint")
def wiki_lint():
    return kb_wiki.lint()


@router.post("/api/wiki/compile")
def wiki_compile():
    return JSONResponse(
        status_code=501,
        content={"ok": False, "mode": "disabled",
                 "error": "LLM Wiki 文章生成尚未启用（P12 下一步）：当前可用的「知识图谱」在 /api/graph/*"},
    )


_PAGE_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 28px 32px 64px; max-width: 780px; font: 15px/1.75 -apple-system,
  "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #1f2430; background: #fff; }
a { color: #4f46e5; text-decoration: none; border-bottom: 1px solid rgba(79,70,229,.25); }
h1 { font-size: 26px; margin: 0 0 10px; }
.nav { margin-bottom: 18px; font-size: 13px; color: #6b7280; }
blockquote { margin: 14px 0; padding: 10px 14px; border-left: 3px solid #c7d2fe; color: #4b5563;
  background: #f8f9ff; border-radius: 4px; }
@media (prefers-color-scheme: dark) {
  body { color: #e5e7eb; background: #16171d; }
  blockquote { background: #1f2230; border-color: #4c51bf; color: #cbd5e1; }
  a { color: #a5b4fc; }
}
"""


def _shell(title: str, body: str, prefix: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)} · LLM Wiki</title><style>{_PAGE_CSS}</style></head>
<body><div class="nav"><a href="{prefix}/wiki/">LLM Wiki</a> · <a href="{prefix}/graph/">知识图谱</a> · <a href="{prefix}/">返回知识库</a></div>
{body}</body></html>"""


@router.get("/wiki/", response_class=HTMLResponse)
@router.get("/wiki", response_class=HTMLResponse)
def wiki_index_html(request: Request):
    body = (
        "<h1>LLM Wiki</h1>"
        "<blockquote>这一层是「由模型撰写的知识页」：把某个主题/实体相关的报告综合成一篇带引用的长文，"
        "而不是节点列表。它和「知识图谱」（节点 + 共现边 + 关系检索）是两层：图谱回答关系与路径，"
        "Wiki 回答「目前我知道什么」。</blockquote>"
        "<h2>当前状态</h2>"
        "<p>P12 已完成架构拆分：图谱能力在 <a href=\"" + _prefix(request) + "/graph/\">知识图谱</a>，"
        "本入口暂为占位。Wiki 文章表（<code>wiki_article</code>）、AI 撰写、增量重写与"
        "「重新生成」按钮在下一阶段接入。</p>"
        "<p>届时每篇文章会带：来源报告引用（[n]）、生成模型与时间、来源 hash（用于增量）、"
        "以及过期/断链体检（lint）。</p>"
    )
    return HTMLResponse(_shell("LLM Wiki", body, _prefix(request)))


@router.get("/wiki/{slug:path}", response_class=HTMLResponse)
def wiki_article_html(slug: str, request: Request):
    article = kb_wiki.article_detail(slug)
    if not article:
        return HTMLResponse(
            _shell("文章尚未生成", "<h1>文章尚未生成</h1><p>LLM Wiki 文章生成将在下一阶段接入。</p>"
                   f'<p><a href="{_prefix(request)}/graph/">去知识图谱浏览实体与主题</a></p>', _prefix(request)),
            status_code=404,
        )
    return HTMLResponse(_shell(article["title"], f"<h1>{html.escape(article['title'])}</h1><pre>{html.escape(article['body_md'])}</pre>", _prefix(request)))
