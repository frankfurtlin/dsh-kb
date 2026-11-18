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
from app.services import kb_graph, kb_store

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
:root {
  color-scheme: light dark;
  --bg: #f6f2e8; --bg-soft: #fbf8f1;
  --ink: #3d3a33; --ink-2: #6f6a5f; --ink-3: #9a948a;
  --gold: #a67c2e; --lav: #6b5fc4; --rose: #9c5f74; --mint: #2f8f74;
  --line: rgba(92, 88, 80, .14); --line-2: rgba(92, 88, 80, .28);
  --card: rgba(255, 253, 246, .78); --chip: rgba(255, 255, 255, .55);
  --shadow: 0 24px 60px rgba(61, 58, 51, .1);
  --serif: "Songti SC", "Noto Serif SC", "Source Han Serif SC", Georgia, serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d0e0e; --bg-soft: #141514;
    --ink: #ecece9; --ink-2: #a5a6a1; --ink-3: #75766f;
    --gold: #e6cf98; --lav: #c3bcf2; --rose: #e3b3c4; --mint: #ace0cf;
    --line: rgba(228, 228, 222, .11); --line-2: rgba(228, 228, 222, .24);
    --card: rgba(255, 255, 255, .035); --chip: rgba(255, 255, 255, .06);
    --shadow: 0 24px 60px rgba(0, 0, 0, .45);
  }
}
body {
  margin: 0; padding: 34px clamp(20px, 4vw, 56px) 72px; max-width: 1020px;
  font: 14px/1.7 -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: var(--ink); background:
    radial-gradient(1100px 560px at 12% -12%, rgba(201, 162, 74, .16), transparent 60%),
    radial-gradient(900px 520px at 108% 4%, rgba(107, 95, 196, .1), transparent 55%),
    var(--bg);
  -webkit-font-smoothing: antialiased;
}
.nav { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 0 0 28px;
  font-size: 11px; letter-spacing: .14em; text-transform: uppercase; color: var(--ink-3); }
.nav a { color: var(--ink-2); border: 0; }
.nav a:hover { color: var(--gold); }
.eyebrow { margin: 0 0 10px; font-size: 11px; letter-spacing: .22em; font-weight: 600;
  text-transform: uppercase; color: var(--gold); }
.eyebrow span { margin-left: 10px; letter-spacing: .1em; font-weight: 400;
  text-transform: none; color: var(--ink-2); }
h1 { margin: 0 0 10px; font: 600 clamp(26px, 3.4vw, 34px)/1.24 var(--serif); letter-spacing: .01em; }
.lead { margin: 0 0 20px; max-width: 58ch; font-size: 14px; color: var(--ink-2); }
.stats { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 24px; }
.stat { flex: 1 1 132px; padding: 14px 16px; border: 1px solid var(--line);
  border-radius: 16px; background: var(--card); backdrop-filter: blur(14px); }
.stat b { display: block; font: 600 24px/1.1 var(--serif); color: var(--ink);
  font-variant-numeric: tabular-nums; }
.stat span { display: block; margin-top: 6px; font-size: 11.5px; letter-spacing: .06em;
  text-transform: uppercase; color: var(--ink-3); }
.panel { margin: 0 0 18px; padding: 18px 22px; border: 1px solid var(--line); border-radius: 20px;
  background: var(--card); backdrop-filter: blur(14px); box-shadow: var(--shadow); }
.legend { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 0; font-size: 12.5px; color: var(--ink-2); }
.legend i { display: inline-block; width: 9px; height: 9px; margin-right: 7px; border-radius: 50%;
  vertical-align: 1px; }
.legend .line { display: inline-block; width: 18px; height: 2px; margin-right: 7px;
  vertical-align: 4px; border-radius: 2px; }
