"""剪藏：把一段粘贴文本里的分享链接抓成知识库报告。

设计要点：
- 入口只有「粘贴一段文本」；服务端自己从文本里抽 URL（最多 MAX_URLS 条），不要求用户
  先把链接单独挑出来。
- 分平台走已验证的现成脚本（Hermes 的 social-post-to-html 技能），通用网页用 trafilatura。
- 抓取脚本里有固定临时路径（/tmp/wx_article.html、~/zh_pick/），所以整个抓取流程
  串行执行（模块级锁），不做并发。
- 产出一篇自包含 HTML 直接投喂进索引库（2026-11-25 起不落 reports/）。文档以
  `source_url` 作为唯一键：同一条链接再剪藏 = 更新正文与 blob，不产生副本。
- 标题/来源/站点/抓取时间写在正文顶部的元信息行（出处字段在入库那一刻就落下）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from app.services import kb_blob, kb_store

# 抓取脚本来自 Hermes 的 social-post-to-html 技能：默认在家目录下，可用环境变量覆盖。
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
SKILL_SCRIPTS = Path(
    os.environ.get("KB_SKILL_SCRIPTS")
    or (HERMES_HOME / "skills" / "social-media" / "social-post-to-html" / "scripts")
)
CATEGORY = "剪藏"
MAX_URLS = 5
XHS_MAX_IMAGES = 4
FETCH_TIMEOUT = 150

_URL_RE = re.compile(r"https?://[^\s<>\"'（）()【】\[\]，。、；;！!？?]+")
_TRAILING = ".,;:!?)]}>'\"”’》」』"

_LOCK = threading.Lock()


# ---------------------------------------------------------------- URL 抽取


def extract_urls(text: str) -> list[str]:
    """从任意粘贴文本里抓出 URL，去重且保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.strip().rstrip(_TRAILING)
        if not url or url in seen:
            continue
        if len(url) > 2000:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= MAX_URLS:
            break
    return out


def site_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).lower() if m else ""


def platform_of(url: str) -> str:
    host = site_of(url)
    if host.endswith("mp.weixin.qq.com"):
        return "wechat"
    if host.endswith("zhihu.com") or host.endswith("zhihu.cn"):
        return "zhihu"
    if "xiaohongshu.com" in host or "xhslink.com" in host or "xhslink.cn" in host:
        return "xhs"
    return "generic"


# ---------------------------------------------------------------- 平台抓取


