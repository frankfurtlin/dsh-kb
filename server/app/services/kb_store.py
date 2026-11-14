"""知识库索引库（SQLite）：报告元数据 + 摘要/正文全文 + 已读/收藏状态。

设计要点：
- `reports/` 目录始终只读；本模块只写 `KB_DATA_DIR/kb.sqlite`；
- 增量同步：按 (size, mtime) 判断变化，只有变化的报告才重新读盘、抽摘要/正文；
- 全文检索用 `LIKE`：报告量级（数百篇）下足够快，且对中文任意长度查询都有效
  （FTS5 的 trigram 不支持 1–2 字中文查询，unicode61 又无法切分中文）。
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from app.config import DATA_DIR, INDEX_FILE
from app.services import kb_index

_BODY_LIMIT = 20000
_SUMMARY_LIMIT = 180
_SNIPPET_LIMIT = 160

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    series TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0,
    summary_ai TEXT NOT NULL DEFAULT '',
    key_points TEXT NOT NULL DEFAULT '[]',
    entities TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    ai_version INTEGER NOT NULL DEFAULT 0,
    ai_at TEXT NOT NULL DEFAULT '',
    ai_error TEXT NOT NULL DEFAULT '',
    public INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
CREATE INDEX IF NOT EXISTS idx_reports_series ON reports(series);

-- 标签关系表：标签从「JSON 文本」升级为一等公民。
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    reports INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS report_tags (
    report_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'ai',
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (report_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_report_tags_tag ON report_tags(tag_id);
CREATE TABLE IF NOT EXISTS tag_aliases (
    alias TEXT PRIMARY KEY,
    canonical_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);
-- 变更轨迹：AI 重分类/改标签、人工覆盖、状态变更都留痕（append-only）。
CREATE TABLE IF NOT EXISTS report_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_report_events_path ON report_events(path, id);
"""

# 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里逐列 ALTER。
EMBED_COLUMNS = {
    "embedding": "BLOB",
    "embed_model": "TEXT NOT NULL DEFAULT ''",
    "embed_at": "TEXT NOT NULL DEFAULT ''",
}

PUBLIC_COLUMNS = {
    "public": "INTEGER NOT NULL DEFAULT 0",
}

# v0.3：三类时间戳 + AI 模型溯源（旧库自动补列，新行由写入路径填值）。
META_COLUMNS = {
    "created_at": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
    "read_at": "TEXT NOT NULL DEFAULT ''",
    "starred_at": "TEXT NOT NULL DEFAULT ''",
    "public_at": "TEXT NOT NULL DEFAULT ''",
    "ai_model": "TEXT NOT NULL DEFAULT ''",
}

AI_COLUMNS = {
    "summary_ai": "TEXT NOT NULL DEFAULT ''",
    "key_points": "TEXT NOT NULL DEFAULT '[]'",
    "entities": "TEXT NOT NULL DEFAULT '[]'",
    "tags": "TEXT NOT NULL DEFAULT '[]'",
    "ai_version": "INTEGER NOT NULL DEFAULT 0",
    "ai_at": "TEXT NOT NULL DEFAULT ''",
    "ai_error": "TEXT NOT NULL DEFAULT ''",
}

_write_lock = threading.Lock()
_indexed_at = 0.0


