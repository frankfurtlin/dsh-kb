"""LLM Wiki 接口与浏览页（P13）：模型撰写的知识长文。

- API：`/api/wiki/articles`、`/api/wiki/article/{slug}`、`/api/wiki/stats`、
  `/api/wiki/lint`、`POST /api/wiki/compile`；
- HTML：`/wiki/` 索引（文章卡片 + 「生成/重新生成」按钮）、`/wiki/{slug}` 文章页
  （[n] 引用跳到页尾来源列表）。

文章由 `services/kb_wiki.py` 生成：后台按 source_hash 增量、或手动触发。
"""

from __future__ import annotations

import html as html_lib
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.api.routes.graph import render_markdown
from app.config import ROUTE_PREFIX
from app.services import kb_wiki
from app.services.kb_wiki import WikiError

router = APIRouter()


class CompileBody(BaseModel):
    slug: str = ""
    limit: int = 1
    force: bool = False


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


def _prefix(request: Request) -> str:
    """根据访问路径决定内链前缀：DSH iframe 用 /kb，直连后端用空。"""
    path = request.url.path
    if path.startswith(ROUTE_PREFIX + "/"):
        return ROUTE_PREFIX
    return ""


# ---------- API ----------


@router.get("/api/wiki/articles")
def wiki_articles(kind: str = "", q: str = "", limit: int = 50, offset: int = 0):
    return kb_wiki.article_list(
        kind=kind, q=q, limit=min(max(limit, 1), 200), offset=max(offset, 0)
    )


@router.get("/api/wiki/article/{slug:path}")
def wiki_article(slug: str):
    article = kb_wiki.article_detail(slug)
    if not article:
        return _error(404, "wiki 文章不存在")
    return article


@router.get("/api/wiki/stats")
def wiki_stats():
    return kb_wiki.stats()


@router.get("/api/wiki/lint")
def wiki_lint():
    return kb_wiki.lint()


@router.post("/api/wiki/compile")
def wiki_compile(body: CompileBody | None = None):
    body = body or CompileBody()
    if kb_wiki.stats().get("mode") != "ai":
        return _error(503, "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）")
    try:
        if body.slug:
            return kb_wiki.generate(body.slug, force=body.force)
        return kb_wiki.build_pending(limit=max(1, min(body.limit, 10)), force=body.force)
    except WikiError as exc:
        return _error(400, str(exc))


# ---------- HTML ----------

_PAGE_CSS = """
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1f2430; --muted:#6b7280; --accent:#4f46e5;
  --line:rgba(31,36,48,.12); --card:#f8f9fc; }
@media (prefers-color-scheme: dark) { :root { --bg:#16171d; --fg:#e5e7eb; --muted:#9aa3b2;
  --accent:#a5b4fc; --line:rgba(229,231,235,.14); --card:#1d1f28; } }
body { margin:0; padding:28px 32px 72px; max-width:860px; font:15px/1.75 -apple-system,
  "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; color:var(--fg); background:var(--bg); }
a { color:var(--accent); text-decoration:none; border-bottom:1px solid rgba(0,0,0,0); }
a:hover { border-bottom-color: currentColor; }
h1 { font-size:27px; margin:0 0 10px; } h2 { font-size:19px; margin:26px 0 8px; }
.readbar { position:fixed; top:0; left:0; right:0; height:3px; background:transparent; z-index:20; }
.readbar i { display:block; height:100%; width:0; background:linear-gradient(90deg,#b99a4e,#8c6f2f); transition:width .12s linear; }
.toc { border:1px solid var(--line,#e5dfd2); border-radius:12px; padding:12px 16px; margin:18px 0 6px;
  background:var(--bg-soft,#fbf8f1); font-size:13px; }
.toc-title { display:block; font-size:11px; letter-spacing:.14em; color:#8c8578; text-transform:uppercase; margin-bottom:6px; }
.toc ol { margin:0; padding-left:18px; } .toc li { margin:4px 0; }
.toc a { color:inherit; text-decoration:none; border-bottom:1px dotted rgba(0,0,0,.2); }
.toc a:hover { border-bottom-style:solid; }
h3 { font-size:16px; margin:20px 0 6px; }
.nav { margin-bottom:18px; font-size:13px; color:var(--muted); }
blockquote { margin:14px 0; padding:10px 14px; border-left:3px solid var(--accent);
  color:var(--muted); background:var(--card); border-radius:4px; font-size:13.5px; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:14px; margin-top:20px; }
.card { border:1px solid var(--line); border-radius:10px; padding:14px 16px; background:var(--card); }
.card h3 { margin:0 0 6px; font-size:16px; }
.card .meta { font-size:12.5px; color:var(--muted); margin-bottom:8px; }
.card .excerpt { font-size:13.5px; opacity:.86; }
.badge.warn { border-color: rgba(180,120,20,.5); color: #9a6b12; background: rgba(230,180,80,.14); }
.badge { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11.5px;
  background:rgba(79,70,229,.14); color:var(--accent); margin:0 6px 0 0; }
.actions { margin:18px 0 6px; display:flex; gap:10px; flex-wrap:wrap; }
button { font:inherit; font-size:13px; padding:6px 12px; border-radius:8px; cursor:pointer;
  border:1px solid var(--line); background:transparent; color:var(--fg); }
button.primary { border-color:transparent; background:var(--accent); color:#fff; }
button[disabled] { opacity:.5; cursor:default; }
.cite a { font-size:11px; vertical-align:super; }
.sources { margin-top:26px; border-top:1px solid var(--line); padding-top:12px; font-size:13px; color:var(--muted); }
.sources li { margin:4px 0; }
.warn { color:#b45309; }
@media (prefers-color-scheme: dark) { .warn { color:#fbbf24; } }
"""


