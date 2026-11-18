"""LLM Wiki（P12/P13）：由 DSH 默认模型撰写的知识长文，与知识图谱分层。

分工：
- 图谱（`kb_graph.py`）= 节点 + 共现边 + PPR：回答「谁和谁相关、从 A 能走到哪些报告」；
- 本模块 = AI 撰写的文章：回答「这个主题/实体目前我知道什么、各来源怎么说」。
  每篇带 [n] 来源引用、生成模型与时间、来源 hash（用于增量重写），并支持 lint。

数据：
- `wiki_article`（一篇文章一行）、`wiki_article_source`（[n] → 报告路径）、
  `wiki_article_job`（生成队列）；
- markdown 同时写到 `WIKI_ARTICLES_DIR`（默认 `<插件根>/wiki/`），可直接当 Obsidian vault 打开。

生成策略（按用户选择）：后台低频增量 + 手动「重新生成」。
候选来自标签覆盖数 ≥ `WIKI_MIN_REPORTS` 的主题，以及 AI 实体聚合同样的阈值；
每篇最多引用 `WIKI_MAX_SOURCES` 篇报告，来源摘要按 `WIKI_SOURCE_CHARS` 截断。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from app import config
from app.services import kb_ai, kb_store

logger = logging.getLogger("dsh-kb.wiki")

WIKI_VERSION = 1
_SYSTEM = (
    "你是知识库编辑，负责把多篇来源报告综合成一篇中文 Wiki 文章。"
    "只输出 JSON 对象，字段："
    '{"title": "文章标题",'
    ' "overview": "150-250 字的总览，概括这批来源的核心结论",'
    ' "sections": [{"heading": "小节标题", "body": "150-400 字，引用来源处写 [n]"}],'
    ' "takeaways": ["3-5 条关键结论"]}。'
    "要求：只使用给定来源里的事实与数字，不要编造；[n] 必须对应给定来源编号；"
    "小节 2-5 个；语言简洁、面向决策，不要空话。"
)


class WikiError(RuntimeError):
    """文章生成失败（候选不存在、模型不可用、输出非法）。"""


# ---------- 候选 ----------


def _slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[\s/\\]+", "-", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fa5._-]+", "", text)
    return text.strip("-.") or "untitled"


def _candidates() -> list[dict]:
    """按覆盖报告数生成候选：主题（标签）与实体（AI 抽取）两类。"""
    conn = kb_store.connect()
    try:
        topic_rows = conn.execute(
            """
            SELECT t.name AS name, COUNT(*) AS n, MAX(r.date) AS latest
            FROM report_tags rt
            JOIN tags t ON t.id = rt.tag_id
            JOIN reports r ON r.id = rt.report_id
            GROUP BY t.name HAVING n >= ?
            ORDER BY n DESC, name
            """,
            (config.WIKI_MIN_REPORTS,),
        ).fetchall()
        report_rows = conn.execute(
            "SELECT path, title, date, category, summary, summary_ai, entities, body FROM reports"
        ).fetchall()
    finally:
        conn.close()

    entities: dict[str, dict] = {}
    for row in report_rows:
        try:
            items = json.loads(row["entities"] or "[]")
        except ValueError:
            items = []
        for item in items if isinstance(items, list) else []:
            name = (item.get("name") if isinstance(item, dict) else str(item)) or ""
            name = re.sub(r"\s+", " ", name).strip()
            if len(name) < 2:
                continue
            slot = entities.setdefault(name, {"name": name, "paths": [], "latest": ""})
            if row["path"] not in slot["paths"]:
                slot["paths"].append(row["path"])
            if row["date"] and row["date"] > slot["latest"]:
                slot["latest"] = row["date"]

    candidates = [
        {
            "slug": f"topic-{_slugify(row['name'])}",
            "kind": "topic",
            "subject": row["name"],
            "count": row["n"],
            "latest": row["latest"] or "",
        }
        for row in topic_rows
        if len(row["name"]) >= 2
    ]
    candidates += [
        {
            "slug": f"entity-{_slugify(item['name'])}",
            "kind": "entity",
            "subject": item["name"],
            "count": len(item["paths"]),
            "latest": item["latest"],
        }
        for item in entities.values()
        if len(item["paths"]) >= config.WIKI_MIN_REPORTS
    ]
    candidates.sort(key=lambda c: (-c["count"], c["subject"]))
    return candidates


def candidates(limit: int = 20) -> list[dict]:
    return _candidates()[: max(1, limit)]


def _candidate(slug: str) -> dict | None:
    for item in _candidates():
        if item["slug"] == slug:
            return item
    return None


# ---------- 来源 ----------


def _source_rows(candidate: dict) -> list[dict]:
    conn = kb_store.connect()
    try:
        if candidate["kind"] == "topic":
            rows = conn.execute(
                """
                SELECT r.path, r.title, r.date, r.category, r.summary, r.summary_ai, r.entities,
                       r.body, r.content_hash
                FROM report_tags rt
                JOIN tags t ON t.id = rt.tag_id
                JOIN reports r ON r.id = rt.report_id
                WHERE t.name = ?
                ORDER BY r.date DESC, r.path
                LIMIT ?
                """,
                (candidate["subject"], config.WIKI_MAX_SOURCES),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, title, date, category, summary, summary_ai, entities, body, content_hash "
                "FROM reports ORDER BY date DESC, path"
            ).fetchall()
    finally:
        conn.close()

    result = []
    needle = candidate["subject"]
    for row in rows:
        if candidate["kind"] == "entity":
            try:
                items = json.loads(row["entities"] or "[]")
            except ValueError:
                items = []
            names = {
                re.sub(r"\s+", " ", (item.get("name") if isinstance(item, dict) else str(item)) or "").strip()
                for item in (items if isinstance(items, list) else [])
            }
            if needle not in names:
                continue
        result.append(
            {
                "path": row["path"],
                "title": row["title"],
                "date": row["date"],
                "category": row["category"],
                "text": (row["summary_ai"] or row["summary"] or row["body"] or "")[
                    : config.WIKI_SOURCE_CHARS
                ],
                "content_hash": row["content_hash"] or "",
            }
        )
        if len(result) >= config.WIKI_MAX_SOURCES:
            break
    return result


def _source_hash(sources: list[dict]) -> str:
    payload = "\n".join(f"{src['path']}:{src['content_hash'] or src['title']}" for src in sources)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------- 生成 ----------


def _build_prompt(candidate: dict, sources: list[dict]) -> str:
    lines = [
        f"主题：{candidate['subject']}",
        f"类型：{'标签主题' if candidate['kind'] == 'topic' else '实体'}，共 {len(sources)} 篇来源报告。",
        "",
        "来源列表：",
    ]
    for index, src in enumerate(sources, start=1):
        lines.append(f"[{index}] {src['title']}（{src['date'] or '无日期'} · {src['category']}）")
        if src["text"]:
            lines.append(src["text"].replace("\n", " ")[: config.WIKI_SOURCE_CHARS])
        lines.append("")
    return "\n".join(lines)


def _parse_article(candidate: dict, payload: dict, model: str, now: str) -> dict:
    title = str(payload.get("title") or "").strip() or candidate["subject"]
    overview = str(payload.get("overview") or "").strip()
    sections = []
    for item in (payload.get("sections") or []) if isinstance(payload.get("sections") or [], list) else []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or "").strip()
        body = str(item.get("body") or "").strip()
        if heading and body:
            sections.append({"heading": heading[:80], "body": body})
    takeaways = [
        str(item).strip()
        for item in (payload.get("takeaways") or [])
        if str(item).strip()
    ][:6]
    if not overview and not sections:
        raise WikiError("模型输出缺少 overview 与 sections")

    meta = (
        f"> LLM Wiki · {'标签主题' if candidate['kind'] == 'topic' else '实体'}"
        f"「{candidate['subject']}」 · {len(sections)} 节 · 模型 {model or '未知'} · {now[:10]}"
    )
    parts = [f"# {title}", "", meta, "", overview]
    for section in sections:
        parts += ["", f"## {section['heading']}", "", section["body"]]
    if takeaways:
        parts += ["", "## 关键结论", ""]
        parts += [f"- {item}" for item in takeaways]
    body_md = "\n".join(parts).strip() + "\n"
    return {
        "title": title[:120],
        "overview": overview[:2000],
        "body_md": body_md,
        "sections": sections,
        "takeaways": takeaways,
    }


def generate(slug: str, force: bool = False) -> dict:
    """生成/重写一篇文章。返回 {ok, slug, skipped, ...}。"""
    candidate = _candidate(slug)
    if not candidate:
        raise WikiError(f"没有可达标的候选：{slug}（阈值 {config.WIKI_MIN_REPORTS} 篇）")
    if not kb_ai.is_configured():
        raise WikiError("AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）")
    sources = _source_rows(candidate)
    if len(sources) < config.WIKI_MIN_REPORTS:
        raise WikiError(f"来源不足：{len(sources)} 篇（阈值 {config.WIKI_MIN_REPORTS}）")
    source_hash = _source_hash(sources)

    existing = kb_store.wiki_article_detail(slug)
    if existing and not force and existing.get("source_hash") == source_hash and existing.get("state") == "live":
        return {"ok": True, "slug": slug, "skipped": True, "reason": "hash 命中", "title": existing["title"]}

    kb_store.record_wiki_job(slug, "running")

    def _call(sources_in: list[dict], max_tokens: int) -> dict:
        return kb_ai.chat_json(
            _SYSTEM,
            _build_prompt(candidate, sources_in),
            max_tokens=max_tokens,
        )

    try:
        try:
            payload = _call(sources, config.WIKI_MAX_TOKENS)
        except Exception as first_error:  # 推理模型偶尔把 token 全花在思考上 → 加预算 + 缩来源重试一次
            logger.warning("wiki 首次生成失败（%s），加大 token 预算重试", first_error)
            payload = _call(sources[: min(6, len(sources))], max(config.WIKI_MAX_TOKENS * 2, 4000))
        now = kb_store._now()
        model = kb_ai.model_label()
        article = _parse_article(candidate, payload, model, now)
        dated = sorted((src for src in sources if src.get("date")), key=lambda src: src["date"])
        if len(dated) >= 2:
            lines = ["", "## 时间线", ""]
            lines += [f"- {src['date']} · {src['title']}" for src in dated]
            article["body_md"] = article["body_md"].rstrip() + "\n" + "\n".join(lines) + "\n"
        kb_store.save_wiki_article(
            slug,
            title=article["title"],
            kind=candidate["kind"],
            subject=candidate["subject"],
            overview=article["overview"],
            body_md=article["body_md"],
            source_hash=source_hash,
            sources=sources,
            model=model,
            takeaways=len(article["takeaways"]),
        )
        _write_markdown(slug, article, sources)
        logger.info("wiki 文章已生成：%s（%s 篇来源，%s 节）", slug, len(sources), len(article["sections"]))
        return {
            "ok": True,
            "slug": slug,
            "title": article["title"],
            "sources": len(sources),
            "sections": len(article["sections"]),
            "takeaways": len(article["takeaways"]),
            "model": model,
        }
    except Exception as exc:  # 记录失败，交给下一轮重试
        kb_store.record_wiki_job(slug, "failed", f"{type(exc).__name__}: {exc}")
        logger.warning("wiki 文章生成失败 %s：%s", slug, exc)
        raise WikiError(str(exc)) from exc


def _write_markdown(slug: str, article: dict, sources: list[dict]) -> None:
    pages = Path(config.WIKI_DIR) / "articles"
    pages.mkdir(parents=True, exist_ok=True)
    lines = [article["body_md"].strip(), "", "## 来源", ""]
    for index, src in enumerate(sources, start=1):
        lines.append(
            f"{index}. [{src['title']}](reports/{src['path']}) — {src['date'] or '无日期'} · {src['category']}"
        )
    (pages / f"{slug}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_index()


def _write_index() -> None:
    stats_row = kb_store.wiki_article_stats()
    listing = kb_store.wiki_article_list(limit=200)
    lines = [
        "# LLM Wiki",
        "",
        f"> {stats_row['articles']} 篇文章 · 来源 {stats_row['sources']} 条 · 过期 {stats_row['stale']} 篇",
        "",
    ]
    for item in listing["items"]:
        lines.append(
            f"- [{item['title']}](articles/{item['slug']}.md) — {item['source_count']} 篇来源 · {item['state']}"
        )
    Path(config.WIKI_DIR).mkdir(parents=True, exist_ok=True)
    (Path(config.WIKI_DIR) / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_pending(limit: int = 1, force: bool = False) -> dict:
    """后台增量：为还没有文章（或 hash 变化）的候选补写，最多 limit 篇。"""
    done, skipped, failed = [], 0, []
    for candidate in _candidates():
        if len(done) >= limit:
            break
        existing = kb_store.wiki_article_detail(candidate["slug"])
        if existing and not force and existing.get("state") == "live":
            if existing.get("source_hash") == _source_hash(_source_rows(candidate)):
                skipped += 1
                continue
        try:
            result = generate(candidate["slug"], force=force)
        except WikiError as exc:
            failed.append({"slug": candidate["slug"], "error": str(exc)})
            continue
        if result.get("skipped"):
            skipped += 1
        else:
            done.append(result)
    return {"ok": True, "generated": done, "skipped": skipped, "failed": failed, "stats": stats()}


# ---------- 读取/体检 ----------


def stats() -> dict:
    raw = kb_store.wiki_article_stats()
    configured = kb_ai.is_configured()
    return {
        "version": WIKI_VERSION,
        "mode": "ai" if configured else "disabled",
        "articles": raw["articles"],
        "sources": raw["sources"],
        "stale": raw["stale"],
        "pending_jobs": raw["pending_jobs"],
        "candidates": len(_candidates()),
        "model": kb_ai.model_label() if configured else "",
        "note": "" if configured else "缺少 DSH AI 桥密钥，文章生成不可用。",
    }


def article_list(kind: str = "", q: str = "", limit: int = 50, offset: int = 0) -> dict:
    result = kb_store.wiki_article_list(kind=kind, q=q, limit=limit, offset=offset)
    result["mode"] = "ai" if kb_ai.is_configured() else "disabled"
    return result


def article_detail(slug: str) -> dict | None:
    return kb_store.wiki_article_detail(slug)


def lint() -> dict:
    stale, empty, missing = [], [], []
    for article in kb_store.wiki_articles_live():
        detail = kb_store.wiki_article_detail(article["slug"]) or {}
        sources = detail.get("sources") or []
        if not sources:
            empty.append(article["slug"])
        for src in sources:
            if not (Path(config.REPORTS_DIR) / src["path"]).exists():
                missing.append({"slug": article["slug"], "path": src["path"]})
        candidate = _candidate(article["slug"])
        if not candidate:
            stale.append({"slug": article["slug"], "reason": "候选已不可达（来源减少）"})
        elif sources and article.get("source_hash") != _source_hash(_source_rows(candidate)):
            stale.append({"slug": article["slug"], "reason": "来源内容已变化"})
    return {
        "mode": "ai" if kb_ai.is_configured() else "disabled",
        "stale": stale,
        "empty": empty,
        "missing_sources": missing,
        "checked": len(kb_store.wiki_articles_live()),
    }
