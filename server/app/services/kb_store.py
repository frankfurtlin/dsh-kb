"""知识库索引库（SQLite）：报告元数据 + 摘要/正文全文 + 已读/收藏状态。

设计要点：
- `reports/` 目录始终只读；本模块只写 `KB_DATA_DIR/kb.sqlite`；
- 增量同步：按 (size, mtime) 判断变化，只有变化的报告才重新读盘、抽摘要/正文；
- 全文检索用 `LIKE`：报告量级（数百篇）下足够快，且对中文任意长度查询都有效
  （FTS5 的 trigram 不支持 1–2 字中文查询，unicode61 又无法切分中文）。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from app.config import DATA_DIR, INDEX_FILE, SEMANTIC_MIN_SCORE
from app.services import kb_index

CLASSIFY_VERSION = 1  # AI 分类提示词/规则版本：升级会让所有报告重新分类

_BODY_LIMIT = 20000
_SUMMARY_LIMIT = 180
_SNIPPET_LIMIT = 160

# P0-1：语义相似度门槛（对「已去中心」的余弦而言），来自 config.SEMANTIC_MIN_SCORE
# （env KB_SEMANTIC_MIN_SCORE）。bge-small-zh 的原始余弦基线偏高（无关文本 0.3–0.5），
# 去中心后无关约 0.10–0.30，相关报告通常 ≥0.35。

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

-- P10：知识图谱 + LLM Wiki。wiki 页是 reports 与 AI 实体的派生产物，
-- 这里只存索引与引用关系；正文 markdown 同时写到 GRAPH_DIR/pages/<slug>.md。
CREATE TABLE IF NOT EXISTS wiki_page (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'entity',       -- entity | topic | query
    title TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'live',        -- live | stale | archived
    report_count INTEGER NOT NULL DEFAULT 0,
    generated_by TEXT NOT NULL DEFAULT 'deterministic',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wiki_page_kind ON wiki_page(kind, state);

-- 页内出链：target_slug 指向另一张 wiki 页（[[slug|label]]）。
CREATE TABLE IF NOT EXISTS wiki_link (
    page_id INTEGER NOT NULL,
    target_slug TEXT NOT NULL,
    PRIMARY KEY (page_id, target_slug)
);
CREATE INDEX IF NOT EXISTS idx_wiki_link_target ON wiki_link(target_slug);

-- 页的来源报告（反向引用）：删除/更新报告时靠它定位要重建的页。
CREATE TABLE IF NOT EXISTS wiki_source (
    page_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    contribution TEXT NOT NULL DEFAULT 'entity',
    PRIMARY KEY (page_id, path)
);
CREATE INDEX IF NOT EXISTS idx_wiki_source_path ON wiki_source(path);

-- P11：实体共现边（多行关系）。weight = 同篇报告共同出现的篇数；
-- 无向边按 (source_slug < target_slug) 规范化，kind 预留 cooccur/related/manual。
CREATE TABLE IF NOT EXISTS entity_edges (
    source_slug TEXT NOT NULL,
    target_slug TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'cooccur',
    weight INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_slug, target_slug, kind)
);
CREATE INDEX IF NOT EXISTS idx_entity_edges_source ON entity_edges(source_slug);
CREATE INDEX IF NOT EXISTS idx_entity_edges_target ON entity_edges(target_slug);

-- 增量编译台账：hash 命中即跳过，失败可重试。
CREATE TABLE IF NOT EXISTS compile_job (
    report_path TEXT NOT NULL,
    page_slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (report_path, page_slug)
);

-- P13：LLM Wiki 文章（模型撰写，带 [n] 来源引用；与图谱节点分表分目录）。
CREATE TABLE IF NOT EXISTS wiki_article (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'topic',        -- topic | entity | overview
    subject TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'live',        -- live | stale | archived
    model TEXT NOT NULL DEFAULT '',
    takeaway_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wiki_article_state ON wiki_article(state, updated_at);

CREATE TABLE IF NOT EXISTS wiki_article_source (
    article_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,       -- 正文里的引用编号 [n]
    title TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (article_id, path)
);
CREATE INDEX IF NOT EXISTS idx_wiki_article_source_path ON wiki_article_source(path);

-- 生成队列：手动/后台排队的文章；状态 pending | running | done | failed。
CREATE TABLE IF NOT EXISTS wiki_article_job (
    slug TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
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

# R1：AI 分类（规则值保留在 *_rule，AI 值在 *_ai，category/series 是生效值）。
CLASSIFY_COLUMNS = {
    "category_rule": "TEXT NOT NULL DEFAULT ''",
    "series_rule": "TEXT NOT NULL DEFAULT ''",
    "category_ai": "TEXT NOT NULL DEFAULT ''",
    "series_ai": "TEXT NOT NULL DEFAULT ''",
    "classify_version": "INTEGER NOT NULL DEFAULT 0",
    "classify_at": "TEXT NOT NULL DEFAULT ''",
    "classify_model": "TEXT NOT NULL DEFAULT ''",
    "classify_error": "TEXT NOT NULL DEFAULT ''",
}

# P10/P12：报告内容指纹（sha1），供图谱节点增量编译与「过期节点」检测。
WIKI_COLUMNS = {
    "content_hash": "TEXT NOT NULL DEFAULT ''",
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

# P10：wiki 页向量（step 3：让 wiki 页进入同一套混合检索）。
WIKI_EMBED_COLUMNS = {
    "embedding": "BLOB",
    "embed_model": "TEXT NOT NULL DEFAULT ''",
    "embed_at": "TEXT NOT NULL DEFAULT ''",
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
    _ensure_wiki_columns(conn)
    _backfill_meta(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给旧索引库补齐 P1 新增的 AI 列（幂等）。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(reports)")}
    for column, definition in {
        **AI_COLUMNS,
        **EMBED_COLUMNS,
        **PUBLIC_COLUMNS,
        **META_COLUMNS,
        **CLASSIFY_COLUMNS,
        **WIKI_COLUMNS,
    }.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {column} {definition}")
    conn.commit()


def _ensure_wiki_columns(conn: sqlite3.Connection) -> None:
    """给旧库的 wiki_page / wiki_article 表补齐向量列（幂等）。"""
    for table in ("wiki_page", "wiki_article"):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in WIKI_EMBED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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
    conn.execute("UPDATE reports SET category_rule = category WHERE category_rule = ''")
    conn.execute("UPDATE reports SET series_rule = series WHERE series_rule = ''")
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


def _content_hash(path: Path) -> str:
    """报告文件内容指纹（sha1），供节点增量编译判断「这篇报告变没变」。"""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def connect() -> sqlite3.Connection:
    """公开的连接入口：其他模块（kb_wiki）复用同一套 schema/迁移。"""
    return _connect()


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
    """把扫描结果增量同步进索引库；返回 {indexed, removed, total}。

    变更判定分两类，避免把「AI 改过分类」误判成文件变化：

    - 内容变化（size/mtime）：重读正文，旧 AI sidecar 与 AI 分类全部失效并回落规则值；
    - 元数据变化（标题/日期/规则分类/系列）：只更新规则基线与展示字段，AI 结论保留。

    之前这里直接比较生效列 `category/series`，AI 分类写回后每次同步都会被当成
    「规则变了」而清空 AI 结果，R1 联调时踩过，回归用例见 tests/test_classify.py。
    """
    global _indexed_at
    items = kb_index.cached_scan(force=force)
    incoming = {item["path"]: item for item in items}
    indexed = removed = 0
    with _write_lock:
        conn = _connect()
        try:
            existing = {
                row["path"]: row
                for row in conn.execute(
                    "SELECT path, size, mtime, title, category, series, category_rule, series_rule, date,"
                    " content_hash"
                    " FROM reports"
                )
            }
            changed: list[str] = []
            now = _now()
            for path, item in incoming.items():
                stat_key = (int(item["size"]), int(item["mtime"]))
                row = existing.get(path)
                content_changed = row is None or (row["size"], row["mtime"]) != stat_key
                rules_changed = row is not None and (
                    (row["category_rule"] or ""),
                    (row["series_rule"] or ""),
                ) != (item["category"], item["series"])
                meta_changed = row is not None and (
                    (row["title"], row["date"]) != (item["title"], item["date"]) or rules_changed
                )
                if row is not None and not content_changed and not (row["content_hash"] or ""):
                    # 旧库升级：补算内容指纹（不触发 AI 失效，只补 hash）。
                    conn.execute(
                        "UPDATE reports SET content_hash=? WHERE path=?",
                        (_content_hash(kb_index.REPORTS_DIR / path), path),
                    )
                if not content_changed and not meta_changed:
                    continue

                if content_changed:
                    report_file = kb_index.REPORTS_DIR / path
                    summary, body = _extract_text(report_file)
                    chash = _content_hash(report_file)
                    conn.execute(
                        """
                        INSERT INTO reports
                            (path, title, category, series, category_rule, series_rule,
                             date, size, mtime, summary, body, content_hash, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            title=excluded.title,
                            date=excluded.date, size=excluded.size, mtime=excluded.mtime,
                            summary=excluded.summary, body=excluded.body,
                            content_hash=excluded.content_hash,
                            category_rule=excluded.category_rule,
                            series_rule=excluded.series_rule,
                            -- 正文变化 → AI 分类失效，生效值回落规则值。
                            category=excluded.category_rule,
                            series=excluded.series_rule,
                            updated_at=excluded.updated_at,
                            summary_ai='', key_points='[]', entities='[]', tags='[]',
                            ai_version=0, ai_at='', ai_error='',
                            category_ai='', series_ai='',
                            classify_version=0, classify_at='', classify_error=''
                        """,
                        (
                            path,
                            item["title"],
                            item["category"],
                            item["series"],
                            item["category"],
                            item["series"],
                            item["date"],
                            stat_key[0],
                            stat_key[1],
                            summary,
                            body,
                            chash,
                            now,
                            now,
                        ),
                    )
                else:
                    # 元数据变化：AI 结论仍在，生效值优先用 AI 结论。
                    conn.execute(
                        """
                        UPDATE reports SET
                            title=?, date=?, category_rule=?, series_rule=?,
                            category=CASE WHEN category_ai != '' THEN category_ai ELSE ? END,
                            series=CASE WHEN category_ai != '' THEN series_ai ELSE ? END,
                            updated_at=?
                        WHERE path=?
                        """,
                        (
                            item["title"],
                            item["date"],
                            item["category"],
                            item["series"],
                            item["category"],
                            item["series"],
                            now,
                            path,
                        ),
                    )
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
                    indexed += 1
                else:
                    refreshed = conn.execute(
                        "SELECT category, series FROM reports WHERE path=?", (path,)
                    ).fetchone()
                    if (row["category"], row["series"]) != (
                        refreshed["category"] if refreshed else item["category"],
                        refreshed["series"] if refreshed else item["series"],
                    ):
                        _record_event(
                            conn,
                            path,
                            "reclassify",
                            {
                                "from": {"category": row["category"], "series": row["series"]},
                                "to": {
                                    "category": refreshed["category"] if refreshed else item["category"],
                                    "series": refreshed["series"] if refreshed else item["series"],
                                },
                                "source": "rule",
                            },
                        )
                    indexed += 1
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


def _keyword_tokens(keyword: str) -> list[str]:
    """把查询拆成词：空格/中英文逗号、分号、竖线都算分隔符。"""
    return [token for token in re.split(r"[\s,，、;；|]+", (keyword or "").strip()) if token]


def _keyword_sql(keyword: str) -> tuple[str, list[str]]:
    """多词查询按 AND 组合（每个词都要在标题或正文里出现），单词时等价 LIKE。"""
    tokens = _keyword_tokens(keyword)
    parts: list[str] = []
    params: list[str] = []
    for token in tokens:
        parts.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
        params.extend([_like(token), _like(token)])
    return " AND ".join(parts), params


def _lexical_score(title: str, body: str, keyword: str) -> tuple[float, str]:
    """词法匹配分：整串命中 > 全词命中 > 多词近似命中。"""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return 0.0, "semantic"
    if keyword in title:
        return 1.0, "title"
    if keyword in body:
        return 0.55, "body"
    tokens = [token.lower() for token in _keyword_tokens(keyword) if len(token) >= 2]
    if len(tokens) > 1:
        title_hits = sum(1 for token in tokens if token in title)
        body_hits = sum(1 for token in tokens if token in body)
        if title_hits == len(tokens):
            return 0.9, "title"
        if body_hits == len(tokens):
            return 0.5, "body"
        if body_hits >= max(2, len(tokens) - 1):
            return 0.3, "body"
    return 0.0, "semantic"


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
        # R1：规则值 / AI 值 / 当前生效值（category、series 是生效值）。
        "category_rule": row["category_rule"] or "",
        "series_rule": row["series_rule"] or "",
        "category_ai": row["category_ai"] or "",
        "series_ai": row["series_ai"] or "",
        "classify_version": row["classify_version"] or 0,
        "classify_at": row["classify_at"] or "",
        "classify_model": row["classify_model"] or "",
        "classify_error": row["classify_error"] or "",
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
        clause, clause_params = _keyword_sql(keyword)
        if clause:
            where.append("(" + clause + ")")
            params.extend(clause_params)
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
        clause, clause_params = _keyword_sql(keyword)
        if clause:
            where.append("(" + clause + ")")
            params.extend(clause_params)
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
    clause, clause_params = _keyword_sql(keyword)
    lowered = keyword.lower()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT *, (CASE WHEN title LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) AS title_hit "
            "FROM reports WHERE " + (clause or "title LIKE ? ESCAPE '\\'") + " "
            "ORDER BY title_hit DESC, CASE WHEN date = '' THEN 1 ELSE 0 END, date DESC, title ASC",
            [pattern] + (clause_params if clause else [pattern]),
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


# ---------- R1：AI 分类 ----------


def classify_stats() -> dict:
    """AI 分类进度。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN classify_version >= ? THEN 1 ELSE 0 END) AS classified,"
            " SUM(CASE WHEN classify_version < ? AND classify_error != '' THEN 1 ELSE 0 END) AS failed"
            " FROM reports",
            (CLASSIFY_VERSION, CLASSIFY_VERSION),
        ).fetchone()
    finally:
        conn.close()
    total = int(row["total"] or 0)
    classified = int(row["classified"] or 0)
    failed = int(row["failed"] or 0)
    return {
        "total": total,
        "classified": classified,
        "failed": failed,
        "pending": max(0, total - classified - failed),
        "version": CLASSIFY_VERSION,
    }