def _run(cmd: list[str], timeout: int = FETCH_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"抓取超时（>{timeout}s）"
    except FileNotFoundError as exc:
        return 127, "", f"脚本不存在：{exc}"


def _fetch_wechat(url: str, work: Path) -> dict[str, Any]:
    script = SKILL_SCRIPTS / "fetch_wechat.sh"
    if not script.exists():
        return {"ok": False, "error": "缺 fetch_wechat.sh"}
    page = work / "wechat.html"
    code, out, err = _run(["bash", str(script), url, str(page)])
    if code == 0 and ("js_content 未找到" in out or len(out.split("\n---\n", 1)[-1].strip()) < 40):
        time.sleep(2)  # 偶发验证墙，隔 2 秒重试一次
        code, out, err = _run(["bash", str(script), url, str(page)])
    if code == 2:
        return {"ok": False, "error": "被微信验证墙拦截（环境异常），稍后重试或换链接"}
    if code != 0:
        return {"ok": False, "error": (err or out or "抓取失败").strip()[:400]}

    title = account = ""
    body = out
    m = re.search(r"^标题:\s*(.+)$", out, re.M)
    if m:
        title = m.group(1).strip()
    m = re.search(r"^公众号:\s*(.+)$", out, re.M)
    if m:
        account = m.group(1).strip()
    if "\n---\n" in out:
        body = out.split("\n---\n", 1)[1]

    images: list[str] = []
    if page.exists():
        raw = page.read_text("utf-8", "ignore")
        images = list(dict.fromkeys(re.findall(r'data-src="(https?://mmbiz[^"]+)"', raw)))[:6]
        if not title:
            m = re.search(r'id="activity-name"[^>]*>(.*?)</', raw, re.S)
            if m:
                title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    return {"ok": True, "title": title, "author": account, "text": body.strip(), "images": images}


def _fetch_xhs(url: str, work: Path) -> dict[str, Any]:
    script = SKILL_SCRIPTS / "fetch_xhs.sh"
    if not script.exists():
        return {"ok": False, "error": "缺 fetch_xhs.sh"}
    before = {p.name for p in Path("/tmp/dsh-res").glob("xhs_*")} if Path("/tmp/dsh-res").is_dir() else set()
    code, out, err = _run(["bash", str(script), url])
    if code == 2:
        return {"ok": False, "error": "小红书页面为空（链接失效或被拦）"}

    dirs = sorted(Path("/tmp/dsh-res").glob("xhs_*")) if Path("/tmp/dsh-res").is_dir() else []
    fresh = [d for d in dirs if d.name not in before]
    if not fresh:
        return {"ok": False, "error": (err or "未找到抓取目录").strip()[:300]}
    run_dir = fresh[-1]

    title = desc = ""
    meta = run_dir / "meta.txt"
    if meta.exists():
        for line in meta.read_text("utf-8", "ignore").splitlines():
            if line.startswith("title="):
                title = line[6:].strip()
            elif line.startswith("desc="):
                desc = line[4:].strip()

    ocr_script = SKILL_SCRIPTS / "ocr_xhs.sh"
    text = desc
    if ocr_script.exists():
        # 只 OCR 前几张，避免长图文把请求拖到分钟级。
        shots = sorted(run_dir.glob("p*.png"))[:XHS_MAX_IMAGES]
        chunks: list[str] = []
        for shot in shots:
            code, out, _ = _run(["bash", str(ocr_script), str(run_dir)], timeout=90)
            if code == 0 and out.strip():
                chunks.append(out.strip())
            break  # ocr_xhs.sh 一次就把目录里所有 p*.png 跑完
        if chunks:
            text = (desc + "\n\n" if desc else "") + chunks[0]

    images = [str(p) for p in sorted(run_dir.glob("p*.png"))[:XHS_MAX_IMAGES]]
    return {"ok": True, "title": title or desc[:40], "author": "", "text": text.strip(), "images": [], "local_images": images}


def _fetch_zhihu(url: str, work: Path) -> dict[str, Any]:
    cookie = (getattr(config, "ZHIHU_COOKIE", "") or "").strip()
    if not cookie:
        return {"ok": False, "error": "知乎需要登录 cookie（未配置 ZHIHU_COOKIE），可直接粘贴答案正文"}
    script = SKILL_SCRIPTS / "fetch_zhihu.sh"
    if not script.exists():
        return {"ok": False, "error": "缺 fetch_zhihu.sh"}
    code, out, err = _run(["bash", str(script), url, cookie])
    # 抓取脚本把正文固定写到家目录下的 zh_pick/：运行时推导，不写死绝对路径。
    body_file = Path.home() / "zh_pick" / "body.txt"
    if code != 0 or not body_file.exists():
        return {"ok": False, "error": (err or out or "知乎抓取失败").strip()[:300]}
    return {"ok": True, "title": "", "author": "", "text": body_file.read_text("utf-8", "ignore").strip(), "images": []}


def _fetch_generic(url: str, work: Path) -> dict[str, Any]:
    try:
        import trafilatura

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {"ok": False, "error": "网页抓取失败（无内容）"}
        data = trafilatura.bare_extraction(
            downloaded, url=url, favor_recall=True, include_comments=False, include_tables=True
        )
        if data is None:
            return {"ok": False, "error": "正文抽取失败（页面可能是纯图片或需要登录）"}
        # trafilatura 2.x 返回 Document 对象，老版本返回 dict。
        get = data.get if isinstance(data, dict) else lambda k, d=None: getattr(data, k, d)
        text = (get("text") or get("raw_text") or "").strip()
        if not text:
            return {"ok": False, "error": "正文抽取为空"}
        return {
            "ok": True,
            "title": (get("title") or "").strip(),
            "author": (get("author") or "").strip(),
            "text": text,
            "images": [],
        }
    except Exception as exc:  # noqa: BLE001 - 抓取失败要原样回给前端
        return {"ok": False, "error": f"通用抓取失败：{type(exc).__name__}: {exc}"[:300]}


_FETCHERS = {
    "wechat": _fetch_wechat,
    "xhs": _fetch_xhs,
    "zhihu": _fetch_zhihu,
    "generic": _fetch_generic,
}

_PLATFORM_LABEL = {"wechat": "微信公众号", "xhs": "小红书", "zhihu": "知乎", "generic": "网页"}


# ---------------------------------------------------------------- HTML 生成


def _slugify(title: str, url: str) -> str:
    base = unicodedata.normalize("NFKD", title or "")
    keep = [ch for ch in base if not unicodedata.combining(ch)]
    base = "".join(keep).lower()
    base = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", base).strip("-")
    base = base[:40] or "clip"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


def _inline_markup(text: str) -> str:
    """把抓来的纯文本排成最简单的 HTML：标题行、列表行、图片占位、代码块。"""
    out: list[str] = []
    in_list = in_code = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(html_lib.escape(line))
            continue
        m = re.match(r"^\[IMAGE:\s*(https?://\S+?)\]$", stripped)
        if m:
            out.append(f'<figure><img src="{html_lib.escape(m.group(1))}" alt="" loading="lazy"></figure>')
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(4, len(m.group(1)) + 1)
            out.append(f"<h{level}>{html_lib.escape(m.group(2))}</h{level}>")
            continue
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html_lib.escape(stripped[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if stripped:
            out.append(f"<p>{html_lib.escape(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<!-- bl:category: {category} -->
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0 auto; padding: 32px 20px 72px; max-width: 760px;
         font: 16px/1.75 -apple-system, "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
         color: #1f2328; background: #fff; }}
  h1 {{ font-size: 1.6em; line-height: 1.35; margin: 0 0 12px; }}
  h2, h3, h4 {{ margin: 28px 0 10px; line-height: 1.4; }}
  p {{ margin: 12px 0; }}
  ul {{ margin: 12px 0; padding-left: 22px; }}
  li {{ margin: 4px 0; }}
  figure {{ margin: 18px 0; }}
  img {{ max-width: 100%; border-radius: 8px; display: block; }}
  pre {{ background: #f6f8fa; padding: 12px 14px; border-radius: 8px; overflow-x: auto; }}
  code {{ font: 0.92em/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .meta {{ margin: 0 0 20px; padding: 10px 14px; border-radius: 8px;
           background: #f6f8fa; color: #57606a; font-size: 0.88em; line-height: 1.7;
           word-break: break-all; }}
  .meta a {{ color: #0969da; text-decoration: none; }}
  .clipped {{ margin-top: 40px; padding-top: 14px; border-top: 1px solid #d8dee4;
              color: #8b949e; font-size: 0.82em; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6edf3; background: #0d1117; }}
    .meta {{ background: #161b22; color: #8b949e; }}
    pre {{ background: #161b22; }}
    .clipped {{ border-color: #30363d; }}
  }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">
  <div>来源：<a href="{url}" target="_blank" rel="noopener noreferrer">{site_label}</a>
       · {platform_label}{author_line}</div>
  <div>抓取时间：{fetched_at} · 原文地址：<a href="{url}" target="_blank" rel="noopener noreferrer">{url_short}</a></div>
</div>
{body}
<div class="clipped">剪藏于 {fetched_at}，正文由 dsh-kb 自动抓取；格式与图片可能和原文有差异，以原文为准。</div>
</body>
</html>
"""


def _render(item: dict[str, Any]) -> str:
    author = item.get("author") or ""
    return _PAGE.format(
        title=html_lib.escape(item.get("title") or "未命名剪藏"),
        category=CATEGORY,
        url=html_lib.escape(item["url"], quote=True),
        url_short=html_lib.escape(item["url"][:120]),
        site_label=html_lib.escape(site_of(item["url"])),
        platform_label=_PLATFORM_LABEL.get(item["platform"], "网页"),
        author_line=(f" · {html_lib.escape(author)}" if author else ""),
        fetched_at=item["fetched_at"],
        body=item["body_html"],
    )


# ---------------------------------------------------------------- 主流程


def _persist(item: dict[str, Any], *, body_text: str, html: str, when: dt.datetime) -> dict[str, Any]:
    """把一条剪藏写进索引库；同一来源链接重复剪藏走更新。"""
    data = html.encode("utf-8")
    slug = _slugify(item["title"], item["url"])
    blob = kb_blob.put(data, "text/html", f"clip-{slug}.html")
    published = when.strftime("%Y-%m-%d")
    existing = kb_store.find_document_by_source_url(item["url"])
    old_sha = (existing or {}).get("blob_sha") or ""
    if existing:
        doc = kb_store.update_document(
            existing["uid"],
            title=item["title"],
            body=body_text,
            blob_sha=blob["sha"],
            size=len(data),
            published_at=published,
            storage_tier=blob.get("tier", "local"),
            mime="text/html",
        )
        updated = True
    else:
        doc = kb_store.create_document(
            title=item["title"],
            body=body_text,
            mime="text/html",
            blob_sha=blob["sha"],
            size=len(data),
            published_at=published,
            source="clip",
            source_url=item["url"],
            category=CATEGORY,
            storage_tier=blob.get("tier", "local"),
        )
        updated = False
    if doc is None:  # 理论上不可达：更新前刚查到
        raise RuntimeError("剪藏入库失败：文档行不存在")
    # 更新会生成新 blob；旧 blob 没人引用就删掉，避免 COS 里堆垃圾。
    if old_sha and old_sha != blob["sha"] and kb_store.count_reports_by_blob(old_sha) == 0:
        kb_blob.drop(old_sha)
    return {
        "uid": doc["uid"],
        "path": doc["path"],
        "doc_url": doc["url"],
        "sha": blob["sha"],
        "updated": updated,
    }


def clip_text(text: str, *, save: bool = True) -> dict[str, Any]:
    """抓取粘贴文本里的链接；返回逐条结果（含入库文档的 uid 与打开链接）。"""
    urls = extract_urls(text)
    if not urls:
        return {"ok": False, "error": "没在文本里找到链接（需要以 http:// 或 https:// 开头）", "items": []}

    items: list[dict[str, Any]] = []
    written: list[str] = []
    with _LOCK:
        for url in urls:
            platform = platform_of(url)
            now = dt.datetime.now()
            work = Path("/tmp/dsh-kb-clip") / hashlib.sha1(url.encode()).hexdigest()[:10]
            work.mkdir(parents=True, exist_ok=True)
            try:
                result = _FETCHERS[platform](url, work)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它链接
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

            item: dict[str, Any] = {
                "url": url,
                "site": site_of(url),
                "platform": platform,
                "ok": bool(result.get("ok")),
                "title": (result.get("title") or "").strip(),
                "author": (result.get("author") or "").strip(),
                "error": result.get("error") or "",
                "path": "",
                "uid": "",
                "doc_url": "",
                "updated": False,
                "chars": 0,
            }
            if item["ok"]:
                body_text = (result.get("text") or "").strip()
                item["chars"] = len(body_text)
                if len(body_text) < 40:
                    item["ok"] = False
                    item["error"] = "正文太短：可能被平台验证墙拦住，或页面本身没有正文"
                else:
                    item["title"] = item["title"] or body_text.splitlines()[0][:60]
                    payload = dict(item, body_html=_inline_markup(body_text),
                                   fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"))
                    if save:
                        entry = _persist(item, body_text=body_text, html=_render(payload), when=now)
                        item["path"] = entry["path"]
                        item["uid"] = entry["uid"]
                        item["doc_url"] = entry["doc_url"]
                        item["updated"] = entry["updated"]
                        written.append(entry["path"])
            items.append(item)
            try:
                shutil.rmtree(work, ignore_errors=True)
            except OSError:
                pass

    ok_count = sum(1 for i in items if i["ok"])
    return {
        "ok": ok_count > 0,
        "imported": ok_count,
        "failed": len(items) - ok_count,
        "items": items,
        "error": "" if ok_count else (items[0].get("error") or "全部失败"),
    }