# ---------- 连接与建表 ----------


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # 排序/临时 B-tree 放内存，避免在受限环境下写临时文件失败。
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    _backfill_meta(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给旧索引库补齐 P1 新增的 AI 列（幂等）。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(reports)")}
    for column, definition in {**AI_COLUMNS, **EMBED_COLUMNS, **PUBLIC_COLUMNS, **META_COLUMNS}.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {column} {definition}")
    conn.commit()


# ---------- v0.3：标签、事件与时间戳 ----------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_tag(raw: str) -> str:
    """标签规范化：去 #、折叠空白、去掉控制字符，长度收敛到 48 字。"""
    value = str(raw or "").strip().lstrip("#").strip()
    value = " ".join(value.split())
    value = "".join(ch for ch in value if ch.isprintable())
    return value[:48]


def _normalize_tags(values) -> list[str]:
    """标签数组规范化 + 去重（保持首次出现顺序）。"""
    seen: list[str] = []
    for value in values or []:
        tag = normalize_tag(value)
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _tag_list(raw: str) -> list[str]:
    """把库里的 tags JSON 还原成去重后的规范化标签列表。"""
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return _normalize_tags(values)


def _record_event(conn: sqlite3.Connection, path: str, kind: str, payload: dict, model: str = "") -> None:
    conn.execute(
        "INSERT INTO report_events (path, kind, payload, model, created_at) VALUES (?, ?, ?, ?, ?)",
        (path, kind, json.dumps(payload, ensure_ascii=False)[:4000], model[:120], _now()),
    )


def _rebuild_report_tags(conn: sqlite3.Connection, path: str, raw_tags: str = "", source: str = "ai") -> None:
    """把一篇报告的标签 JSON 落到 tags/report_tags（先删后建，幂等）。"""
    row = conn.execute("SELECT id, tags FROM reports WHERE path = ?", (path,)).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM report_tags WHERE report_id = ?", (row["id"],))
    tags = _tag_list(raw_tags if raw_tags else row["tags"])
    now = _now()
    for tag in tags:
        conn.execute(
            "INSERT INTO tags (name, created_at, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at",
            (tag, now, now),
        )
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO report_tags (report_id, tag_id, source, created_at) VALUES (?, ?, ?, ?)",
            (row["id"], tag_id, source, now),
        )


def _refresh_tag_counts(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE tags SET reports = (SELECT COUNT(*) FROM report_tags WHERE report_tags.tag_id = tags.id)"
    )
    conn.execute("DELETE FROM tags WHERE reports = 0")


def _backfill_meta(conn: sqlite3.Connection) -> None:
    """给旧库补 v0.3 的时间戳与标签关系（幂等，只在确实缺数据时跑）。

    已有行没有「入库时间」，只能用文件 mtime 近似（写进 created_at）；新行由 sync()
    写入真实入库时间。标签关系从各行的 tags JSON 重建一次。
    """
    missing_time = conn.execute(
        "SELECT COUNT(*) AS n FROM reports WHERE created_at = ''"
    ).fetchone()["n"]
    if missing_time:
        conn.execute(
            "UPDATE reports SET created_at = strftime('%Y-%m-%dT%H:%M:%S', mtime, 'unixepoch', 'localtime')"
            " WHERE created_at = '' AND mtime > 0"
        )
        conn.execute("UPDATE reports SET created_at = ? WHERE created_at = ''", (_now(),))
    conn.execute("UPDATE reports SET updated_at = created_at WHERE updated_at = ''")
    links = conn.execute("SELECT COUNT(*) AS n FROM report_tags").fetchone()["n"]
    tagged = conn.execute(
        "SELECT COUNT(*) AS n FROM reports WHERE tags != '' AND tags != '[]'"
    ).fetchone()["n"]
    if not links and tagged:
        for row in conn.execute("SELECT path FROM reports").fetchall():
            _rebuild_report_tags(conn, row["path"])
        _refresh_tag_counts(conn)
    conn.commit()


# ---------- 正文抽取 ----------