def next_pending_classify(limit: int = 3, *, include_failed: bool = False) -> list[dict]:
    """取一批待分类报告；给模型只需标题/日期/规则分类/标签/摘要，不用全文。"""
    if limit <= 0:
        return []
    clause = "classify_version < ?" + ("" if include_failed else " AND classify_error = ''")
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT path, title, date, category, series, category_rule, series_rule,"
            " tags, summary_ai, summary FROM reports"
            f" WHERE {clause} ORDER BY classify_at ASC, mtime DESC LIMIT ?",
            [CLASSIFY_VERSION, int(limit)],
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def set_classify(path: str, payload: dict, model: str = "") -> dict | None:
    """写入 AI 分类并把 category/series 切到生效值（人工覆盖尚未实现，AI 优先于规则）。"""
    from app.services import kb_index

    category = str(payload.get("category") or "").strip()
    if category not in kb_index.CATEGORIES:
        raise ValueError(f"分类不在允许枚举内：{category!r}")
    series = " ".join(str(payload.get("series") or "").split())[:40]
    reason = " ".join(str(payload.get("reason") or "").split())[:60]
    now = _now()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT category, series FROM reports WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE reports SET category = ?, series = ?, category_ai = ?, series_ai = ?,"
            " classify_version = ?, classify_at = ?, classify_model = ?, classify_error = '',"
            " updated_at = ? WHERE path = ?",
            (category, series, category, series, CLASSIFY_VERSION, now, str(model or "")[:120], now, path),
        )
        before = {"category": row["category"], "series": row["series"]}
        after = {"category": category, "series": series}
        if before != after:
            _record_event(
                conn,
                path,
                "classify",
                {"from": before, "to": after, "reason": reason, "source": "ai"},
                model=model,
            )
        conn.commit()
    finally:
        conn.close()
    return {"path": path, "category": category, "series": series}