def _shell(title: str, body: str, prefix: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html_lib.escape(title)} · LLM Wiki</title><style>{_PAGE_CSS}</style></head>
<body><div class="nav"><a href="{prefix}/wiki/">LLM Wiki</a> · <a href="{prefix}/graph/">知识图谱</a>
 · <a href="{prefix}/">返回知识库</a></div>
{body}</body></html>"""


_GEN_JS = """
<script>
async function gen(slug, btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '生成中…（约 20-60 秒）';
  try {
    const C = window.__KB_PREFIX__ || '';
    const resp = await fetch(C + '/api/wiki/compile', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(slug ? {slug: slug} : {limit: 1}),
    });
    const data = await resp.json();
    if (!data.ok) { alert(data.error || '生成失败'); }
    else { location.reload(); }
  } catch (err) { alert('生成失败：' + err); }
  btn.disabled = false; btn.textContent = label;
}
</script>
"""


@router.get("/wiki/", response_class=HTMLResponse)
@router.get("/wiki", response_class=HTMLResponse)
def wiki_index_html(request: Request):
    prefix = _prefix(request)
    stats = kb_wiki.stats()
    listing = kb_wiki.article_list(limit=200)
    cards = []
    for item in listing["items"]:
        stale = item["state"] != "live"
        state = '<span class="badge warn">来源已变 · 建议重新生成</span>' if stale else ""
        kind = "主题" if item["kind"] == "topic" else "实体"
        excerpt = html_lib.escape((item["overview"] or "")[:160])
        slug = html_lib.escape(item["slug"])
        # P1-4：索引页只给「失败/过期」的文章留重新生成按钮，正常文章点进去再操作，
        # 避免 90+ 张卡片各挂一个高成本按钮。
        actions = f'<a class="card-link" href="{prefix}/wiki/{slug}">打开 →</a>'
        if stale:
            actions += f'<button onclick="gen(\'{slug}\', this)">重新生成</button>'
        cards.append(
            f'<div class="card"><h3><a href="{prefix}/wiki/{slug}">{html_lib.escape(item["title"])}</a></h3>'
            f'<div class="meta">{state}<span class="badge">{kind}</span>'
            f'{item["source_count"]} 篇来源 · {html_lib.escape((item["model"] or "")[:44])} · '
            f'{html_lib.escape(item["generated_at"][:10])}</div>'
            f'<div class="excerpt">{excerpt}…</div>'
            f'<div class="actions">{actions}</div></div>'
        )
    if not cards:
        cards.append(
            "<blockquote>还没有文章。点上方「生成一篇」会从覆盖报告最多的主题开始写第一篇；"
            "每篇约需 20-60 秒（调用 DSH 默认模型）。也可以等后台按增量慢慢补。</blockquote>"
        )
    ai_note = (
        " · 模型 " + html_lib.escape(stats.get("model") or "")
        if stats.get("mode") == "ai"
        else ' · <span class="warn">AI 未配置</span>'
    )
    head = (
        "<h1>LLM Wiki</h1>"
        f"<blockquote>{stats['articles']} 篇文章 · 来源 {stats['sources']} 条 · "
        f"可达标候选 {stats.get('candidates', 0)} 个{ai_note}</blockquote>"
        f'<div class="actions"><button class="primary" onclick="gen(\'\', this)">生成一篇</button>'
        f'<a href="{prefix}/graph/" style="align-self:center;font-size:13px">去知识图谱看节点与关系 →</a></div>'
        '<p style="font-size:13px;color:var(--muted)">文章由模型综合多篇来源报告写成，每段 [n] 对应页尾来源；'
        "来源内容变化时文章会被标记为过期，可重新生成。</p>"
    )
    prefix_js = f"<script>window.__KB_PREFIX__={prefix!r};</script>"
    return HTMLResponse(
        _shell("LLM Wiki", prefix_js + head + _GEN_JS + '<div class="cards">' + "".join(cards) + "</div>", prefix)
    )


@router.get("/wiki/{slug:path}", response_class=HTMLResponse)
def wiki_article_html(slug: str, request: Request):
    prefix = _prefix(request)
    article = kb_wiki.article_detail(slug)
    if not article:
        return HTMLResponse(
            _shell(
                "文章不存在",
                "<h1>文章不存在</h1><p>这篇文章还没有生成，或已被清理。</p>"
                f'<p><a href="{prefix}/wiki/">返回 LLM Wiki 索引</a> · '
                f'<a href="{prefix}/graph/">去知识图谱浏览节点</a></p>',
                prefix,
            ),
            status_code=404,
        )
    markdown = article["body_md"].lstrip()
    # 页面头部已经展示标题与元信息，正文里跳过重复的 `# 标题` 与 `> LLM Wiki ...` 元信息行
    lines = markdown.splitlines()
    while lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if lines and lines[0].startswith("> LLM Wiki"):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    body = render_markdown("\n".join(lines), prefix)
    body = re.sub(
        r"\[(\d+)\]",
        lambda m: f'<sup class="cite"><a href="#src-{m.group(1)}">[{m.group(1)}]</a></sup>',
        body,
    )
    # P2-3：给正文 H2 生成锚点 id，顺带抽出一份目录；长文再加顶部阅读进度条。
    toc_items: list[tuple[str, str]] = []

    def _anchor(match):
        sid = f"sec-{len(toc_items) + 1}"
        label = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        toc_items.append((sid, label))
        return f'<h2 id="{sid}">{match.group(1)}</h2>'

    body = re.sub(r"<h2>(.*?)</h2>", _anchor, body, flags=re.S)
    toc = ""
    if len(toc_items) >= 2:
        toc = (
            '<nav class="toc" aria-label="文章目录">'
            '<span class="toc-title">目录</span><ol>'
            + "".join(f'<li><a href="#{sid}">{html_lib.escape(label)}</a></li>' for sid, label in toc_items)
            + "</ol></nav>"
        )
    progress = (
        '<div class="readbar" aria-hidden="true"><i id="readbar-i"></i></div>'
        "<script>(function(){var bar=document.getElementById('readbar-i');if(!bar)return;"
        "function up(){var h=document.documentElement;var max=(h.scrollHeight-h.clientHeight)||1;"
        "var p=Math.min(1,Math.max(0,((window.scrollY||h.scrollTop||0))/max));"
        "bar.style.width=(p*100).toFixed(1)+'%';}"
        "window.addEventListener('scroll',up,{passive:true});window.addEventListener('resize',up);up();})();</script>"
    )
    sources = "".join(
        f'<li id="src-{src["n"]}">{src["n"]}. '
        f'<a href="{prefix}/{html_lib.escape(src["url"])}">{html_lib.escape(src["title"])}</a> '
        f'<span style="opacity:.7">（{html_lib.escape(src["path"])}）</span></li>'
        for src in article.get("sources") or []
    )
    state = (
        '<span class="warn">（来源已变化，建议重新生成）</span>'
        if article["state"] != "live"
        else ""
    )
    generated = html_lib.escape((article.get("generated_at") or "")[:16].replace("T", " "))
    header = (
        f'<h1>{html_lib.escape(article["title"])}</h1>'
        f'<div class="nav">{html_lib.escape(article["kind"])}「{html_lib.escape(article["subject"])}」 · '
        f'{article["source_count"]} 篇来源 · 模型 {html_lib.escape(article.get("model") or "")} · '
        f'{generated} {state}</div>'
        f'<div class="actions"><button onclick="gen(\'{html_lib.escape(article["slug"])}\', this)">重新生成</button></div>'
    )
    related = "".join(
        f'<li><a href="{prefix}/{html_lib.escape(item["url"])}">{html_lib.escape(item["title"])}</a>'
        f' <span style="opacity:.7">（共享 {item["shared"]} 篇来源）</span></li>'
        for item in article.get("related") or []
    )
    related_block = f'<div class="sources"><h2>相关文章</h2><ol style="padding-left:20px">{related}</ol></div>' if related else ""
    timeline = "".join(
        f'<li><span style="opacity:.7">{html_lib.escape(src["date"])}</span> · '
        f'<a href="{prefix}/{html_lib.escape(src["url"])}">{html_lib.escape(src["title"])}</a></li>'
        for src in article.get("timeline") or []
    )
    timeline_block = f'<div class="sources"><h2>时间线</h2><ol style="padding-left:20px">{timeline}</ol></div>' if timeline else ""
    footer = f'<div class="sources"><h2>来源</h2><ol style="padding-left:20px">{sources}</ol></div>'
    prefix_js = f"<script>window.__KB_PREFIX__={prefix!r};</script>"
    return HTMLResponse(
        _shell(
            article["title"],
            prefix_js + progress + header + toc + body + timeline_block + related_block + footer + _GEN_JS,
            prefix,
        )
    )