def _extract_text(path: Path) -> tuple[str, str]:
    """返回 (summary, body)：summary 优先取第一个有内容的 <p>，body 为全文纯文本。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    paragraphs = []
    for match in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", raw):
        text = re.sub(r"(?s)<[^>]+>", " ", match.group(1))
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        if len(text) >= 40:
            paragraphs.append(text)
        if len(paragraphs) >= 3:
            break
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    body = re.sub(r"(?s)<!--.*?-->", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(re.sub(r"\s+", " ", body)).strip()
    summary = paragraphs[0] if paragraphs else body[:_SUMMARY_LIMIT]
    return summary[:_SUMMARY_LIMIT], body[:_BODY_LIMIT]


# ---------- 同步 ----------


def sync(force: bool = False) -> dict:
    """把扫描结果增量同步进索引库；返回 {indexed, removed, total}。"""
    global _indexed_at
    items = kb_index.cached_scan(force=force)
    incoming = {item["path"]: item for item in items}
    indexed = removed = 0
    with _write_lock:
        conn = _connect()
        try:
            existing = {
                row["path"]: row
                for row in conn.execute("SELECT path, size, mtime, title, category, series, date FROM reports")
            }
            changed: list[str] = []
            now = _now()
            for path, item in incoming.items():
                stat_key = (int(item["size"]), int(item["mtime"]))
                row = existing.get(path)
                if row is not None and (row["size"], row["mtime"]) == stat_key and (
                    row["title"],
                    row["category"],
                    row["series"],
                    row["date"],
                ) == (item["title"], item["category"], item["series"], item["date"]):
                    continue
                summary, body = _extract_text(kb_index.REPORTS_DIR / path)
                cursor = conn.execute(
                    """
                    INSERT INTO reports
                        (path, title, category, series, date, size, mtime, summary, body,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        title=excluded.title, category=excluded.category, series=excluded.series,
                        date=excluded.date, size=excluded.size, mtime=excluded.mtime,
                        summary=excluded.summary, body=excluded.body,
                        updated_at=excluded.updated_at,
                        -- 正文变化后旧的 AI sidecar 立即失效，等待重新抽取。
                        summary_ai='', key_points='[]', entities='[]', tags='[]',
                        ai_version=0, ai_at='', ai_error=''
                    """,
                    (
                        path,
                        item["title"],
                        item["category"],
                        item["series"],
                        item["date"],
                        stat_key[0],
                        stat_key[1],
                        summary,
                        body,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount:
                    indexed += 1
                    changed.append(path)
                    if row is None:
                        _record_event(
                            conn,
                            path,
                            "ingest",
                            {
                                "title": item["title"],
                                "category": item["category"],
                                "series": item["series"],
                                "date": item["date"],
                            },
                        )
                    elif (row["category"], row["series"]) != (item["category"], item["series"]):
                        _record_event(
                            conn,
                            path,
                            "reclassify",
                            {
                                "from": {"category": row["category"], "series": row["series"]},
                                "to": {"category": item["category"], "series": item["series"]},
                                "source": "rule",
                            },
                        )
            removed_paths: list[str] = []
            for path in existing.keys() - incoming.keys():
                _record_event(conn, path, "remove", {"reason": "file-missing"})
                conn.execute("DELETE FROM reports WHERE path = ?", (path,))
                removed_paths.append(path)
                removed += 1
            # 有标签的报告重建标签关系；被删除的报告由计数刷新兜底清掉。
            for path in changed:
                _rebuild_report_tags(conn, path)
            if changed or removed_paths:
                _refresh_tag_counts(conn)
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        finally:
            conn.close()
    _indexed_at = time.time()
    return {
        "indexed": indexed,
        "removed": removed,
        "total": total,
        "scan_total": len(items),
        "synced_at": _now(),
    }


# ---------- 查询 ----------


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _load_json(raw: str, fallback):
    """把库里存的 JSON 文本还原成 Python 值；坏数据不影响列表接口。"""
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _row_to_item(row: sqlite3.Row) -> dict:
    return {
        "title": row["title"],
        "category": row["category"],
        "series": row["series"],
        "date": row["date"],
        "path": row["path"],
        "url": f"reports/{row['path']}",
        "size": row["size"],
        "mtime": row["mtime"],
        "summary": row["summary"],
        "read": bool(row["read"]),
        "starred": bool(row["starred"]),
        # P1 AI sidecar（未生成时为空；前端据此决定是否展示）。
        "summary_ai": row["summary_ai"] or "",
        "key_points": _load_json(row["key_points"], []),
        "entities": _load_json(row["entities"], []),
        "tags": _load_json(row["tags"], []),
        "ai_version": row["ai_version"] or 0,
        "ai_at": row["ai_at"] or "",
        "ai_error": row["ai_error"] or "",
        "ai_model": row["ai_model"] or "",
        # P2：是否已有语义向量（前端可据此显示「语义可检索」状态）。
        "has_embedding": bool(row["embedding"]),
        # P6：是否已公开（可被匿名只读访问）。
        "public": bool(row["public"]),
        # v0.3：时间线（入库/索引/操作），用于「最近上传/最近读过」与审计。
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "read_at": row["read_at"] or "",
        "starred_at": row["starred_at"] or "",
        "public_at": row["public_at"] or "",
    }


def _norm_date(value: str) -> str:
    """把 `20260101` / `2026.01.01` / `2026-01-01` 统一成索引里存的 `2026-01-01`。

    索引日期列是 ISO 形态（目录名 20260101 → 2026-01-01），字符串比较才有序；
    调用方可能传 8 位（老链接、脚本）或 ISO（日期输入框），这里统一收口。
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) < 8:
        return ""
    d = digits[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def list_reports(
    q: str = "",
    category: str = "",
    series: str = "",
    tag: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    sort: str = "date_desc",
    offset: int = 0,
    limit: int = 0,
) -> tuple[list[dict], int]:
    keyword = (q or "").strip()
    where: list[str] = []
    params: list[object] = []
    if keyword:
        where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
        params.extend([_like(keyword), _like(keyword)])
    if category:
        where.append("category = ?")
        params.append(category)
    if series:
        where.append("series = ?")
        params.append(series)
    tag = normalize_tag(tag)
    if tag:
        where.append(
            "id IN (SELECT report_tags.report_id FROM report_tags"
            " JOIN tags ON tags.id = report_tags.tag_id WHERE tags.name = ?)"
        )
        params.append(tag)
    date_from = _norm_date(date_from)
    date_to = _norm_date(date_to)
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)
    if view == "unread":
        where.append("read = 0")
    elif view == "starred":
        where.append("starred = 1")
    elif view == "public":
        where.append("public = 1")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    order = {
        "date_asc": "CASE WHEN date = '' THEN 1 ELSE 0 END, date ASC, title ASC",
        "title": "title ASC",
        "updated": "mtime DESC, title ASC",
    }.get(sort, "CASE WHEN date = '' THEN 1 ELSE 0 END, date DESC, title ASC")
    conn = _connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM reports{clause}", params).fetchone()[0]
        sql = f"SELECT * FROM reports{clause} ORDER BY {order}"
        if limit and limit > 0:
            sql += " LIMIT ? OFFSET ?"
            rows = conn.execute(sql, [*params, int(limit), int(offset)]).fetchall()
        else:
            rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = _row_to_item(row)
        if keyword:
            item.update(_snippet_for(row, keyword))
        items.append(item)
    return items, int(total)