def set_classify_error(path: str, message: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE reports SET classify_error = ?, classify_at = ?, updated_at = ? WHERE path = ?",
            (str(message)[:300], _now(), _now(), path),
        )
        _record_event(conn, path, "classify_error", {"error": str(message)[:300]})
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


def _centered_cosine(matrix, vector, center) -> "np.ndarray":
    """去除语料各向异性后的余弦相似度。

    bge 这类中文小模型的原始余弦有很强的"全局偏移"：任何两段中文的相似度都
    在 0.3–0.5，直接拿 0.28 当门槛等于放行全库。先减去语料中心向量再归一化
    点积，无关文本会掉到 0.15–0.30，相关文本仍保持在 0.35 以上。
    """
    vec = np.asarray(vector, dtype=np.float32).reshape(-1)
    if center is not None and len(center):
        flat = np.asarray(center, dtype=np.float32).reshape(-1)
        vec = vec - flat
        mat = matrix - flat
    else:
        mat = matrix
    vnorm = float(np.linalg.norm(vec)) or 1.0
    mnorms = np.linalg.norm(mat, axis=1, keepdims=True)
    mnorms[mnorms == 0] = 1.0
    return (mat / mnorms) @ (vec / vnorm)


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
    include_nodes: bool = True,
    include_articles: bool = True,
    ppr_weight: float = 0.0,
    mode: str = "hybrid",
) -> tuple[list[dict], int]:
    """语义 + 词法混合检索；返回 (items, total)。

    词法分：标题命中 1.0、正文命中 0.55；语义分为去中心余弦（<0 记 0）。
    mode="hybrid"（默认）：词法命中是硬门槛，语义只负责排序——查不到词就返回空，
    避免"任何查询都返回全库"；mode="semantic"：走自适应语义召回（去中心余弦
    ≥SEMANTIC_MIN_SCORE），适合"意思相近但字面不同"的探索。
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
    vector = np.asarray(query_vector, dtype=np.float32)
    center = matrix.mean(axis=0, keepdims=True) if getattr(matrix, "size", 0) else None
    # 报告向量可能还没补齐（刚重启/新库），但这不影响 wiki 页与文章候选。
    cosine = _centered_cosine(matrix, vector, center) if rows else np.zeros(0)
    lowered = keyword.lower()
    # semantic 模式再加一条"相对门槛"：只保留和 top1 差距不大的候选，
    # 避免一个泛化查询把半库都算成"语义相关"。
    cutoff = SEMANTIC_MIN_SCORE
    if mode == "semantic" and lowered and getattr(cosine, "size", 0):
        cutoff = max(cutoff, float(np.max(cosine)) * 0.85)
    scored = []
    for index, row in enumerate(rows):
        title = (row["title"] or "").lower()
        body = (row["body"] or "").lower()
        lexical, match = _lexical_score(title, body, lowered)
        similarity = max(0.0, float(cosine[index]))
        if lowered:
            if mode == "semantic":
                if similarity < cutoff:
                    continue
            elif lexical == 0.0:
                # P0-1：混合模式必须词法命中，语义不单独召回。
                continue
        elif similarity < cutoff:
            continue
        score = semantic_weight * similarity + lexical_weight * lexical
        if score <= 0.05:
            continue
        scored.append((score, similarity, match, row))
    # P11：图上的第三路召回——以当前 top 报告为种子在 wiki 页图上跑 PPR，
    # 把页质量摊回报告后作为小幅提升项（默认权重由 HYBRID_WEIGHT_PPR 控制）。
    if keyword and ppr_weight > 0 and scored:
        try:
            top_paths = [
                row["path"]
                for _, _, _, row in sorted(scored, key=lambda entry: -entry[0])[:8]
                if not isinstance(row, dict)
            ]
            boost = ppr_report_boost(top_paths)
            if boost:
                scored = [
                    (score + ppr_weight * boost.get(row["path"], 0.0), similarity, match, row)
                    for score, similarity, match, row in scored
                ]
        except Exception:  # 图检索失败不能影响主检索
            pass

    # wiki 页作为「单实体文档」参与同一排序；有分类/系列/日期筛选时先不混入，
    # 避免把浏览视图（按分类翻报告）变成混合列表。
    filtered = bool(category or series or date_from or date_to or view != "all")
    if include_nodes and not filtered:
        scored.extend(
            _node_candidates(
                keyword,
                query_vector,
                model=model,
                semantic_weight=semantic_weight,
                lexical_weight=lexical_weight,
                mode=mode,
                center=center,
                cutoff=cutoff,
            )
        )
    if include_articles and not filtered:
        scored.extend(
            _article_candidates(
                keyword,
                query_vector,
                model=model,
                semantic_weight=semantic_weight,
                lexical_weight=lexical_weight,
                mode=mode,
                center=center,
                cutoff=cutoff,
            )
        )
    scored.sort(key=lambda entry: (-entry[0], (entry[3]["date"] if not isinstance(entry[3], dict) else "") or "",
                                   entry[3]["title"] or ""))
    total = len(scored)
    window = scored[max(0, int(offset)) : max(0, int(offset)) + int(limit)]
    items = []
    for score, similarity, match, row in window:
        if isinstance(row, dict):  # wiki 页候选已经是成品 item
            item = dict(row)
            item["score"] = round(float(score), 4)
            item["semantic_score"] = round(float(similarity), 4)
            items.append(item)
            continue
        item = _row_to_item(row)
        snippet = _snippet_for(row, keyword) if match == "body" else {"snippet": item["summary_ai"] or item["summary"], "match": match}
        item.update(snippet)
        item["score"] = round(float(score), 4)
        item["semantic_score"] = round(float(similarity), 4)
        item["match"] = match if match != "semantic" else "semantic"
        items.append(item)
    return items, total

# ---------- P10 step 3：wiki 页向量与混合检索候选 ----------


def node_embed_stats(model: str) -> dict:
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM wiki_page").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM wiki_page WHERE embedding IS NOT NULL AND embed_model = ?",
            (model,),
        ).fetchone()[0]
        return {"total": total, "embedded": embedded, "pending": max(0, total - embedded)}
    finally:
        conn.close()


def next_pending_node_embedding(limit: int, *, model: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slug, title, entity_type, body_md FROM wiki_page"
            " WHERE embed_model != ? OR embedding IS NULL"
            " ORDER BY report_count DESC, slug ASC LIMIT ?",
            (model, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_node_embedding(slug: str, vector, model: str) -> None:
    payload = np.asarray(vector, dtype=np.float32).tobytes()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE wiki_page SET embedding = ?, embed_model = ?, embed_at = ? WHERE slug = ?",
            (payload, model, _now(), slug),
        )
        conn.commit()
    finally:
        conn.close()


def node_vectors(*, model: str):
    """取全部 wiki 页向量（按行归一化），返回 (rows, matrix)。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slug, title, entity_type, body_md, report_count, embedding"
            " FROM wiki_page WHERE embedding IS NOT NULL AND embed_model = ?",
            (model,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    matrix = np.vstack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return rows, (matrix / norms).astype(np.float32)


def _wiki_plain_text(body_md: str) -> str:
    """把 wiki markdown 压成纯文本摘要（去 front matter/标题标记/wiki 链接语法）。"""
    text = str(body_md or "")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                lines = lines[index + 1:]
                break
    text = "\n".join(lines)
    text = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: (m.group(2) or m.group(1)), text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = text.replace("**", "").replace("`", "")
    return " ".join(text.split())


def article_embed_stats(model: str) -> dict:
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM wiki_article WHERE state != 'archived'").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM wiki_article WHERE state != 'archived'"
            " AND embedding IS NOT NULL AND embed_model = ?",
            (model,),
        ).fetchone()[0]
        return {"total": total, "embedded": embedded, "pending": max(0, total - embedded)}
    finally:
        conn.close()


