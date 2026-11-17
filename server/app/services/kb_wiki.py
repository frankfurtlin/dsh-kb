"""知识图谱 + LLM Wiki：把 reports 的 AI 实体聚合成互相链接的 markdown 页。

设计（见 docs/GRAPH-WIKI.md）：
- reports/ + kb.sqlite 是事实源，wiki 是完全派生的展示层，可随时重建；
- 页表在 SQLite（wiki_page/wiki_link/wiki_source），正文同时写成
  `WIKI_DIR/pages/<slug>.md`，可直接用 Obsidian 打开浏览；
- 确定性生成（不调模型）：先用规则把实体聚合成页，保证离线可用、成本为零；
  后续版本再给高价值页加 AI 润色（generated_by/model 字段已预留）；
- 增量：按「报告内容 hash」判断页是否过期，hash 命中就跳过写盘；
- 删除：靠 wiki_source 反向引用定位需要重建的页。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from app import config
from app.services import kb_store

PAGES_DIRNAME = "pages"

# 不可作为页面的实体名（AI 抽取噪声）。
JUNK_NAMES = {
    "如此", "这些", "那些", "什么", "以及", "其他", "相关", "目前", "今天", "今年",
    "报告", "数据", "分析", "内容", "方法", "问题", "情况", "部分", "方面", "时候",
}

_SAFE_SLUG_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff._-]+")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_name(raw: str) -> str:
    """实体名规范化：折叠空白、去首尾标点。"""
    value = " ".join(str(raw or "").split())
    return value.strip(" \t\r\n|,，。；;:：\"'“”‘’()（）[]【】")


def slugify(name: str) -> str:
    """把实体名转成稳定 slug（保留中文，Obsidian 文件名友好）。"""
    value = normalize_name(name).lower().replace(" ", "-")
    value = _SAFE_SLUG_RE.sub("-", value).strip("-.")
    value = re.sub(r"-{2,}", "-", value)
    return value[:80] or "page"


def _alias_key(name: str) -> str:
    """别名归并键：忽略空白与大小写、忽略中英文空格差异。"""
    return re.sub(r"\s+", "", normalize_name(name)).lower()


def _is_junk(name: str) -> bool:
    return len(name) < 2 or name in JUNK_NAMES


def _entities_of(raw: str) -> list[dict]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    out = []
    for item in values:
        if not isinstance(item, dict):
            continue
        name = normalize_name(item.get("name", ""))
        if not name or _is_junk(name):
            continue
        out.append({"name": name, "type": normalize_name(item.get("type", "")) or "主题"})
    return out


def _page_dir() -> Path:
    return Path(config.WIKI_DIR) / PAGES_DIRNAME


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _hash(parts: list[str]) -> str:
    digest = hashlib.sha1()
    for part in sorted(parts):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


# ---------- 数据装载 ----------


def _load_reports(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT path, title, date, category, series, summary, summary_ai, tags, entities,"
        " content_hash FROM reports WHERE entities != '[]' ORDER BY date DESC, title ASC"
    ).fetchall()
    return [
        {
            "path": row["path"],
            "title": row["title"],
            "date": row["date"] or "",
            "category": row["category"],
            "series": row["series"] or "",
            "summary": (row["summary_ai"] or row["summary"] or "").strip(),
            "tags": kb_store._load_json(row["tags"], []),
            "entities": _entities_of(row["entities"]),
            "content_hash": row["content_hash"] or "",
        }
        for row in rows
    ]


def _canonicalize(reports: list[dict]) -> dict[str, dict]:
    """按别名键归并实体；展示名取「出现篇数最多」的拼写。"""
    groups: dict[str, dict] = {}
    for report in reports:
        for entity in report["entities"]:
            key = _alias_key(entity["name"])
            group = groups.setdefault(
                key,
                {"counts": {}, "types": {}, "reports": {}},
            )
            group["counts"][entity["name"]] = group["counts"].get(entity["name"], 0) + 1
            group["types"][entity["type"]] = group["types"].get(entity["type"], 0) + 1
            group["reports"][report["path"]] = report
    for key, group in groups.items():
        name = max(group["counts"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        entity_type = max(group["types"].items(), key=lambda kv: (kv[1], kv[0]))[0]
        group["name"] = name
        group["type"] = entity_type
        group["slug"] = slugify(name)
    return groups


# ---------- 页面渲染 ----------


def _render_page(title: str, entity_type: str, reports: list[dict], related: list[dict],
                 slug: str, updated_at: str) -> str:
    dates = sorted({r["date"] for r in reports if r["date"]})
    first, last = (dates[0], dates[-1]) if dates else ("", "")
    categories: dict[str, int] = {}
    for report in reports:
        categories[report["category"]] = categories.get(report["category"], 0) + 1
    top_categories = "、".join(
        name for name, _ in sorted(categories.items(), key=lambda kv: -kv[1])[:4]
    )
    lines = [
        "---",
        f"title: {title}",
        f"slug: {slug}",
        "kind: entity",
        f"entity_type: {entity_type}",
        f"report_count: {len(reports)}",
        f"updated_at: {updated_at}",
        "---",
        "",
        f"# {title}",
        "",
        f"> {entity_type} · {len(reports)} 篇报告提及"
        + (f" · 首次 {first} · 最近 {last}" if first else ""),
        "",
        "## 概述",
        "",
        f"「{title}」在知识库的 {len(reports)} 篇报告中被提及"
        + (f"，主要集中在 {top_categories}。" if top_categories else "。"),
        "",
        "## 相关报告",
        "",
    ]
    for report in reports[:40]:
        summary = report["summary"][:120].replace("\n", " ")
        lines.append(
            f"- {report['date']} [[report:{report['path']}|{report['title']}]]"
            + (f" — {summary}" if summary else "")
        )
    if len(reports) > 40:
        lines.append(f"- …另有 {len(reports) - 40} 篇")
    lines += ["", "## 相关实体", ""]
    if related:
        for item in related:
            lines.append(f"- [[{item['slug']}|{item['name']}]]（共同出现 {item['count']} 篇）")
    else:
        lines.append("- （暂无）")
    lines += ["", "## 标签", ""]
    tag_counts: dict[str, int] = {}
    for report in reports:
        for tag in report["tags"] or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    if tag_counts:
        top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:12]
        lines.append(" ".join(f"`#{tag}`({count})" for tag, count in top_tags))
    else:
        lines.append("- （暂无）")
    lines += ["", "## 来源", ""]
    for report in reports:
        lines.append(f"- reports/{report['path']}")
    lines.append("")
    return "\n".join(lines)


def _index_md(pages: list[dict], topics: list[dict] | None, updated_at: str) -> str:
    topics = topics or []
    lines = [
        "---",
        "title: 知识库 Wiki 索引",
        "kind: index",
        f"updated_at: {updated_at}",
        "---",
        "",
        "# 知识库 Wiki 索引",
        "",
        f"> 自动生成 · {len(pages)} 个实体页 · {len(topics)} 个主题页 · {updated_at}",
        "",
        "## 实体页（按提及报告数排序）",
        "",
    ]
    for page in pages:
        lines.append(f"- [[{page['slug']}|{page['title']}]]（{page['report_count']} 篇）")
    if topics:
        lines += ["", "## 主题页（按标签覆盖报告数排序）", ""]
        for topic in topics:
            lines.append(f"- [[{topic['slug']}|{topic['title']}]]（{topic['report_count']} 篇）")
    lines.append("")
    return "\n".join(lines)


_SCHEMA_MD = """# Wiki Schema