h2 { margin: 26px 0 10px; font: 600 16px/1.4 var(--serif); letter-spacing: .02em; }
ul { padding-left: 18px; margin: 0; columns: 2; column-gap: 34px; }
li { margin: 5px 0; break-inside: avoid; font-size: 13.5px; }
a { color: var(--ink); text-decoration: none; border-bottom: 1px solid var(--line-2); }
a:hover { border-bottom-color: var(--gold); }
blockquote { margin: 0; padding: 0; border: 0; background: none; color: var(--ink-2); font-size: 13.5px; }
code { padding: 1px 6px; border-radius: 6px; background: var(--chip); font-size: 12.5px; }
.muted { color: var(--ink-3); font-size: 12px; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 11px;
  border: 1px solid var(--line); border-radius: 999px; font-size: 10.5px;
  letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
.canvas-wrap { padding: 12px; background: rgba(255, 253, 246, .4); }
@media (prefers-color-scheme: dark) { .canvas-wrap { background: rgba(255, 255, 255, .03); } }
#kb-canvas { display: block; width: 100%; height: clamp(420px, 62vh, 720px);
  border-radius: 14px; background: transparent; cursor: grab; }
.canvas-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 0 0 12px; }
.canvas-bar input { flex: 0 1 240px; padding: 9px 14px; border: 1px solid var(--line-2);
  border-radius: 999px; background: var(--bg-soft); color: var(--ink);
  font: 13px/1.4 inherit; outline: none; }
.canvas-bar input:focus { border-color: var(--gold); }
.canvas-bar button { padding: 9px 16px; border: 1px solid var(--line-2); border-radius: 999px;
  background: transparent; color: var(--ink-2); font: 13px inherit; cursor: pointer;
  letter-spacing: .02em; }
.canvas-bar button:hover { border-color: var(--gold); color: var(--ink); }
#kb-meta { font-size: 12px; color: var(--ink-3); letter-spacing: .02em; }
#kb-panel { margin: 16px 0 0; min-height: 64px; font-size: 13.5px; color: var(--ink-2); }
#kb-panel h3 { margin: 0 0 6px; font: 600 17px/1.4 var(--serif); color: var(--ink); }
#kb-panel .meta { font-size: 12px; color: var(--ink-3); margin-bottom: 8px; }
@media (max-width: 760px) { ul { columns: 1; } body { padding: 24px 18px 56px; } }
/* 水母与光球：ambient layer（借鉴 betterlife neural 的质感与素材） */
.stage { position: fixed; inset: 0; z-index: 0; overflow: hidden; pointer-events: none; }
body > *:not(.stage) { position: relative; z-index: 1; }
.stage video.jelly { position: absolute; inset: -6%; width: 112%; height: 112%; object-fit: cover;
  filter: blur(26px) saturate(.9) brightness(1.04); opacity: .16; mix-blend-mode: soft-light; }
.stage .orbimg { position: absolute; inset: -4%; background: url("/static/media/orb.jpg") center/cover no-repeat;
  filter: blur(3px) saturate(.9) contrast(.96); opacity: .34; mix-blend-mode: multiply;
  animation: drift-b 40s ease-in-out infinite alternate; }
.stage .orb { position: absolute; width: 46vmax; height: 46vmax; border-radius: 50%;
  filter: blur(46px); opacity: .5; mix-blend-mode: screen; }
.stage .orb.gold { top: -16vmax; left: -10vmax;
  background: radial-gradient(circle at 38% 36%, rgba(255,246,214,.95), rgba(214,175,92,.35) 46%, transparent 68%);
  animation: drift-a 26s ease-in-out infinite alternate; }
.stage .orb.lav { right: -14vmax; bottom: -18vmax; width: 52vmax; height: 52vmax;
  background: radial-gradient(circle at 42% 40%, rgba(220,214,255,.9), rgba(120,104,205,.3) 48%, transparent 70%);
  animation: drift-b 32s ease-in-out infinite alternate; }
.stage .orb.rose { right: 12vmax; top: 8vmax; width: 30vmax; height: 30vmax;
  background: radial-gradient(circle at 45% 42%, rgba(255,224,232,.85), rgba(179,112,140,.22) 50%, transparent 72%);
  animation: drift-a 22s ease-in-out infinite alternate-reverse; }