def next_pending_article_embedding(limit: int, *, model: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slug, title, kind, overview, body_md FROM wiki_article"
            " WHERE state != 'archived' AND (embed_model != ? OR embedding IS NULL)"
            " ORDER BY source_count DESC, slug ASC LIMIT ?",
            (model, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_article_embedding(slug: str, vector, model: str) -> None:
    payload = np.asarray(vector, dtype=np.float32).tobytes()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE wiki_article SET embedding = ?, embed_model = ?, embed_at = ? WHERE slug = ?",
            (payload, model, _now(), slug),
        )
        conn.commit()
    finally:
        conn.close()


def article_vectors(*, model: str):
    """取全部文章向量（按行归一化），返回 (rows, matrix)。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT slug, title, kind, subject, overview, body_md, source_count, generated_at, embedding"
            " FROM wiki_article WHERE state != 'archived' AND embedding IS NOT NULL AND embed_model = ?",
            (model,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    matrix = np.vstack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return rows, (matrix / norms).astype(np.float32)


def _article_candidates(keyword: str, query_vector, *, model: str,
                        semantic_weight: float, lexical_weight: float,
                        mode: str = "hybrid", center=None,
                        cutoff: float = SEMANTIC_MIN_SCORE) -> list[tuple]:
    """把 LLM Wiki 文章也当成可检索文档（标题/总览/正文参与词法，向量参与语义）。"""
    rows, matrix = article_vectors(model=model)
    if not rows:
        return []
    vector = np.asarray(query_vector, dtype=np.float32)
    cosine = _centered_cosine(matrix, vector, center)
    lowered = (keyword or "").lower()
    out = []
    for index, row in enumerate(rows):
        plain = _wiki_plain_text(row["body_md"])
        title = (row["title"] or "").lower()
        haystack = f"{row['subject']} {row['overview']} {plain}".lower()
        lexical, match = _lexical_score(title, haystack, lowered)
        similarity = max(0.0, float(cosine[index]))
        if lowered:
            if mode == "semantic":
                if similarity < cutoff:
                    continue
            elif lexical == 0.0:
                continue
        elif similarity < cutoff:
            continue
        score = semantic_weight * similarity + lexical_weight * lexical
        if score <= 0.05:
            continue
        summary = (row["overview"] or plain)[:280]
        item = {
            "kind": "article",
            "slug": row["slug"],
            "path": f"article:{row['slug']}",
            "url": f"wiki/{row['slug']}",
            "title": row["title"],
            "entity_type": row["kind"],
            "category": "LLM Wiki",
            "series": "文章",
            "date": (row["generated_at"] or "")[:10],
            "summary": summary,
            "summary_ai": summary,
            "source_count": row["source_count"] or 0,
            "match": match,
            "snippet": summary[:160],
            "score": round(float(score), 4),
            "semantic_score": round(float(similarity), 4),
        }
        out.append((score, similarity, match, item))
    return out


def _node_candidates(keyword: str, query_vector, *, model: str,
                     semantic_weight: float, lexical_weight: float,
                     mode: str = "hybrid", center=None,
                     cutoff: float = SEMANTIC_MIN_SCORE) -> list[tuple]:
    """把 wiki 页当成「一篇只讲一个实体的文档」参与同一套混合打分。"""
    rows, matrix = node_vectors(model=model)
    if not rows:
        return []
    vector = np.asarray(query_vector, dtype=np.float32)
    cosine = _centered_cosine(matrix, vector, center)
    lowered = (keyword or "").lower()
    out = []
    for index, row in enumerate(rows):
        title = (row["title"] or "").lower()
        body = (row["body_md"] or "").lower()
        lexical, match = _lexical_score(title, body, lowered)
        similarity = max(0.0, float(cosine[index]))
        if lowered:
            if mode == "semantic":
                if similarity < cutoff:
                    continue
            elif lexical == 0.0:
                continue
        elif similarity < cutoff:
            continue
        score = semantic_weight * similarity + lexical_weight * lexical
        if score <= 0.05:
            continue
        summary = _wiki_plain_text(row["body_md"])[:280]
        item = {
            "kind": "node",
            "slug": row["slug"],
            "path": f"node:{row['slug']}",
            "url": f"wiki/{row['slug']}",
            "title": row["title"],
            "entity_type": row["entity_type"] or "",
            "category": "",
            "series": row["entity_type"] or "",
            "date": "",
            "summary": summary,
            "summary_ai": "",
            "report_count": row["report_count"] or 0,
            "match": match,
            "snippet": summary[:160],
            "score": round(float(score), 4),
            "semantic_score": round(float(similarity), 4),
        }
        out.append((score, similarity, match, item))
    return out


# ---------- P11：实体图（共现边 + PPR 第三路召回） ----------

_GRAPH_CACHE: dict = {"at": 0.0, "adj": None, "pages": None}


def graph_data(limit: int = 220, *, min_weight: int = 0) -> dict:
    """图谱画布数据：按提及报告数取前 N 个节点，附节点间共现边与互链边。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT slug, kind, title, entity_type, report_count FROM wiki_page"
            " WHERE state = 'live' ORDER BY report_count DESC, slug ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        nodes = [
            {
                "slug": row["slug"],
                "kind": row["kind"],
                "title": row["title"],
                "entity_type": row["entity_type"],
                "report_count": row["report_count"],
            }
            for row in rows
        ]
        slugs = {node["slug"] for node in nodes}
        edges: dict[tuple[str, str], int] = {}
        for row in conn.execute("SELECT source_slug, target_slug, weight FROM entity_edges"):
            source, target = row["source_slug"], row["target_slug"]
            if source in slugs and target in slugs and row["weight"] >= min_weight:
                key = (source, target) if source <= target else (target, source)
                edges[key] = max(edges.get(key, 0), int(row["weight"]))
        for row in conn.execute(
            "SELECT p.slug AS source, l.target_slug AS target FROM wiki_link l"
            " JOIN wiki_page p ON p.id = l.page_id WHERE p.state = 'live'"
        ):
            source, target = row["source"], row["target"]
            if source in slugs and target in slugs:
                key = (source, target) if source <= target else (target, source)
                edges.setdefault(key, 1)
        total = conn.execute("SELECT COUNT(*) FROM wiki_page WHERE state = 'live'").fetchone()[0]
        return {
            "nodes": nodes,
            "edges": [{"source": a, "target": b, "weight": w} for (a, b), w in edges.items()],
            "showing": len(nodes),
            "total_nodes": total,
        }
    finally:
        conn.close()