本目录由 dsh-kb 自动生成，事实源是 `reports/` 与 SQLite 索引库。

## 结构

- `pages/<slug>.md`：实体页（kind=entity），每个页 = 一个实体 + 引用它的报告。
- `index.md`：导航索引（按报告数排序）。
- `log.md`：编译日志（最近一次编译的统计）。
- `SCHEMA.md`：本文件。

## 约定

- 页内链接：`[[slug|显示名]]` 指向其它实体页；`[[report:<path>|标题]]` 指向原报告。
- 页面是**派生物**：不要只改这里，改完请重新跑 `POST /api/wiki/compile`。
- 编译是确定性的（不调模型），可以由 reports/ 随时重建。
"""


# ---------- P11：主题页（来自 AI 标签） ----------


def _topic_slug(name: str) -> str:
    return f"topic-{slugify(name)}"


def _topic_candidates(conn, entity_titles: set[str]) -> list[dict]:
    """从 tags/report_tags 里挑达到阈值的标签，生成主题页候选（含来源报告路径）。"""
    rows = conn.execute(
        """
        SELECT t.name AS name, r.path AS path, r.date AS date
        FROM report_tags rt
        JOIN tags t ON t.id = rt.tag_id
        JOIN reports r ON r.id = rt.report_id
        """
    ).fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        name = normalize_name(row["name"])
        if len(name) < 2 or name in JUNK_NAMES or name in entity_titles:
            continue
        item = grouped.setdefault(name, {"name": name, "paths": set(), "latest": ""})
        item["paths"].add(row["path"])
        if row["date"] and row["date"] > item["latest"]:
            item["latest"] = row["date"]
    topics = [
        {
            "name": item["name"],
            "slug": _topic_slug(item["name"]),
            "paths": sorted(item["paths"]),
            "latest": item["latest"],
        }
        for item in grouped.values()
        if len(item["paths"]) >= config.WIKI_TOPIC_MIN_REPORTS
    ]
    topics.sort(key=lambda t: (-len(t["paths"]), t["name"]))
    return topics


def _render_topic(topic: dict, reports_by_path: dict, related_entities: list[dict],
                  related_tags: list[tuple[str, int]], updated_at: str) -> str:
    categories = Counter(
        reports_by_path[path]["category"] for path in topic["paths"] if path in reports_by_path
    )
    lines = [
        "---",
        f"title: 主题 · {topic['name']}",
        f"slug: {topic['slug']}",
        "kind: topic",
        f"report_count: {len(topic['paths'])}",
        f"updated_at: {updated_at}",
        "---",
        "",
        f"# 主题 · {topic['name']}",
        "",
        f"> 标签 · {len(topic['paths'])} 篇报告 · 最近 {topic['latest'] or '—'}",
        "",
        "## 概述",
        "",
        f"「{topic['name']}」这个标签覆盖 {len(topic['paths'])} 篇报告，分布在"
        + "、".join(f"{name} {count} 篇" for name, count in categories.most_common(4)) + "。",
        "",
        "## 相关报告",
        "",
    ]
    for path in topic["paths"][:40]:
        report = reports_by_path.get(path) or {}
        lines.append(f"- {(report.get('date') or '')[:10]} [[report:{path}|{report.get('title') or path}]]")
    if related_entities:
        lines += ["", "## 主要实体", ""]
        for item in related_entities:
            lines.append(f"- [[{item['slug']}|{item['name']}]]（{item['count']} 篇）")
    if related_tags:
        lines += ["", "## 相关标签", ""]
        lines.append(" · ".join(f"#{name}（{count}）" for name, count in related_tags))
    lines.append("")
    return "\n".join(lines)


def _write_topic_pages(conn, topics: list[dict], reports_by_path: dict) -> tuple[int, int]:
    """写入主题页（hash 命中跳过）；返回 (written, skipped)。"""
    existing = {
        row["slug"]: row
        for row in conn.execute(
            "SELECT id, slug, source_hash, title, report_count, state FROM wiki_page WHERE kind='topic'"
        )
    }
    written = skipped = 0
    now = _now()
    page_dir = _page_dir()
    for topic in topics:
        paths = topic["paths"]
        source_hash = _hash(
            [f"{path}:{reports_by_path[path]['content_hash']}" for path in paths if path in reports_by_path]
        )
        row = existing.get(topic["slug"])
        if (row is not None and row["source_hash"] == source_hash
                and row["report_count"] == len(paths) and row["state"] == "live"):
            skipped += 1
            continue
        # 相关实体：主题覆盖报告里出现的实体页
        counts: Counter = Counter()
        for path in paths:
            report = reports_by_path.get(path) or {}
            for entity in report.get("entities") or []:
                key = _alias_key(entity["name"])
                counts[key] += 1
        related_entities = []
        for key, count in counts.most_common(10):
            entity_row = conn.execute(
                "SELECT slug, title FROM wiki_page WHERE kind='entity' AND state='live' AND lower(replace(title,' ','')) = ?",
                (key,),
            ).fetchone()
            if entity_row:
                related_entities.append({"slug": entity_row["slug"], "name": entity_row["title"], "count": count})
        # 相关标签：与主题共同出现的其它标签
        related_tags = []
        for row_tag in conn.execute(
            """
            SELECT t.name AS name, COUNT(*) AS n
            FROM report_tags rt
            JOIN tags t ON t.id = rt.tag_id
            JOIN reports r ON r.id = rt.report_id
            WHERE r.path IN (SELECT value FROM json_each(?))
              AND t.name != ?
            GROUP BY t.id ORDER BY n DESC LIMIT 8
            """,
            (json.dumps(paths), topic["name"]),
        ):
            related_tags.append((row_tag["name"], row_tag["n"]))
        body = _render_topic(topic, reports_by_path, related_entities, related_tags, now)
        conn.execute(
            """
            INSERT INTO wiki_page
                (slug, kind, title, entity_type, body_md, source_hash, state,
                 report_count, generated_by, model, created_at, updated_at)
            VALUES (?, 'topic', ?, '主题', ?, ?, 'live', ?, 'deterministic', '', ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                title=excluded.title, entity_type=excluded.entity_type, body_md=excluded.body_md,
                source_hash=excluded.source_hash, state='live',
                report_count=excluded.report_count, updated_at=excluded.updated_at
            """,
            (topic["slug"], f"主题 · {topic['name']}", body, source_hash, len(paths), now, now),
        )
        page_id = conn.execute("SELECT id FROM wiki_page WHERE slug=?", (topic["slug"],)).fetchone()[0]
        conn.execute("DELETE FROM wiki_link WHERE page_id=?", (page_id,))
        for item in related_entities:
            conn.execute(
                "INSERT OR IGNORE INTO wiki_link (page_id, target_slug) VALUES (?, ?)",
                (page_id, item["slug"]),
            )
        conn.execute("DELETE FROM wiki_source WHERE page_id=?", (page_id,))
        for path in paths:
            conn.execute(
                "INSERT OR IGNORE INTO wiki_source (page_id, path, contribution) VALUES (?, ?, 'topic')",
                (page_id, path),
            )
        conn.execute("DELETE FROM compile_job WHERE page_slug=?", (topic["slug"],))
        for path in paths:
            conn.execute(
                "INSERT OR REPLACE INTO compile_job (report_path, page_slug, status, attempts, updated_at)"
                " VALUES (?, ?, 'done', 0, ?)",
                (path, topic["slug"], now),
            )
        _write_text(page_dir / f"{topic['slug']}.md", body)
        written += 1
    return written, skipped


# ---------- 编译 ----------


def _related_for(group: dict, groups: dict[str, dict], reports_by_path: dict, limit: int = 8):
    """共现实体：与该实体在报告中共同出现次数最多的其它实体。"""
    counts: dict[str, int] = {}
    paths = set(group["reports"])
    for path in paths:
        for entity in reports_by_path[path]["entities"]:
            key = _alias_key(entity["name"])
            if key == _alias_key(group["name"]):
                continue
            counts[key] = counts.get(key, 0) + 1
    related = []
    for key, count in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]:
        other = groups.get(key)
        if other and other.get("slug"):
            related.append({"slug": other["slug"], "name": other["name"], "count": count})
    return related


def build(force: bool = False, limit_pages: int | None = None) -> dict:
    """全量重建 wiki 页（确定性）。hash 未变的页跳过写盘。"""
    started = time.time()
    conn = kb_store.connect()
    try:
        reports = _load_reports(conn)
        groups = _canonicalize(reports)
        reports_by_path = {r["path"]: r for r in reports}

        # 只保留达到阈值、且 slug 唯一的实体。
        candidates = [
            g for g in groups.values()
            if len(g["reports"]) >= config.WIKI_MIN_REPORTS and g.get("slug")
        ]
        candidates.sort(key=lambda g: -len(g["reports"]))
        max_pages = limit_pages or config.WIKI_MAX_PAGES
        candidates = candidates[:max_pages]

        by_slug: dict[str, dict] = {}
        for group in candidates:
            by_slug.setdefault(group["slug"], group)
        # 只有建了页的实体才允许出现在「相关实体」里（否则产生断链）。
        page_slugs = set(by_slug)

        # P11：主题页（AI 标签 → kind=topic），与实体页共享同一套页表。
        entity_titles = {group["name"] for group in by_slug.values()}
        topics = _topic_candidates(conn, entity_titles)
        topic_slugs = {topic["slug"] for topic in topics}

        existing = {
            row["slug"]: row
            for row in conn.execute(
                "SELECT id, slug, source_hash, title, report_count, state FROM wiki_page"
            )
        }

        written = skipped = 0
        now = _now()
        page_dir = _page_dir()
        page_dir.mkdir(parents=True, exist_ok=True)

        for slug, group in sorted(by_slug.items()):
            paths = sorted(group["reports"])
            source_hash = _hash(
                [f"{path}:{reports_by_path[path]['content_hash']}" for path in paths]
            )
            row = existing.get(slug)
            same = (
                row is not None
                and row["source_hash"] == source_hash
                and row["title"] == group["name"]
                and row["report_count"] == len(paths)
                and row["state"] == "live"
            )
            if same and not force:
                skipped += 1
                continue
            related = [
                item for item in _related_for(group, groups, reports_by_path)
                if item["slug"] in page_slugs and item["slug"] != slug
            ]
            title = group["name"]
            body = _render_page(title, group["type"], [reports_by_path[p] for p in paths],
                                related, slug, now)
            conn.execute(
                """
                INSERT INTO wiki_page
                    (slug, kind, title, entity_type, body_md, source_hash, state,
                     report_count, generated_by, model, created_at, updated_at)
                VALUES (?, 'entity', ?, ?, ?, ?, 'live', ?, 'deterministic', '', ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title=excluded.title, entity_type=excluded.entity_type,
                    body_md=excluded.body_md, source_hash=excluded.source_hash,
                    state='live', report_count=excluded.report_count,
                    generated_by=excluded.generated_by, updated_at=excluded.updated_at
                """,
                (slug, title, group["type"], body, source_hash, len(paths), now, now),
            )
            page_id = conn.execute("SELECT id FROM wiki_page WHERE slug=?", (slug,)).fetchone()[0]
            conn.execute("DELETE FROM wiki_link WHERE page_id=?", (page_id,))
            for item in related:
                conn.execute(
                    "INSERT OR IGNORE INTO wiki_link (page_id, target_slug) VALUES (?, ?)",
                    (page_id, item["slug"]),
                )
            conn.execute("DELETE FROM wiki_source WHERE page_id=?", (page_id,))
            for path in paths:
                conn.execute(
                    "INSERT OR IGNORE INTO wiki_source (page_id, path, contribution) VALUES (?, ?, 'entity')",
                    (page_id, path),
                )
            conn.execute(
                "DELETE FROM compile_job WHERE page_slug=?", (slug,)
            )
            for path in paths:
                conn.execute(
                    "INSERT OR REPLACE INTO compile_job (report_path, page_slug, status, attempts, updated_at)"
                    " VALUES (?, ?, 'done', 0, ?)",
                    (path, slug, now),
                )
            _write_text(page_dir / f"{slug}.md", body)
            written += 1

        topic_written, topic_skipped = _write_topic_pages(conn, topics, reports_by_path)
        written += topic_written
        skipped += topic_skipped

        # P11：实体共现边（多行关系）——同一篇报告里共同出现的实体页配对计数。
        slugs_by_path: dict[str, list[str]] = {}
        for slug, group in by_slug.items():
            for path in group["reports"]:
                slugs_by_path.setdefault(path, []).append(slug)
        pair_counts: Counter = Counter()
        for path_slugs in slugs_by_path.values():
            ordered = sorted(set(path_slugs))
            for index, left in enumerate(ordered):
                for right in ordered[index + 1:]:
                    pair_counts[(left, right)] += 1
        edge_count = kb_store.replace_entity_edges(
            ((left, right, weight) for (left, right), weight in pair_counts.items()),
            conn=conn,
        )

        # 清掉不再满足条件的页（也不满足阈值 / 实体消失）。
        keep = set(by_slug) | topic_slugs
        removed = 0
        for slug, row in existing.items():
            if slug in keep:
                continue
            conn.execute("DELETE FROM wiki_link WHERE page_id=?", (row["id"],))
            conn.execute("DELETE FROM wiki_source WHERE page_id=?", (row["id"],))
            conn.execute("DELETE FROM wiki_page WHERE id=?", (row["id"],))
            conn.execute("DELETE FROM compile_job WHERE page_slug=?", (slug,))
            stale_file = page_dir / f"{slug}.md"
            if stale_file.exists():
                try:
                    stale_file.unlink()
                except OSError:
                    pass
            removed += 1
        conn.commit()

        pages = [
            {"slug": slug, "title": group["name"], "report_count": len(group["reports"])}
            for slug, group in sorted(by_slug.items(), key=lambda kv: -len(kv[1]["reports"]))
        ]
        topic_list = [
            {"slug": topic["slug"], "title": f"主题 · {topic['name']}", "report_count": len(topic["paths"])}
            for topic in topics
        ]
        _write_text(Path(config.WIKI_DIR) / "index.md", _index_md(pages, topic_list, now))
        _write_text(Path(config.WIKI_DIR) / "SCHEMA.md", _SCHEMA_MD)
        _write_text(
            Path(config.WIKI_DIR) / "log.md",
            f"# 编译日志\n\n- {now} 全量构建：{written} 写 / {skipped} 跳过 / {removed} 删除，"
            f"{len(by_slug)} 个实体页 + {len(topics)} 个主题页 / {edge_count} 条共现边，"
            f"耗时 {int((time.time() - started) * 1000)}ms\n",
        )
        return {
            "pages": len(by_slug),
            "topics": len(topics),
            "edges": edge_count,
            "written": written,
            "skipped": skipped,
            "removed": removed,
            "entities": len(groups),
            "reports": len(reports),
            "duration_ms": int((time.time() - started) * 1000),
            "built_at": now,
        }
    finally:
        conn.close()


def after_sync(changed: list[str], removed: list[str]) -> dict:
    """sync 之后的增量编译钩子：先标 pending，再重建受影响的页。"""
    if not config.WIKI_AUTO:
        return {"skipped": True, "reason": "KB_WIKI_AUTO=0"}
    try:
        return build()
    except Exception as exc:  # 编译失败不能影响索引同步
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------- 查询 / 检查 ----------


def page_detail(slug: str) -> dict | None:
    conn = kb_store.connect()
    try:
        row = conn.execute("SELECT * FROM wiki_page WHERE slug=?", (slug,)).fetchone()
        if not row:
            return None
        links = [
            {"slug": link["target_slug"]}
            for link in conn.execute(
                "SELECT target_slug FROM wiki_link WHERE page_id=? ORDER BY target_slug",
                (row["id"],),
            )
        ]
        titles = {
            item["slug"]: conn.execute(
                "SELECT title FROM wiki_page WHERE slug=?", (item["slug"],)
            ).fetchone()
            for item in links
        }
        for item in links:
            found = titles.get(item["slug"])
            item["title"] = found[0] if found else item["slug"]
            item["exists"] = bool(found)
        sources = [
            {"path": src["path"]}
            for src in conn.execute(
                "SELECT path FROM wiki_source WHERE page_id=? ORDER BY path", (row["id"],)
            )
        ]
        report_rows = {
            item["path"]: conn.execute(
                "SELECT title, date, category FROM reports WHERE path=?", (item["path"],)
            ).fetchone()
            for item in sources
        }
        for item in sources:
            found = report_rows.get(item["path"])
            item["title"] = found["title"] if found else item["path"]
            item["date"] = (found["date"] or "") if found else ""
            item["category"] = found["category"] if found else ""
        return {
            "slug": row["slug"],
            "kind": row["kind"],
            "title": row["title"],
            "entity_type": row["entity_type"],
            "body_md": row["body_md"],
            "state": row["state"],
            "report_count": row["report_count"],
            "generated_by": row["generated_by"],
            "model": row["model"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "links": links,
            "sources": sources,
        }
    finally:
        conn.close()


def page_list(kind: str = "", state: str = "", q: str = "", limit: int = 50, offset: int = 0) -> dict:
    conn = kb_store.connect()
    try:
        where, params = ["1=1"], []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if state:
            where.append("state=?")
            params.append(state)
        if q:
            where.append("(title LIKE ? OR slug LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        clause = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM wiki_page WHERE {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT slug, title, kind, entity_type, state, report_count, updated_at"
            f" FROM wiki_page WHERE {clause} ORDER BY report_count DESC, title ASC"
            f" LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {
            "total": total,
            "items": [dict(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }
    finally:
        conn.close()


def stats() -> dict:
    conn = kb_store.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(report_count),0) FROM wiki_page"
        ).fetchone()
        states = {
            r["state"]: r["n"]
            for r in conn.execute("SELECT state, COUNT(*) AS n FROM wiki_page GROUP BY state")
        }
        links = conn.execute("SELECT COUNT(*) FROM wiki_link").fetchone()[0]
        sources = conn.execute("SELECT COUNT(*) FROM wiki_source").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
        topics = conn.execute("SELECT COUNT(*) FROM wiki_page WHERE kind='topic'").fetchone()[0]
        entities = conn.execute("SELECT COUNT(*) FROM wiki_page WHERE kind='entity'").fetchone()[0]
        return {
            "pages": row[0],
            "entities": entities,
            "topics": topics,
            "total_report_links": row[1],
            "states": states,
            "links": links,
            "edges": edges,
            "sources": sources,
            "min_reports": config.WIKI_MIN_REPORTS,
            "topic_min_reports": config.WIKI_TOPIC_MIN_REPORTS,
        }
    finally:
        conn.close()


def lint() -> dict:
    """只读体检：孤儿页、断链、过期页、高频无页实体。"""
    conn = kb_store.connect()
    try:
        orphans = [
            dict(row)
            for row in conn.execute(
                "SELECT p.slug, p.title FROM wiki_page p"
                " WHERE NOT EXISTS (SELECT 1 FROM wiki_source s WHERE s.page_id = p.id)"
                " ORDER BY p.slug LIMIT 50"
            )
        ]
        broken = [
            dict(row)
            for row in conn.execute(
                "SELECT p.slug AS page, p.title, l.target_slug AS target FROM wiki_link l"
                " JOIN wiki_page p ON p.id = l.page_id"
                " WHERE NOT EXISTS (SELECT 1 FROM wiki_page t WHERE t.slug = l.target_slug)"
                " ORDER BY p.slug LIMIT 50"
            )
        ]
        # 过期页：页的 source_hash 与来源报告当前 content_hash 不一致。
        stale = []
        for page in conn.execute(
            "SELECT p.id, p.slug, p.source_hash FROM wiki_page p LIMIT 500"
        ).fetchall():
            parts = [
                f"{row['path']}:{row['content_hash']}"
                for row in conn.execute(
                    "SELECT r.path, r.content_hash FROM wiki_source s JOIN reports r ON r.path = s.path"
                    " WHERE s.page_id=?",
                    (page["id"],),
                )
            ]
            if parts and _hash(parts) != page["source_hash"]:
                stale.append({"slug": page["slug"]})
        high = [
            dict(row)
            for row in conn.execute(
                "SELECT name, reports FROM tags WHERE reports >= 8 AND name NOT IN"
                " (SELECT title FROM wiki_page) ORDER BY reports DESC LIMIT 20"
            )
        ]
        return {
            "ok": True,
            "counts": {
                "orphan_pages": len(orphans),
                "broken_links": len(broken),
                "stale_pages": len(stale),
                "unpaged_tags": len(high),
            },
            "orphans": orphans,
            "broken_links": broken,
            "stale_pages": stale,
            "unpaged_tags": high,
        }
    finally:
        conn.close()
