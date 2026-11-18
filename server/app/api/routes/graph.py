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
        f'<p><a href="{_prefix(request)}/graph/canvas">打开可视化画布 →</a></p>'
        "<p class=\"muted\">节点与共现边由 <code>reports/</code> 的 AI 实体与标签确定性编译；"
        "重新编译见 <code>POST /api/graph/compile</code>。"
        "长文阅读与 AI 综述在「LLM Wiki」入口（下一阶段）。</p>"
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
  const COLORS = { entity: "#2563eb", topic: "#db2777" };

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
    meta.textContent = nodes.length + " 个节点 · " + edges.length + " 条边（共 " + data.total_nodes + " 个节点，可缩小窗口按提及数取前 N）";
    draw();
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

  function draw() {
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    ctx.fillStyle = getComputedStyle(document.body).backgroundColor || "#fff";
    ctx.fillRect(0, 0, rect.width, rect.height);
    const pos = {};
    nodes.forEach(function (n) { pos[n.slug] = toScreen(n); });
    const focus = selected || hover;
    edges.forEach(function (e) {
      const a = pos[e.source], b = pos[e.target]; if (!a || !b) return;
      const hot = focus && (e.source === focus.slug || e.target === focus.slug);
      ctx.strokeStyle = hot ? "rgba(37,99,235,.45)" : "rgba(120,120,140,.16)";
      ctx.lineWidth = hot ? 1.4 : Math.min(2, 0.5 + Math.log2(1 + (e.weight || 1)) * 0.25);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    });
    const drawnLabels = [];
    nodes.forEach(function (n) {
      const p = pos[n.slug], r = radius(n);
      const dim = focus && focus.slug !== n.slug && !(edges.some(function (e) { return (e.source === focus.slug && e.target === n.slug) || (e.target === focus.slug && e.source === n.slug); }));
      ctx.globalAlpha = dim ? 0.4 : 1;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fillStyle = COLORS[n.kind] || "#6b7280"; ctx.fill();
      if (focus && focus.slug === n.slug) { ctx.strokeStyle = "#111"; ctx.lineWidth = 2; ctx.stroke(); }
      const wantsLabel = (n.report_count || 0) >= 8 || (focus && focus.slug === n.slug);
      if (wantsLabel) {
        const label = (n.title || n.slug).slice(0, 14);
        ctx.font = "11px system-ui, sans-serif";
        const width = ctx.measureText(label).width;
        const box = { x: p.x + r + 3, y: p.y - 7, w: width, h: 14 };
        const clash = drawnLabels.some(function (b) {
          return box.x < b.x + b.w && b.x < box.x + box.w && box.y < b.y + b.h && b.y < box.y + box.h;
        });
        if (!clash || (focus && focus.slug === n.slug)) {
          ctx.fillStyle = "#4b5563";
          ctx.fillText(label, box.x, p.y + 3);
          drawnLabels.push(box);
        }
      }
      ctx.globalAlpha = 1;
    });
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
    if (n !== hover) { hover = n; canvas.style.cursor = n ? "pointer" : "grab"; draw(); }
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
    style = (
        "<style>"
        "#kb-canvas{width:100%;height:calc(100vh - 170px);min-height:420px;display:block;"
        "background:var(--panel);border:1px solid var(--border);border-radius:12px;cursor:grab}"
        ".kb-canvas-bar{display:flex;gap:10px;align-items:center;margin:10px 0 8px}"
        ".kb-canvas-bar input{flex:0 0 220px}"
        "#kb-panel{min-height:100px}"
        "</style>"
    )
    body = (
        "<h1>图谱画布</h1>"
        '<div class="nav">圆点=节点（蓝=实体，粉=主题），线=共现/互链关系；'
        '滚轮缩放、拖拽平移、点节点看详情。 '
        f'<a href="{prefix}/graph/">← 回到节点索引</a></div>'
        '<div class="kb-canvas-bar"><input id="kb-search" placeholder="过滤节点…">'
        '<button id="kb-refit">重新布局</button>'
        '<span id="kb-meta" style="font-size:12px;opacity:.7"></span></div>'
        '<canvas id="kb-canvas"></canvas>'
        '<div id="kb-panel"></div>'
        f"<script>window.__KB_PREFIX__={prefix!r};</script>"
        f"<script>{_CANVAS_JS}</script>"
    )
    return HTMLResponse(_shell("图谱画布", style + body, prefix))


@router.get("/graph/{slug:path}", response_class=HTMLResponse)
def graph_node_html(slug: str, request: Request):
    page = kb_graph.page_detail(slug)
    if not page:
        return HTMLResponse(_shell("未找到", "<h1>未找到该节点</h1>", _prefix(request)), status_code=404)
    body = render_markdown(page["body_md"], _prefix(request))
    return HTMLResponse(_shell(page["title"], body, _prefix(request)))