def recent_reports(since_date: str = "", limit: int = 0) -> list[dict]:
    """按日期倒序取报告（供 P5 周报聚合用）；since_date 形如 2026-11-01，空表示不限。"""
    conn = _connect()
    try:
        if since_date:
            rows = conn.execute(
                "SELECT * FROM reports WHERE date >= ? ORDER BY date DESC, title ASC",
                (since_date,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reports ORDER BY date DESC, title ASC").fetchall()
    finally:
        conn.close()
    items = [_row_to_item(row) for row in rows]
    return items[:limit] if limit and limit > 0 else items


def _filter_clause(
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    *,
    include_dates: bool = True,
    tag: str = "",
) -> tuple[str, list[object]]:
    """把列表筛选条件拼成 WHERE，供 tree() 统计「当前筛选下的」计数。"""
    keyword = (q or "").strip()
    where: list[str] = []
    params: list[object] = []
    if keyword:
        where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
        params.extend([_like(keyword), _like(keyword)])
    tag = normalize_tag(tag)
    if tag:
        where.append(
            "id IN (SELECT report_tags.report_id FROM report_tags"
            " JOIN tags ON tags.id = report_tags.tag_id WHERE tags.name = ?)"
        )
        params.append(tag)
    if include_dates:
        df, dt = _norm_date(date_from), _norm_date(date_to)
        if df:
            where.append("date >= ?")
            params.append(df)
        if dt:
            where.append("date <= ?")
            params.append(dt)
    if view == "unread":
        where.append("read = 0")
    elif view == "starred":
        where.append("starred = 1")
    elif view == "public":
        where.append("public = 1")
    return (" AND ".join(where) or "1 = 1"), params


def tree(
    q: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    tag: str = "",
) -> dict:
    """层次导航树。

    category/date/q/view 传入时，分类计数反映当前筛选（老站行为：搜索或选日期后
    分类数字跟着收敛）；series 只统计当前选中分类；时间列表按 q/view 过滤但不按
    日期过滤，否则选完某天就看不到其它日期了。
    """
    clause, params = _filter_clause(q, date_from, date_to, view, tag=tag)
    date_clause, date_params = _filter_clause(
        q, date_from, date_to, view, include_dates=False, tag=tag
    )
    conn = _connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM reports WHERE {clause}", params).fetchone()["n"]
        read_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM reports WHERE {clause} AND read = 1", params
        ).fetchone()["n"]
        counts = {
            key: conn.execute(f"SELECT COUNT(*) AS n FROM reports WHERE {clause} AND {key} = 1", params).fetchone()["n"]
            for key in ("read", "starred", "public")
        }
        categories = [
            {"name": row["name"], "count": row["count"]}
            for row in conn.execute(
                f"SELECT category AS name, COUNT(*) AS count FROM reports WHERE {clause} "
                "GROUP BY category ORDER BY count DESC",
                params,
            )
        ]
        series_clause = clause + (" AND category = ?" if category else "")
        series_params = params + ([category] if category else [])
        series = [
            {"category": row["category"], "name": row["series"], "count": row["count"]}
            for row in conn.execute(
                f"SELECT category, series, COUNT(*) AS count FROM reports WHERE {series_clause} AND series != '' "
                "GROUP BY category, series ORDER BY category, count DESC",
                series_params,
            )
        ]
        dates = [
            {"date": row["date"], "count": row["count"]}
            for row in conn.execute(
                f"SELECT date, COUNT(*) AS count FROM reports WHERE {date_clause} AND date != '' "
                "GROUP BY date ORDER BY date DESC LIMIT 200",
                date_params,
            )
        ]
    finally:
        conn.close()
    return {
        "total": int(total),
        "unread": int(total) - int(read_count),
        "starred": int(counts["starred"]),
        "public": int(counts["public"]),
        "categories": categories,
        "series": series,
        "dates": dates,
        # 标签导航（全局 top N，不受当前筛选影响，方便随时换标签）。
        "tags": tags_overview(limit=15),
    }


def search(q: str = "", offset: int = 0, limit: int = 20) -> tuple[list[dict], int]:
    keyword = (q or "").strip()
    if not keyword:
        return [], 0
    pattern = _like(keyword)
    lowered = keyword.lower()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT *, (CASE WHEN title LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) AS title_hit "
            "FROM reports WHERE title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
            "ORDER BY title_hit DESC, CASE WHEN date = '' THEN 1 ELSE 0 END, date DESC, title ASC",
            [pattern, pattern, pattern],
        ).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = _row_to_item(row)
        body = row["body"] or ""
        index = body.lower().find(lowered)
        if index >= 0:
            start = max(0, index - _SNIPPET_LIMIT // 2)
            end = min(len(body), index + len(keyword) + _SNIPPET_LIMIT // 2)
            snippet = ("…" if start > 0 else "") + body[start:end] + ("…" if end < len(body) else "")
            match = "body"
        else:
            snippet = item["summary"]
            match = "title"
        item["snippet"] = snippet
        item["match"] = match
        items.append(item)
    start = max(0, int(offset))
    return items[start : start + int(limit) if limit else None], len(items)


def _snippet_for(row: sqlite3.Row, keyword: str) -> dict:
    """命中正文时给一段上下文；只命中标题时回退到摘要。"""
    body = row["body"] or ""
    index = body.lower().find(keyword.lower())
    if index < 0:
        return {"snippet": row["summary"], "match": "title"}
    start = max(0, index - _SNIPPET_LIMIT // 2)
    end = min(len(body), index + len(keyword) + _SNIPPET_LIMIT // 2)
    snippet = ("…" if start > 0 else "") + body[start:end] + ("…" if end < len(body) else "")
    return {"snippet": snippet, "match": "body"}


def get_report(path: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM reports WHERE path = ?", (path,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    item = _row_to_item(row)
    item["preview"] = (row["body"] or "")[:1200]
    return item


def set_state(path: str, read: bool | None = None, starred: bool | None = None) -> dict | None:
    assignments = []
    params: list[object] = []
    now = _now()
    event: dict[str, object] = {}
    if read is not None:
        assignments.append("read = ?, read_at = ?")
        params.extend([1 if read else 0, now if read else ""])
        event["read"] = bool(read)
    if starred is not None:
        assignments.append("starred = ?, starred_at = ?")
        params.extend([1 if starred else 0, now if starred else ""])
        event["starred"] = bool(starred)
    if not assignments:
        return get_report(path)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE reports SET {', '.join(assignments)}, updated_at = ? WHERE path = ?",
            [*params, now, path],
        )
        if event:
            _record_event(conn, path, "state", event)
        conn.commit()
        row = conn.execute(
            "SELECT path, read, starred, read_at, starred_at FROM reports WHERE path = ?", (path,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "path": row["path"],
        "read": bool(row["read"]),
        "starred": bool(row["starred"]),
        "read_at": row["read_at"] or "",
        "starred_at": row["starred_at"] or "",
    }


# ---------- P6：公开报告（对外只读共享） ----------


def bulk_update(
    paths: list[str],
    read: bool | None = None,
    starred: bool | None = None,
    public: bool | None = None,
) -> dict:
    """批量更新多篇报告的已读/收藏/公开状态。

    返回 {updated, missing, paths}；不存在的路径计入 missing。
    """
    assignments: list[str] = []
    params: list[object] = []
    now = _now()
    event: dict[str, object] = {}
    if read is not None:
        assignments.append("read = ?, read_at = ?")
        params.extend([1 if read else 0, now if read else ""])
        event["read"] = bool(read)
    if starred is not None:
        assignments.append("starred = ?, starred_at = ?")
        params.extend([1 if starred else 0, now if starred else ""])
        event["starred"] = bool(starred)
    if public is not None:
        assignments.append("public = ?, public_at = ?")
        params.extend([1 if public else 0, now if public else ""])
        event["public"] = bool(public)
    unique = list(dict.fromkeys(paths))
    if not assignments or not unique:
        return {"updated": 0, "missing": unique[:20], "paths": []}
    placeholders = ",".join("?" for _ in unique)
    conn = _connect()
    try:
        cursor = conn.execute(
            f"UPDATE reports SET {', '.join(assignments)}, updated_at = ? WHERE path IN ({placeholders})",
            [*params, now, *unique],
        )
        updated = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        rows = conn.execute(
            f"SELECT path FROM reports WHERE path IN ({placeholders})", unique
        ).fetchall()
        for row in rows:
            _record_event(conn, row["path"], "state", {"bulk": True, **event})
        conn.commit()
    finally:
        conn.close()
    found = {row["path"] for row in rows}
    missing = [path for path in unique if path not in found]
    return {"updated": updated, "missing": missing[:20], "paths": [p for p in unique if p in found]}


def public_paths() -> list[str]:
    """所有已标记公开的报告相对路径（按路径排序，结果稳定便于缓存）。"""
    conn = _connect()
    try:
        rows = conn.execute("SELECT path FROM reports WHERE public = 1 ORDER BY path").fetchall()
    finally:
        conn.close()
    return [row["path"] for row in rows]


def set_public(path: str, public: bool) -> dict | None:
    """切换单篇报告的公开状态；报告不在索引里时返回 None。"""
    conn = _connect()
    try:
        now = _now()
        conn.execute(
            "UPDATE reports SET public = ?, public_at = ?, updated_at = ? WHERE path = ?",
            (1 if public else 0, now if public else "", now, path),
        )
        _record_event(conn, path, "public", {"public": bool(public)})
        conn.commit()
        row = conn.execute(
            "SELECT path, public, public_at FROM reports WHERE path = ?", (path,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"path": row["path"], "public": bool(row["public"]), "public_at": row["public_at"] or ""}


# ---------- P1：AI sidecar（摘要/要点/实体/标签） ----------

AI_VERSION = 1


def ai_stats() -> dict:
    """AI 抽取进度：已生成/待处理/失败/总数。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN ai_version >= ? THEN 1 ELSE 0 END) AS enriched,"
            " SUM(CASE WHEN ai_version < ? AND ai_error != '' THEN 1 ELSE 0 END) AS failed"
            " FROM reports",
            (AI_VERSION, AI_VERSION),
        ).fetchone()
    finally:
        conn.close()
    total = int(row["total"] or 0)
    enriched = int(row["enriched"] or 0)
    failed = int(row["failed"] or 0)
    return {
        "total": total,
        "enriched": enriched,
        "failed": failed,
        "pending": max(0, total - enriched - failed),
        "version": AI_VERSION,
    }


def next_pending(limit: int = 3, *, include_failed: bool = False) -> list[dict]:
    """取一批待抽取的报告（含正文/摘要），失败记录默认跳过，避免反复重试。"""
    if limit <= 0:
        return []
    clause = "ai_version < ?" + ("" if include_failed else " AND ai_error = ''")
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT path, title, category, series, date, summary, body FROM reports"
            f" WHERE {clause} ORDER BY ai_at ASC, mtime DESC LIMIT ?",
            [AI_VERSION, int(limit)],
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def set_ai(path: str, payload: dict, model: str = "") -> None:
    """写入抽取结果；调用方保证 payload 已清洗。同时落标签关系与变更轨迹。"""
    tags_json = json.dumps(_normalize_tags(payload.get("tags")), ensure_ascii=False)
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE reports SET summary_ai = ?, key_points = ?, entities = ?, tags = ?,"
            " ai_version = ?, ai_at = ?, ai_model = ?, ai_error = '', updated_at = ? WHERE path = ?",
            (
                str(payload.get("summary_ai") or "")[:400],
                json.dumps(payload.get("key_points") or [], ensure_ascii=False),
                json.dumps(payload.get("entities") or [], ensure_ascii=False),
                tags_json,
                int(payload.get("ai_version") or AI_VERSION),
                now,
                str(model or "")[:120],
                now,
                path,
            ),
        )
        _rebuild_report_tags(conn, path, tags_json)
        _refresh_tag_counts(conn)
        _record_event(
            conn,
            path,
            "ai_enrich",
            {
                "tags": _tag_list(tags_json),
                "summary_len": len(str(payload.get("summary_ai") or "")),
                "ai_version": int(payload.get("ai_version") or AI_VERSION),
            },
            model=model,
        )
        conn.commit()
    finally:
        conn.close()


def set_ai_error(path: str, message: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE reports SET ai_error = ?, ai_at = ?, updated_at = ? WHERE path = ?",
            (str(message)[:300], _now(), _now(), path),
        )
        _record_event(conn, path, "ai_error", {"error": str(message)[:300]})
        conn.commit()
    finally:
        conn.close()


# ---------- v0.3：标签导航与变更轨迹查询 ----------


def tags_overview(keyword: str = "", limit: int = 200) -> list[dict]:
    """标签列表（按报告数倒序）；keyword 做前缀/包含匹配。"""
    conn = _connect()
    try:
        if keyword.strip():
            rows = conn.execute(
                "SELECT name, reports, updated_at FROM tags WHERE name LIKE ? ESCAPE '\\'"
                " ORDER BY reports DESC, name ASC LIMIT ?",
                (_like(keyword.strip()) + "%", int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, reports, updated_at FROM tags ORDER BY reports DESC, name ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"name": row["name"], "reports": int(row["reports"] or 0), "updated_at": row["updated_at"] or ""}
        for row in rows
        if int(row["reports"] or 0) > 0
    ]


def events_for(path: str = "", limit: int = 50) -> list[dict]:
    """变更轨迹（append-only）：默认取全部，传 path 只看一篇。"""
    conn = _connect()
    try:
        if path:
            rows = conn.execute(
                "SELECT id, path, kind, payload, model, created_at FROM report_events"
                " WHERE path = ? ORDER BY id DESC LIMIT ?",
                (path, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, path, kind, payload, model, created_at FROM report_events"
                " ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": int(row["id"]),
            "path": row["path"],
            "kind": row["kind"],
            "payload": _load_json(row["payload"], {}),
            "model": row["model"] or "",
            "at": row["created_at"] or "",
        }
        for row in rows
    ]


# ---------- P2：语义向量与混合检索 ----------


def overview() -> dict:
    """健康检查用的轻量统计：一条聚合查询，不读正文、不触发模型。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(read = 0), 0) AS unread, "
            "COALESCE(SUM(starred = 1), 0) AS starred, "
            "COALESCE(SUM(public = 1), 0) AS public FROM reports"
        ).fetchone()
    finally:
        conn.close()
    data = {key: int(row[key] or 0) for key in ("total", "unread", "starred", "public")}
    return data


def embed_stats(model: str) -> dict:
    """向量覆盖情况（供 /api/kb/embed/stats）。"""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
        embedded = conn.execute(
            "SELECT COUNT(*) AS n FROM reports WHERE embedding IS NOT NULL AND embed_model = ?",
            [model],
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"total": total, "embedded": embedded, "pending": max(0, total - embedded), "model": model}


def next_pending_embedding(limit: int = 16, *, model: str = "") -> list[dict]:
    """取出还没有当前模型向量的报告（按时间倒序）。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT path, title, category, series, date, summary, summary_ai, tags, body "
            "FROM reports WHERE embedding IS NULL OR embed_model != ? "
            "ORDER BY CASE WHEN date = '' THEN 1 ELSE 0 END, date DESC, title ASC LIMIT ?",
            [model, int(limit)],
        ).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        item["tags"] = _load_json(item.get("tags") or "[]", [])
        items.append(item)
    return items


def set_embedding(path: str, vector, model: str) -> None:
    """写入一篇报告的向量（float32 小端 BLOB）与模型名。"""
    values = np.asarray(list(vector), dtype=np.float32)
    conn = _connect()
    try:
        with _write_lock:
            conn.execute(
                "UPDATE reports SET embedding = ?, embed_model = ?, embed_at = datetime('now') WHERE path = ?",
                [values.tobytes(), model, path],
            )
            conn.commit()
    finally:
        conn.close()


def vectors_snapshot(
    *,
    model: str,
    category: str = "",
    series: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
) -> tuple[list[sqlite3.Row], np.ndarray]:
    """取出符合筛选条件的向量矩阵（N×D，已按行归一化）。"""
    where = ["embedding IS NOT NULL", "embed_model = ?"]
    params: list = [model]
    if category:
        where.append("category = ?")
        params.append(category)
    if series:
        where.append("series = ?")
        params.append(series)
    date_from = _norm_date(date_from)
    date_to = _norm_date(date_to)
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)
    if view == "unread":
        where.append("read = 0")
    elif view == "starred":
        where.append("starred = 1")
    clause = " AND ".join(where)
    conn = _connect()
    try:
        rows = conn.execute(f"SELECT *, embedding FROM reports WHERE {clause}", params).fetchall()
    finally:
        conn.close()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    matrix = np.vstack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return rows, (matrix / norms).astype(np.float32)


def hybrid_search(
    q: str,
    *,
    query_vector: list[float],
    model: str,
    category: str = "",
    series: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    offset: int = 0,
    limit: int = 20,
    semantic_weight: float = 0.62,
    lexical_weight: float = 0.38,
) -> tuple[list[dict], int]:
    """语义 + 词法混合检索；返回 (items, total)。

    词法分：标题命中 1.0、正文命中 0.55；语义分为余弦相似度（<0 记 0）。
    只在「语义 > 0.28 或词法 > 0」的候选里排序，避免语义噪声污染结果。
    """
    keyword = (q or "").strip()
    rows, matrix = vectors_snapshot(
        model=model,
        category=category,
        series=series,
        date_from=date_from,
        date_to=date_to,
        view=view,
    )
    if not rows:
        return [], 0
    vector = np.asarray(query_vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector)) or 1.0
    cosine = matrix @ (vector / norm)
    lowered = keyword.lower()
    scored = []
    for index, row in enumerate(rows):
        title = (row["title"] or "").lower()
        body = (row["body"] or "").lower()
        lexical = 0.0
        match = "semantic"
        if lowered:
            if lowered in title:
                lexical, match = 1.0, "title"
            elif lowered in body:
                lexical, match = 0.55, "body"
        similarity = max(0.0, float(cosine[index]))
        if lowered and lexical == 0.0 and similarity < 0.28:
            continue
        if not lowered and similarity < 0.15:
            continue
        score = semantic_weight * similarity + lexical_weight * lexical
        if score <= 0.05:
            continue
        scored.append((score, similarity, match, row))
    scored.sort(key=lambda entry: (-entry[0], entry[3]["date"] or "", entry[3]["title"] or ""))
    total = len(scored)
    window = scored[max(0, int(offset)) : max(0, int(offset)) + int(limit)]
    items = []
    for score, similarity, match, row in window:
        item = _row_to_item(row)
        snippet = _snippet_for(row, keyword) if match == "body" else {"snippet": item["summary_ai"] or item["summary"], "match": match}
        item.update(snippet)
        item["score"] = round(float(score), 4)
        item["semantic_score"] = round(float(similarity), 4)
        item["match"] = match if match != "semantic" else "semantic"
        items.append(item)
    return items, total
