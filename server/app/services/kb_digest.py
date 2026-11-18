"""P5：周报 / 主题聚合 —— 把最近 N 天的报告按主题、分类、标签聚成一页速览。

流程：
1. 从索引库取窗口内报告（默认 7 天）；
2. 统计分类/系列/标签/实体分布，挑出最近的报告作为素材；
3. 模型可用时，让它只依据这些素材写一份中文周报（主题 3-6 个，每个带报告路径）；
   模型不可用时退化为纯统计视图，接口不报错；
4. 结果按「窗口 + 当天」缓存在 KB_DATA_DIR/digest.json，refresh=true 时重算。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from app import config
from app.services import kb_ai, kb_store

logger = logging.getLogger("dsh-kb.digest")

_MAX_THEMES = 6
_MAX_SOURCE_REPORTS = 14
_PREVIEW_CHARS = 160

_SYSTEM = (
    "你是个人知识库的主编。根据用户提供的报告清单（标题/日期/分类/摘要），"
    "写一份中文周报：先给一段 total 概览（80-150 字），再归纳 3-6 个主题；"
    "每个主题给 name（不超过 12 字）、summary（60-120 字）、paths（只引用素材里出现过的报告路径，1-4 个）。"
    '只输出一个 JSON 对象：{"overview": "...", "themes": [{"name": "...", "summary": "...", "paths": ["..."]}]}，不要输出其他文字。'
)

_PATH_RE = re.compile(r"^[0-9A-Za-z_./-]+$")


def _cache_file() -> Path:
    return Path(config.DATA_DIR) / "digest.json"


def _load_cache(days: int) -> dict | None:
    path = _cache_file()
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if not cached.get("generated_at_ts"):
        # 旧缓存没有精确时间：退化为文件 mtime，UI 仍能显示"生成于 X 前"。
        try:
            cached["generated_at_ts"] = datetime.fromtimestamp(
                path.stat().st_mtime
            ).isoformat(timespec="seconds")
        except OSError:
            pass
    if int(cached.get("days") or 0) != days:
        return None
    if str(cached.get("generated_for") or "") != date.today().isoformat():
        return None
    return cached


def _save_cache(payload: dict) -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _aggregate(days: int) -> dict:
    days = max(1, min(90, int(days or 7)))
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    reports = [item for item in kb_store.recent_reports(since) if item.get("date")]
    categories = Counter(item.get("category") or "其他" for item in reports)
    series = Counter(item.get("series") or "" for item in reports if item.get("series"))
    tags: Counter[str] = Counter()
    entities: Counter[str] = Counter()
    for item in reports:
        for tag in item.get("tags") or []:
            if tag:
                tags[str(tag)] += 1
        for entity in item.get("entities") or []:
            name = entity.get("name") if isinstance(entity, dict) else entity
            if name:
                entities[str(name)] += 1
    sources = reports[:_MAX_SOURCE_REPORTS]
    return {
        "days": days,
        "since": since,
        "until": date.today().isoformat(),
        "total": len(reports),
        "ai_enriched": sum(1 for item in reports if item.get("summary_ai")),
        "categories": [{"name": name, "count": count} for name, count in categories.most_common()],
        "series": [{"name": name, "count": count} for name, count in series.most_common(10)],
        "tags": [{"name": name, "count": count} for name, count in tags.most_common(12)],
        "entities": [{"name": name, "count": count} for name, count in entities.most_common(12)],
        "reports": [
            {
                "title": item.get("title") or "",
                "path": item.get("path") or "",
                "date": item.get("date") or "",
                "category": item.get("category") or "",
                "series": item.get("series") or "",
                "summary": (item.get("summary_ai") or item.get("summary") or "")[:_PREVIEW_CHARS],
            }
            for item in sources
        ],
    }


def _compose_prompt(aggregate: dict) -> str:
    lines = [
        f"窗口：{aggregate['since']} 至 {aggregate['until']}，共 {aggregate['total']} 篇报告。",
        "分类分布：" + "、".join(f"{row['name']} {row['count']}" for row in aggregate["categories"][:8]),
    ]
    if aggregate["tags"]:
        lines.append("高频标签：" + "、".join(f"{row['name']}({row['count']})" for row in aggregate["tags"][:10]))
    lines.append("报告清单：")
    for index, item in enumerate(aggregate["reports"], start=1):
        lines.append(
            f"{index}. 《{item['title']}》 日期 {item['date']} 分类 {item['category']} "
            f"系列 {item['series'] or '-'} 路径 {item['path']}\n   摘要：{item['summary']}"
        )
    return "\n".join(lines)


def _parse_themes(payload: dict, reports: list[dict]) -> tuple[str, list[dict]]:
    overview = str(payload.get("overview") or "").strip()
    known = {item["path"]: item for item in reports}
    themes = []
    raw_themes = payload.get("themes")
    if isinstance(raw_themes, list):
        for raw in raw_themes[:_MAX_THEMES]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()[:24]
            summary = str(raw.get("summary") or "").strip()[:600]
            paths = []
            raw_paths = raw.get("paths")
            if isinstance(raw_paths, list):
                for candidate in raw_paths:
                    path = str(candidate or "").lstrip("/")
                    if path in known and _PATH_RE.match(path) and path not in paths:
                        paths.append(path)
            if name and (summary or paths):
                themes.append(
                    {
                        "name": name,
                        "summary": summary,
                        "reports": [
                            {"title": known[path]["title"], "path": path, "date": known[path]["date"]}
                            for path in paths
                        ],
                    }
                )
    return overview, themes


def build(days: int = 7, refresh: bool = False) -> dict:
    """返回窗口内的周报；结果带缓存，refresh 时重算。"""
    days = max(1, min(90, int(days or 7)))
    if not refresh:
        cached = _load_cache(days)
        if cached:
            # P2-4：缓存命中时补一个"窗口内当前篇数"，UI 才能提示"新增 N 篇"。
            since = (date.today() - timedelta(days=days - 1)).isoformat()
            total_now = len([item for item in kb_store.recent_reports(since) if item.get("date")])
            cached_total = int(cached.get("total") or 0)
            return {
                **cached,
                "cached": True,
                "total_now": total_now,
                "new_since": max(0, total_now - cached_total),
            }
    aggregate = _aggregate(days)
    if not aggregate["total"]:
        empty = {
            **aggregate,
            "title": "本周周报",
            "overview": "这个窗口里还没有报告。",
            "themes": [],
            "mode": "empty",
            "model": "",
            "generated_at": date.today().isoformat(),
            "generated_at_ts": datetime.now().isoformat(timespec="seconds"),
            "generated_for": date.today().isoformat(),
            "cached": False,
        }
        return empty
    overview = ""
    themes: list[dict] = []
    mode = "stats"
    model = ""
    if kb_ai.is_configured():
        try:
            payload = kb_ai.chat_json(_SYSTEM, _compose_prompt(aggregate), max_tokens=1600)
            overview, themes = _parse_themes(payload, aggregate["reports"])
            if themes:
                mode = "ai"
            model = kb_ai.model_label()
        except Exception as error:  # noqa: BLE001 - 周报失败时退化为统计视图
            logger.warning("周报生成失败，退化为统计视图：%s", error)
    if not themes:
        themes = [
            {
                "name": row["name"],
                "summary": f"{row['count']} 篇报告",
                "reports": [],
            }
            for row in aggregate["categories"][:_MAX_THEMES]
        ]
    digest = {
        **aggregate,
        "title": f"{aggregate['since']} → {aggregate['until']} 周报",
        "overview": overview or f"近 {days} 天新增 {aggregate['total']} 篇报告，其中 {aggregate['ai_enriched']} 篇已有 AI 摘要。",
        "themes": themes,
        "mode": mode,
        "model": model,
        "generated_at": date.today().isoformat(),
        "generated_at_ts": datetime.now().isoformat(timespec="seconds"),
        "total_now": aggregate["total"],
        "new_since": 0,
        "generated_for": date.today().isoformat(),
        "cached": False,
    }
    if mode == "ai":
        try:
            _save_cache(digest)
        except OSError as error:
            logger.warning("周报缓存写入失败：%s", error)
    return digest