@keyframes drift-a { from { transform: translate3d(0,0,0) scale(1); } to { transform: translate3d(4vmax,3vmax,0) scale(1.08); } }
@keyframes drift-b { from { transform: translate3d(0,0,0) scale(1.04); } to { transform: translate3d(-5vmax,-4vmax,0) scale(1); } }
@media (prefers-color-scheme: dark) {
  .stage video.jelly { mix-blend-mode: screen; opacity: .16; filter: blur(26px) saturate(1) brightness(.92); }
  .stage .orbimg { mix-blend-mode: screen; opacity: .16; filter: blur(3px) saturate(1.1) contrast(1.05); }
  .stage .orb { opacity: .42; }
}
@media (prefers-reduced-motion: reduce) { .stage .orb { animation: none; } }
"""



def _shell(title: str, body: str, prefix: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)} · 知识图谱</title><style>{_PAGE_CSS}</style>{'<style>body{max-width:none}</style>' if title == "图谱画布" else ''}</head>
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
        '<p class="eyebrow">Knowledge graph<span>知识图谱 · 确定性编译</span></p>'
        "<h1>每一篇报告，都在这里长成网。</h1>"
        '<p class="lead">节点来自报告的 AI 实体与标签，边来自共现与互链；没有模型参与也能重建、可追溯到来源报告。</p>'
        '<div class="stats">'
        f'<div class="stat"><b>{stats.get("entities", 0)}</b><span>实体节点</span></div>'
        f'<div class="stat"><b>{stats.get("topics", 0)}</b><span>主题节点</span></div>'
        f'<div class="stat"><b>{stats["links"]}</b><span>节点互链</span></div>'
        f'<div class="stat"><b>{stats.get("edges", 0)}</b><span>共现边</span></div>'
        "</div>"
        '<div class="panel"><p class="legend">'
        f'<span><i style="background:var(--lav)"></i>实体节点（{stats.get("entities", 0)}）</span>'
        f'<span><i style="background:var(--rose)"></i>主题节点（{stats.get("topics", 0)}）</span>'
        '<span><i class="line" style="background:rgba(166,124,46,.55)"></i>共现边</span>'
        '<span><i class="line" style="background:var(--line-2)"></i>互链边</span>'
        f'<span class="muted">{stats["sources"]} 条报告引用</span>'
        "</p></div>"
        f'<div class="panel"><p><a href="{_prefix(request)}/graph/canvas">打开可视化画布 →</a>'
        '<span class="pill" style="margin-left:10px">canvas · force layout</span></p></div>'
        f'<h2>实体节点（按提及报告数排序）</h2><div class="panel"><ul>{_list("entity")}</ul></div>'
        f'<h2>主题节点（按标签覆盖报告数排序）</h2><div class="panel"><ul>{_list("topic")}</ul></div>'
        '<p class="muted">节点与共现边由 <code>reports/</code> 的 AI 实体与标签确定性编译；'
        "重新编译见 <code>POST /api/graph/compile</code>。"
        "长文阅读与 AI 综述在「LLM Wiki」入口。</p>"
    )
    return HTMLResponse(_shell("知识图谱索引", body, _prefix(request)))


@router.get("/api/graph/graph")
def graph_data(limit: int = 140, min_weight: int = 0):
    return kb_store.graph_data(limit=min(max(limit, 20), 400), min_weight=max(0, min_weight))


_CANVAS_JS = r"""
(function () {
  const PREFIX = window.__KB_PREFIX__ || "";
  const canvas = document.getElementById("kb-canvas");
  const panel = document.getElementById("kb-panel");
  const meta = document.getElementById("kb-meta");
  const input = document.getElementById("kb-search");
  const ctx = canvas.getContext("2d");
  let data = { nodes: [], edges: [] };
  let nodes = [], edges = [], hover = null, selected = null;
  let view = { scale: 1, x: 0, y: 0 }, dragging = false, dragFrom = null, moved = false;
  function css(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  const COLORS = { entity: css("--lav", "#6b5fc4"), topic: css("--rose", "#9c5f74") };
  const INK = { dim: css("--ink-2", "#6f6a5f"), faint: css("--ink-3", "#9a948a"),
    paper: css("--bg-soft", "#fbf8f1"), gold: css("--gold", "#a67c2e") };
  const DARK = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const ORB = new Image();
  ORB.src = (window.__KB_PREFIX__ || "") + "/static/media/orb.jpg";
  function rgba(hex, a) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(String(hex).trim());
    if (!m) return "rgba(107,95,196," + a + ")";
    return "rgba(" + parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16) + "," + a + ")";
  }

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(320, Math.floor(rect.width * ratio));
    canvas.height = Math.max(320, Math.floor(rect.height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function layout() {
    const regex = (input.value || "").trim().toLowerCase();
    nodes = data.nodes
      .filter(function (n) { return !regex || (n.title || n.slug).toLowerCase().indexOf(regex) >= 0 || (n.entity_type || "").toLowerCase().indexOf(regex) >= 0; })
      .slice(0, 260);
    const keep = {};
    nodes.forEach(function (n) { keep[n.slug] = true; });
    edges = data.edges.filter(function (e) { return keep[e.source] && keep[e.target]; });
    const links = {};
    nodes.forEach(function (n) { n.x = Math.random() * 800; n.y = Math.random() * 600; n.vx = 0; n.vy = 0; links[n.slug] = []; });
    edges.forEach(function (e) {
      links[e.source].push({ other: e.target, w: Math.min(3, 1 + Math.log2(1 + (e.weight || 1)) * 0.4) });
      links[e.target].push({ other: e.source, w: Math.min(3, 1 + Math.log2(1 + (e.weight || 1)) * 0.4) });
    });
    const index = {};
    nodes.forEach(function (n) { index[n.slug] = n; });
    const K = 46, rep = 2800, iters = 320;
    for (let step = 0; step < iters; step++) {
      const cool = 1 - step / iters;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy + 0.01;
          if (d2 > 90000) continue;
          const force = rep / d2;
          dx *= force; dy *= force;
          a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy;
        }
      }
      nodes.forEach(function (n) {
        (links[n.slug] || []).forEach(function (l) {
          const o = index[l.other]; if (!o) return;
          const dx = o.x - n.x, dy = o.y - n.y;
          const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
          const force = (d - K) / d * 0.035 * l.w;
          n.vx += dx * force; n.vy += dy * force;
          o.vx -= dx * force; o.vy -= dy * force;
        });
        n.vx += (500 - n.x) * 0.0012; n.vy += (380 - n.y) * 0.0012;
        n.x += Math.max(-9, Math.min(9, n.vx)) * cool;
        n.y += Math.max(-9, Math.min(9, n.vy)) * cool;
        n.vx *= 0.82; n.vy *= 0.82;
      });
    }
    for (let pass = 0; pass < 70; pass++) {
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          const dx = b.x - a.x, dy = b.y - a.y;
          let d = Math.sqrt(dx * dx + dy * dy) || 0.01;
          const min = radius(a) + radius(b) + 14;
          if (d < min) {
            const push = (min - d) / d * 0.5;
            a.x -= dx * push; a.y -= dy * push;
            b.x += dx * push; b.y += dy * push;
          }
        }
      }
    }
    fit();
    meta.textContent = nodes.length + " 个节点 · " + edges.length + " 条边 · 全图 " + data.total_nodes + " 节点";
    fillLegend();
    draw();
  }

  function fillLegend() {
    const legend = document.getElementById("kb-legend");
    if (!legend) return;
    const entities = nodes.filter(function (n) { return n.kind === "entity"; }).length;
    const topics = nodes.filter(function (n) { return n.kind === "topic"; }).length;
    legend.innerHTML =
      '<span><i style="background:' + COLORS.entity + '"></i>实体节点 ' + entities + "</span>" +
      '<span><i style="background:' + COLORS.topic + '"></i>主题节点 ' + topics + "</span>" +
      '<span><i class="line" style="background:rgba(166,124,46,.5)"></i>共现 / 互链边 ' + edges.length + "</span>" +
      '<span class="muted">节点大小 ≈ 提及报告数</span>';
  }

  function fit() {
    if (!nodes.length) { view = { scale: 1, x: 0, y: 0 }; return; }
    let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
    nodes.forEach(function (n) { minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x); minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y); });
    const rect = canvas.getBoundingClientRect();
    const pad = 40;
    const scale = Math.min((rect.width - pad * 2) / Math.max(1, maxX - minX), (rect.height - pad * 2) / Math.max(1, maxY - minY));
    view.scale = Math.max(0.2, Math.min(2.2, scale));
    view.x = rect.width / 2 - ((minX + maxX) / 2) * view.scale;
    view.y = rect.height / 2 - ((minY + maxY) / 2) * view.scale;
  }

  function toScreen(n) { return { x: n.x * view.scale + view.x, y: n.y * view.scale + view.y }; }

  function radius(n) { return Math.max(3.5, Math.min(16, 3 + Math.log2(1 + (n.report_count || 1)) * 2.2)); }

  let hoverSince = 0, pulses = [], raf = 0;

  function ctrlPoint(a, b, e) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 1;
    const bow = Math.min(34, len * 0.14);
    const sign = (e.source + e.target).length % 2 === 0 ? 1 : -1;
    return { x: (a.x + b.x) / 2 - (dy / len) * bow * sign,
             y: (a.y + b.y) / 2 + (dx / len) * bow * sign };
  }

  function firePulse(slug) {
    pulses.push({ from: slug, t0: performance.now(), dur: 900 });
    kick();
  }

  function kick() {
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function tick() {
    raf = 0;
    const now = performance.now();
    pulses = pulses.filter(function (p) { return now - p.t0 < p.dur; });
    draw(now);
    if (pulses.length || hover) kick();
  }

  function draw(now) {
    const t = now || performance.now();
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    if (ORB.complete && ORB.naturalWidth) {
      // 光球纹理：低对比铺在节点后面（亮色 multiply / 暗色 screen）
      const scale = Math.max(rect.width / ORB.naturalWidth, rect.height / ORB.naturalHeight) * 1.25;
      const w = ORB.naturalWidth * scale, h = ORB.naturalHeight * scale;
      ctx.save();
      ctx.globalAlpha = DARK ? 0.16 : 0.11;
      ctx.globalCompositeOperation = DARK ? "screen" : "multiply";
      if ("filter" in ctx) ctx.filter = "blur(2px) saturate(.92)";
      ctx.drawImage(ORB, (rect.width - w) / 2, (rect.height - h) / 2, w, h);
      ctx.filter = "none";
      ctx.restore();
    }
    const pos = {};
    nodes.forEach(function (n) { pos[n.slug] = toScreen(n); });
    const focus = selected || hover;
    edges.forEach(function (e) {
      const a = pos[e.source], b = pos[e.target]; if (!a || !b) return;
      const hot = focus && (e.source === focus.slug || e.target === focus.slug);
      const cool = !focus || hot;
      ctx.strokeStyle = hot ? "rgba(166,124,46,.6)" : (cool ? "rgba(92,88,80,.22)" : "rgba(92,88,80,.08)");
      ctx.lineWidth = hot ? 1.6 : Math.min(2, 0.5 + Math.log2(1 + (e.weight || 1)) * 0.3);
      const cp = ctrlPoint(a, b, e);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(cp.x, cp.y, b.x, b.y);
      ctx.stroke();
    });
    // 脉冲：点击节点后，光芒沿触须向每个邻居扩散一次
    pulses.forEach(function (p) {
      const a = pos[p.from]; if (!a) return;
      const prog = Math.min(1, (t - p.t0) / p.dur);
      edges.forEach(function (e) {
        let target = null;
        if (e.source === p.from) target = e.target;
        else if (e.target === p.from) target = e.source;
        if (!target) return;
        const b = pos[target]; if (!b) return;
        const cp = ctrlPoint(a, b, e);
        const fade = 1 - prog;
        ctx.strokeStyle = "rgba(201,162,74," + (0.55 * fade) + ")";
        ctx.lineWidth = 1.8;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(cp.x, cp.y, b.x, b.y); ctx.stroke();
        const u = prog;
        const qx = (1 - u) * (1 - u) * a.x + 2 * (1 - u) * u * cp.x + u * u * b.x;
        const qy = (1 - u) * (1 - u) * a.y + 2 * (1 - u) * u * cp.y + u * u * b.y;
        ctx.fillStyle = "rgba(255,246,214," + fade + ")";
        ctx.beginPath(); ctx.arc(qx, qy, 2.6, 0, Math.PI * 2); ctx.fill();
      });
    });
    const drawnLabels = [];
    let labelBudget = 24;
    nodes.forEach(function (n) {
      const p = pos[n.slug], r = radius(n);
      const dim = focus && focus.slug !== n.slug && !(edges.some(function (e) { return (e.source === focus.slug && e.target === n.slug) || (e.target === focus.slug && e.source === n.slug); }));
      ctx.globalAlpha = dim ? 0.45 : 1;
      const focusThis = !!(focus && focus.slug === n.slug);
      const color = COLORS[n.kind] || "#6b7280";
      // 光球：外层柔光
      const halo = ctx.createRadialGradient(p.x, p.y, r * 0.6, p.x, p.y, r * 2.1);
      halo.addColorStop(0, rgba(color, focusThis ? 0.5 : 0.26));
      halo.addColorStop(1, rgba(color, 0));
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.1, 0, Math.PI * 2); ctx.fill();
      // 球体：左上高光 → 边缘半透明（水母伞盖的质感）
      const body = ctx.createRadialGradient(p.x - r * 0.34, p.y - r * 0.42, r * 0.12, p.x, p.y, r * 1.06);
      body.addColorStop(0, "#fffdf6");
      body.addColorStop(0.34, color);
      body.addColorStop(1, rgba(color, 0.88));
      ctx.fillStyle = body;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
      // 高光点
      ctx.save();
      ctx.globalAlpha = dim ? 0.25 : 0.65;
      ctx.fillStyle = "rgba(255,255,255,.85)";
      ctx.beginPath();
      ctx.ellipse(p.x - r * 0.3, p.y - r * 0.38, r * 0.3, r * 0.2, -0.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
      if (focusThis) {
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = INK.gold; ctx.lineWidth = 1.6; ctx.stroke();
      } else if ((n.report_count || 0) >= 30) {
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 3, 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(166,124,46,.35)"; ctx.lineWidth = 1; ctx.stroke();
      }
      const wantsLabel = (n.report_count || 0) >= 30 || focusThis;
      if (wantsLabel) {
        const label = (n.title || n.slug).slice(0, 14);
        ctx.font = "12px 'Songti SC', 'Noto Serif SC', Georgia, serif";
        const width = ctx.measureText(label).width;
        const box = { x: p.x + r + 4, y: p.y - 8, w: width, h: 16 };
        const clash = drawnLabels.some(function (b) {
          return box.x < b.x + b.w && b.x < box.x + box.w && box.y < b.y + b.h && b.y < box.y + box.h;
        });
        if (labelBudget > 0 && (!clash || focusThis)) {
          labelBudget -= 1;
          ctx.fillStyle = INK.dim;
          ctx.fillText(label, box.x, p.y + 4);
          drawnLabels.push(box);
        }
      }
      ctx.globalAlpha = 1;
    });
    // 水波纹：hover 节点外扩的同心圆
    if (hover) {
      const hp = pos[hover.slug];
      if (hp) {
        const r = radius(hover);
        const base = hoverSince || t;
        for (let i = 0; i < 3; i++) {
          const phase = ((t - base) / 1600 + i / 3) % 1;
          ctx.beginPath();
          ctx.arc(hp.x, hp.y, r + 4 + phase * 30, 0, Math.PI * 2);
          ctx.strokeStyle = rgba(COLORS[hover.kind] || "#6b5fc4", 0.38 * (1 - phase));
          ctx.lineWidth = 1.2;
          ctx.stroke();
        }
      }
    }
    if (selected) {
      const badge = nodes.length + " 个节点 · " + edges.length + " 条边";
      ctx.fillStyle = INK.faint;
      ctx.font = "11px system-ui, sans-serif";
      ctx.fillText(badge, 14, rect.height - 14);
    }
  }

  function pick(event) {
    const rect = canvas.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    let best = null, bestDist = 12;
    nodes.forEach(function (n) {
      const p = toScreen(n);
      const d = Math.hypot(p.x - mx, p.y - my);
      if (d < Math.max(bestDist, radius(n) + 4)) { best = n; bestDist = d; }
    });
    return best;
  }

  async function openNode(n) {
    selected = n;
    firePulse(n.slug);
    draw();
    const url = PREFIX + "/api/graph/node/" + encodeURIComponent(n.slug);
    const res = await fetch(url).then(function (r) { return r.json(); }).catch(function () { return null; });
    const page = (res && (res.page || res)) || {};
    const neighbours = (page.out_links || []).map(function (l) {
      return '<a href="' + PREFIX + "/api/graph/node/" + encodeURIComponent(l.slug) + '" data-slug="' + l.slug + '">' + (l.title || l.slug) + "</a>";
    }).join("、") || "（暂无）";
    panel.innerHTML =
      "<h3>" + (page.title || n.title) + "</h3>" +
      '<div class="meta">' + (n.kind === "topic" ? "主题" : "实体") + " · " + (n.entity_type || "") + " · " + n.report_count + " 篇报告提及</div>" +
      '<p><a href="' + PREFIX + "/graph/" + encodeURIComponent(n.slug) + '">打开节点详情页 →</a></p>' +
      "<p><b>相关节点</b>：" + neighbours + "</p>";
  }

  canvas.addEventListener("mousemove", function (event) {
    if (dragging) {
      moved = true;
      view.x += event.clientX - dragFrom.x; view.y += event.clientY - dragFrom.y;
      dragFrom = { x: event.clientX, y: event.clientY }; draw(); return;
    }
    const n = pick(event);
    if (n !== hover) {
      hover = n; hoverSince = performance.now();
      canvas.style.cursor = n ? "pointer" : "grab";
      kick();
    }
  });
  canvas.addEventListener("mousedown", function (event) { dragging = true; moved = false; dragFrom = { x: event.clientX, y: event.clientY }; canvas.style.cursor = "grabbing"; });
  window.addEventListener("mouseup", function () { dragging = false; canvas.style.cursor = hover ? "pointer" : "grab"; });
  canvas.addEventListener("click", function (event) { if (!moved) { const n = pick(event); if (n) openNode(n); } });
  canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    view.scale = Math.max(0.15, Math.min(3.5, view.scale * factor));
    view.x = mx - (mx - view.x) * factor; view.y = my - (my - view.y) * factor;
    draw();
  }, { passive: false });
  input.addEventListener("input", function () { layout(); });
  document.getElementById("kb-refit").addEventListener("click", function () { layout(); });
  window.addEventListener("resize", function () { resize(); draw(); });

  resize();
  fetch(PREFIX + "/api/graph/graph?limit=140").then(function (r) { return r.json(); }).then(function (payload) {
    data = payload; layout();
  });
})();
"""


@router.get("/graph/canvas", response_class=HTMLResponse)
def graph_canvas_html(request: Request):
    prefix = _prefix(request)
    body = (
        '<p class="eyebrow">Graph studio<span>可视化画布 · 本地力导向</span></p>'
        "<h1>看清楚，链接往哪里生长。</h1>"
        '<p class="lead">滚轮缩放、拖拽平移、输入过滤；点节点看详情与相关节点。'
        f'<a href="{prefix}/graph/">回到节点索引 →</a></p>'
        '<div class="panel canvas-wrap">'
        '<div class="canvas-bar"><input id="kb-search" placeholder="过滤节点…">'
        '<button id="kb-refit">重新布局</button>'
        '<span id="kb-meta"></span></div>'
        '<canvas id="kb-canvas"></canvas>'
        '<div id="kb-legend" class="legend"></div>'
        "</div>"
        '<div class="panel" id="kb-panel">点一个节点，这里显示它的类型、提及报告数与相关节点。</div>'
        f"<script>window.__KB_PREFIX__={prefix!r};</script>"
        f"<script>{_CANVAS_JS}</script>"
    )
    return HTMLResponse(_shell("图谱画布", body, prefix))


@router.get("/graph/{slug:path}", response_class=HTMLResponse)
def graph_node_html(slug: str, request: Request):
    page = kb_graph.page_detail(slug)
    if not page:
        return HTMLResponse(_shell("未找到", "<h1>未找到该节点</h1>", _prefix(request)), status_code=404)
    body = render_markdown(page["body_md"], _prefix(request))
    return HTMLResponse(_shell(page["title"], body, _prefix(request)))