def invalidate_graph_cache() -> None:
    _GRAPH_CACHE["at"] = 0.0
    _GRAPH_CACHE["adj"] = None
    _GRAPH_CACHE["pages"] = None


def replace_entity_edges(edges, *, conn: sqlite3.Connection | None = None) -> int:
    """整表替换共现边。edges 为 (slug_a, slug_b, weight) 迭代；自动做无向规范化。

    传入 conn 时复用调用方事务（编译期间正持有同一连接的写事务）。
    """
    rows = []
    for a, b, weight in edges:
        if not a or not b or a == b or int(weight) <= 0:
            continue
        left, right = (a, b) if a < b else (b, a)
        rows.append((left, right, int(weight), _now()))
    own = conn is None
    connection = _connect() if own else conn
    try:
        connection.execute("DELETE FROM entity_edges")
        connection.executemany(
            "INSERT OR REPLACE INTO entity_edges (source_slug, target_slug, kind, weight, updated_at)"
            " VALUES (?, ?, 'cooccur', ?, ?)",
            rows,
        )
        if own:
            connection.commit()
    finally:
        if own:
            connection.close()
    invalidate_graph_cache()
    return len(rows)


def _graph() -> tuple[dict[str, dict[str, float]], list[str]]:
    """页级无向图（wiki_link 权重 1 + 实体共现边权重 min(weight,5)），带 30s 缓存。"""
    now = time.time()
    if _GRAPH_CACHE["adj"] is not None and now - _GRAPH_CACHE["at"] < 30:
        return _GRAPH_CACHE["adj"], _GRAPH_CACHE["pages"]
    conn = _connect()
    try:
        pages = [row["slug"] for row in conn.execute(
            "SELECT slug FROM wiki_page WHERE state = 'live'"
        )]
        id_to_slug = {
            row["id"]: row["slug"]
            for row in conn.execute("SELECT id, slug FROM wiki_page WHERE state = 'live'")
        }
        adj: dict[str, dict[str, float]] = {slug: {} for slug in pages}
        for row in conn.execute("SELECT page_id, target_slug FROM wiki_link"):
            source = id_to_slug.get(row["page_id"])
            target = row["target_slug"]
            if source and target in adj:
                adj[source][target] = max(adj[source].get(target, 0.0), 1.0)
                adj[target][source] = max(adj[target].get(source, 0.0), 1.0)
        for row in conn.execute("SELECT source_slug, target_slug, weight FROM entity_edges"):
            source, target = row["source_slug"], row["target_slug"]
            if source in adj and target in adj:
                weight = float(min(int(row["weight"]), 5))
                merged = max(adj[source].get(target, 0.0), weight)
                adj[source][target] = merged
                adj[target][source] = max(adj[target].get(source, 0.0), merged)
    finally:
        conn.close()
    _GRAPH_CACHE.update({"at": now, "adj": adj, "pages": pages})
    return adj, pages


