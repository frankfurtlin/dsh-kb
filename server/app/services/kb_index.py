"""扫描 `KB_REPORTS_DIR` 下的 HTML 报告，产出知识库索引所需的元数据。

与早期版本相比：
- 增加 `series`（系列/项目）推导：Minecraft 版本按大版本号归并、个股按股票归并、
  话题精读按来源归并，其余分类有各自的系列规则；
- 分类规则、日期目录规则保持不变；`url` 仍是相对路径（`reports/<rel>`）。
正文摘要与全文检索文本由 `kb_store` 在建立索引时抽取。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from app.config import REPORTS_DIR

# 报告目录里的资源/图片目录不参与列表；shares.html / index.html 是站点页不是报告。
_SKIP_DIRS = {"assets", "images"}
_SKIP_FILES = {"index.html", "shares.html"}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DATE_DIR_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})/")
_CATEGORY_RE = re.compile(r"<!--\s*bl:category:\s*([^>-]+?)\s*-->")
# 分类规则：顺序敏感，先命中先得。标题与相对路径都参与匹配（沿用原站规则）。
_CATEGORY_RULES = (
    ("Minecraft 版本", lambda title, rel: "mc-versions" in rel),
    ("市场全景", lambda title, rel: "市场全景" in title or "全景" in title),
    ("话题精读", lambda title, rel: any(k in title for k in ("精读", "转录", "点评", "心得", "整理", "解读"))),
    ("策略研究", lambda title, rel: any(k in title for k in ("策略", "回测", "量化"))),
    ("个股分析", lambda title, rel: any(k in title for k in ("投资分析", "持仓", "分析报告"))),
)

_MC_TOKEN_RE = re.compile(r"^v_(.+?)\.html?$", re.IGNORECASE)
_MC_SNAPSHOT_RE = re.compile(r"^(\d{2})w(\d{2})", re.IGNORECASE)
_STOCK_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z]+)\s*\((\d{5,6})(?:\.HK)?\)")

CACHE_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict[str, object] = {"at": 0.0, "items": []}


def _read_title(path: Path, fallback: str) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
    except OSError:
        return fallback
    match = _TITLE_RE.search(head)
    if not match:
        return fallback
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or fallback


def _read_category_override(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
    except OSError:
        return ""
    match = _CATEGORY_RE.search(head)
    if not match:
        return ""
    return match.group(1).strip()


def _category_for(title: str, rel: str) -> str:
    for name, matches in _CATEGORY_RULES:
        if matches(title, rel):
            return name
    return "其他"


def _date_for(rel: str) -> str:
    match = _DATE_DIR_RE.match(rel)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def _mc_series(rel: str, title: str) -> str:
    """Minecraft 版本归并：远古阶段 / Alpha / Beta / 快照 / x.y 大版本系列。"""
    name = rel.split("/")[-1]
    match = _MC_TOKEN_RE.match(name)
    token = match.group(1) if match else ""
    lowered = token.lower()
    if lowered.startswith("infdev"):
        return "远古版本 · Infdev"
    if lowered.startswith("indev"):
        return "远古版本 · Indev"
    if lowered.startswith("rd"):
        return "远古版本 · rd"
    if lowered.startswith("mc"):
        return "远古版本 · Survival Test"
    if lowered.startswith("alpha"):
        return "Alpha 阶段"
    if lowered.startswith("beta"):
        return "Beta 阶段"
    snapshot = _MC_SNAPSHOT_RE.match(token)
    if snapshot:
        return f"快照 {snapshot.group(1)} 年"
    numeric = token.replace("_", ".")
    major = re.match(r"^(\d+)\.(\d+)", numeric)
    if major:
        # 0.x 是 Classic 阶段、1.x 是正式版、26.x 是 2026 新版：
        # 按「大阶段」归并，左导航只留少量入口，具体版本用搜索/关键词定位。
        if major.group(1) == "0":
            return "Classic 0.x"
        if major.group(1) == "1":
            return "1.x 正式版"
        return f"{major.group(1)}.x 新版"
    return "其他版本"


def _series_for(title: str, rel: str, category: str) -> str:
    if category == "Minecraft 版本":
        return _mc_series(rel, title)
    if category == "市场全景":
        return "A股市场全景"
    if category == "个股分析":
        match = _STOCK_RE.search(title)
        if match:
            return f"{match.group(1)}({match.group(2)})"
        return "我的持仓" if "持仓" in title else "个股分析其他"
    if category == "话题精读":
        base = rel.split("/")[-1].lower()
        if base.startswith("xhs-"):
            return "小红书爆款笔记"
        if base.startswith(("wx-", "wechat-")):
            return "公众号长文"
        if base.startswith("yc-"):
            return "YC / 海外文章"
        if "airp" in base:
            return "AIRP 综述"
        return "其他精读"
    if category == "策略研究":
        return "量化策略"
    if category == "旅行影像":
        return "旅行影像册"
    return ""


def _iter_report_paths(root: Path):
    if not root.is_dir():
        return
    for path in root.rglob("*.html"):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if any(part in _SKIP_DIRS for part in parts[:-1]):
            continue
        if parts[-1] in _SKIP_FILES:
            continue
        yield path, rel


def scan() -> list[dict]:
    """全量扫描一次（不做缓存）；返回按日期/标题倒序排好的列表。"""
    items: list[dict] = []
    for path, rel in _iter_report_paths(REPORTS_DIR):
        try:
            stat = path.stat()
        except OSError:
            continue
        title = _read_title(path, fallback=rel)
        category = _read_category_override(path) or _category_for(title, rel)
        items.append(
            {
                "title": title,
                "category": category,
                "series": _series_for(title, rel, category),
                "date": _date_for(rel),
                "path": rel,
                "url": f"reports/{rel}",
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    items.sort(key=lambda item: (item["date"] or "", item["title"]), reverse=True)
    return items


def cached_scan(force: bool = False) -> list[dict]:
    """60 秒 TTL 的内存缓存；refresh=1 时强制重扫（cron 随时写新报告）。"""
    now = time.time()
    with _lock:
        if not force and now - float(_cache["at"]) < CACHE_TTL_SECONDS:
            return list(_cache["items"])  # type: ignore[arg-type]
    items = scan()
    with _lock:
        _cache["at"] = time.time()
        _cache["items"] = items
    return list(items)
