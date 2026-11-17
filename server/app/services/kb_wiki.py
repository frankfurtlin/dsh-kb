"""LLM Wiki（P12 与知识图谱拆分）：由 DSH 默认模型撰写的知识长文页。

与知识图谱的分工：
- 图谱（`kb_graph.py`）= 节点 + 共现边 + PPR：回答「谁和谁相关、从 A 能走到哪些报告」；
- 本模块 = AI 撰写的 wiki 文章：回答「这个主题/实体目前我知道什么、各来源怎么说」，
  每篇带来源引用、生成模型与时间，支持增量重写与过期检测。

当前状态（P12 第一步）：占位实现 —— 文章表与生成器在下一阶段接入
（计划 `wiki_article` + `wiki_article_source` + `wiki_article_link` + 生成队列，
后台按 source_hash 增量重写，前端提供「重新生成」）。
"""

from __future__ import annotations

WIKI_VERSION = 1


def stats() -> dict:
    return {
        "version": WIKI_VERSION,
        "mode": "disabled",
        "articles": 0,
        "sources": 0,
        "links": 0,
        "stale": 0,
        "note": "LLM Wiki 文章生成将在下一阶段接入（图谱已独立在「知识图谱」）。",
    }


def article_list(kind: str = "", q: str = "", limit: int = 50, offset: int = 0) -> dict:
    return {"total": 0, "items": [], "limit": limit, "offset": offset, "mode": "disabled"}


def article_detail(slug: str) -> dict | None:
    return None


def lint() -> dict:
    return {"orphans": [], "broken_links": [], "stale": [], "mode": "disabled"}


def build(force: bool = False) -> dict:
    return {"ok": True, "mode": "disabled", "articles": 0, "written": 0, "skipped": 0}