def personalized_pagerank(
    seed_slugs=None,
    *,
    damping: float = 0.85,
    iterations: int = 20,
) -> dict[str, float]:
    """在 wiki 页图上跑 Personalized PageRank；seed_slugs 为空时用均匀分布。"""
    adj, pages = _graph()
    if not pages:
        return {}
    seeds = [slug for slug in (seed_slugs or []) if slug in adj]
    if not seeds:
        seeds = list(pages)
    base = 1.0 / len(seeds)
    personal = {slug: (base if slug in seeds else 0.0) for slug in pages}
    mass = dict(personal)
    teleport = (1.0 - damping) / len(pages)
    for _ in range(max(1, int(iterations))):
        nxt = {slug: teleport + damping * personal[slug] for slug in pages}
        for slug in pages:
            neighbours = adj.get(slug) or {}
            if not neighbours:
                continue
            total = sum(neighbours.values())
            share = mass[slug] * damping / total
            for target, weight in neighbours.items():
                nxt[target] += share * weight
        mass = nxt
    return mass


def ppr_report_boost(seed_paths, keyword_slugs=()) -> dict[str, float]:
    """把页级 PPR 质量摊回报告：返回 {path: 0..1}（归一化后的提升分）。"""
    if not seed_paths and not keyword_slugs:
        return {}
    conn = _connect()
    try:
        seeds = {slug for slug in keyword_slugs if slug}
        for path in seed_paths or ():
            for row in conn.execute(
                "SELECT p.slug FROM wiki_source s JOIN wiki_page p ON p.id = s.page_id"
                " WHERE s.path = ? AND p.state = 'live'",
                (path,),
            ):
                seeds.add(row["slug"])
        if not seeds:
            return {}
        mass = personalized_pagerank(seeds)
        if not mass:
            return {}
        boost: dict[str, float] = {}
        # 按「页覆盖的报告数」摊薄：一张 44 篇报告的主题页，对每篇只能贡献 1/44 的质量，
        # 否则 PPR 会系统性偏向「提到很多实体的长报告」。
        for row in conn.execute(
            "SELECT p.slug, p.report_count, s.path FROM wiki_source s JOIN wiki_page p ON p.id = s.page_id"
            " WHERE p.state = 'live'"
        ):
            weight = mass.get(row["slug"], 0.0)
            if weight:
                share = max(1, int(row["report_count"] or 1))
                boost[row["path"]] = boost.get(row["path"], 0.0) + weight / share
    finally:
        conn.close()
    peak = max(boost.values()) if boost else 0.0
    if peak <= 0:
        return {}
    return {path: value / peak for path, value in boost.items()}


# ---------- P13：LLM Wiki 文章（kb_wiki 写，API 读） ----------


def _article_row(row, sources=None) -> dict:
    item = {
        "slug": row["slug"],
        "title": row["title"],
        "kind": row["kind"],
        "subject": row["subject"],
        "overview": row["overview"],
        "body_md": row["body_md"],
        "source_hash": row["source_hash"],
        "state": row["state"],
        "model": row["model"],
        "takeaway_count": row["takeaway_count"],
        "source_count": row["source_count"],
        "generated_at": row["generated_at"],
        "updated_at": row["updated_at"],
    }
    if sources is not None:
        item["sources"] = sources
    return item


def wiki_article_stats() -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(state='stale') AS stale, "
            "SUM(source_count) AS sources FROM wiki_article"
        ).fetchone()
        jobs = conn.execute(
            "SELECT COUNT(*) AS n FROM wiki_article_job WHERE status IN ('pending','running')"
        ).fetchone()
        return {
            "articles": row["total"] or 0,
            "stale": row["stale"] or 0,
            "sources": row["sources"] or 0,
            "pending_jobs": jobs["n"] or 0,
        }
    finally:
        conn.close()


def wiki_article_list(kind: str = "", q: str = "", limit: int = 50, offset: int = 0) -> dict:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if q:
        clauses.append("(title LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' OR overview LIKE ? ESCAPE '\\')")
        like = f"%{_like(q)}%"
        params += [like, like, like]
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM wiki_article {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM wiki_article {where} ORDER BY source_count DESC, updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "total": total,
            "items": [_article_row(row) for row in rows],
            "limit": limit,
            "offset": offset,
            "mode": "ai",
        }
    finally:
        conn.close()


def _date_from_path(path: str) -> str:
    """报告路径以 20260101/... 开头时，退化为从路径取日期（索引未写 date 的兜底）。"""
    match = re.match(r"^(\d{4})(\d{2})(\d{2})/", str(path or ""))
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def wiki_article_detail(slug: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM wiki_article WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return None
        sources = [
            {
                "n": item["position"],
                "path": item["path"],
                "title": item["title"],
                "date": item["date"] or _date_from_path(item["path"]),
                "url": f"reports/{item['path']}",
            }
            for item in conn.execute(
                "SELECT s.path, s.position, s.title, r.date AS date FROM wiki_article_source s"
                " LEFT JOIN reports r ON r.path = s.path"
                " WHERE s.article_id = ? ORDER BY s.position",
                (row["id"],),
            ).fetchall()
        ]
        related = [
            {
                "slug": item["slug"],
                "title": item["title"],
                "kind": item["kind"],
                "shared": item["shared"],
                "url": f"wiki/{item['slug']}",
            }
            for item in conn.execute(
                "SELECT a.slug, a.title, a.kind, COUNT(*) AS shared"
                " FROM wiki_article_source s1"
                " JOIN wiki_article_source s2 ON s2.path = s1.path AND s2.article_id != s1.article_id"
                " JOIN wiki_article a ON a.id = s2.article_id"
                " WHERE s1.article_id = ?"
                " GROUP BY a.id ORDER BY shared DESC, a.updated_at DESC LIMIT 6",
                (row["id"],),
            ).fetchall()
        ]
        timeline = sorted(
            (s for s in sources if s["date"]),
            key=lambda item: item["date"],
        )
        detail = _article_row(row, sources)
        detail["related"] = related
        detail["timeline"] = timeline
        return detail
    finally:
        conn.close()


def save_wiki_article(
    slug: str,
    *,
    title: str,
    kind: str,
    subject: str,
    overview: str,
    body_md: str,
    source_hash: str,
    sources: list[dict],
    model: str,
    takeaways: int = 0,
) -> str:
    """写入/更新一篇文章（含来源引用）；返回 generated_at。"""
    now = _now()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO wiki_article (slug, title, kind, subject, overview, body_md, source_hash,
                                     state, model, takeaway_count, source_count, generated_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                title=excluded.title, kind=excluded.kind, subject=excluded.subject,
                overview=excluded.overview, body_md=excluded.body_md,
                source_hash=excluded.source_hash, state='live', model=excluded.model,
                takeaway_count=excluded.takeaway_count, source_count=excluded.source_count,
                generated_at=excluded.generated_at, updated_at=excluded.updated_at
            """,
            (slug, title, kind, subject, overview, body_md, source_hash, model,
             takeaways, len(sources), now, now),
        )
        article_id = conn.execute("SELECT id FROM wiki_article WHERE slug = ?", (slug,)).fetchone()["id"]
        conn.execute("DELETE FROM wiki_article_source WHERE article_id = ?", (article_id,))
        conn.executemany(
            "INSERT INTO wiki_article_source (article_id, path, position, title) VALUES (?, ?, ?, ?)",
            [(article_id, item["path"], index + 1, item.get("title") or item["path"])
             for index, item in enumerate(sources)],
        )
        conn.execute(
            "INSERT INTO wiki_article_job (slug, status, attempts, error, created_at, updated_at) "
            "VALUES (?, 'done', 1, '', ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET status='done', attempts=attempts+1, error='', updated_at=excluded.updated_at",
            (slug, now, now),
        )
        conn.commit()
        return now
    finally:
        conn.close()


def mark_wiki_article_stale(slug: str) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE wiki_article SET state='stale', updated_at=? WHERE slug=?", (_now(), slug))
        conn.commit()
    finally:
        conn.close()


def delete_wiki_article(slug: str) -> bool:
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM wiki_article WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM wiki_article_source WHERE article_id = ?", (row["id"],))
        conn.execute("DELETE FROM wiki_article WHERE id = ?", (row["id"],))
        conn.commit()
        return True
    finally:
        conn.close()


def wiki_articles_live() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM wiki_article ORDER BY source_count DESC, updated_at DESC").fetchall()
        return [_article_row(row) for row in rows]
    finally:
        conn.close()


def record_wiki_job(slug: str, status: str, error: str = "") -> None:
    now = _now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO wiki_article_job (slug, status, attempts, error, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET status=excluded.status, attempts=attempts+1, "
            "error=excluded.error, updated_at=excluded.updated_at",
            (slug, status, error[:500], now, now),
        )
        conn.commit()
    finally:
        conn.close()
