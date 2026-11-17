"""P2：混合检索编排（语义向量 + 词法），以及向量索引的补齐。

- `build_pending()`：给还没有当前模型向量的报告补向量（批量）；
- `search()`：mode=hybrid/auto 时走向量 + 词法混合排序，模型/向量不可用时
  自动降级为纯词法检索（行为与 P1 一致，接口不报错）。
"""

from __future__ import annotations

import logging

from app import config
from app.services import kb_embed, kb_store

logger = logging.getLogger("dsh-kb.search")


def build_pending(limit: int | None = None) -> dict:
    model = config.EMBED_MODEL
    if not kb_embed.available():
        return {"ok": False, "error": kb_embed.status().get("error") or "embedding 不可用", "embedded": 0}
    items = kb_store.next_pending_embedding(limit or config.EMBED_BATCH, model=model)
    if not items:
        return {"ok": True, "embedded": 0, **kb_store.embed_stats(model)}
    texts = [kb_embed.document_text(item) for item in items]
    vectors = kb_embed.embed_texts(texts)
    for item, vector in zip(items, vectors):
        kb_store.set_embedding(item["path"], vector, model)
    stats = kb_store.embed_stats(model)
    logger.info("向量补齐 %s 篇：%s/%s", len(items), stats["embedded"], stats["total"])
    return {"ok": True, "embedded": len(items), **stats}


def build_pending_wiki(limit: int | None = None) -> dict:
    """给 wiki 页补向量（让 wiki 页也能进混合检索）。"""
    model = config.EMBED_MODEL
    if not kb_embed.available():
        return {"ok": False, "embedded": 0}
    items = kb_store.next_pending_wiki_embedding(limit or config.EMBED_BATCH, model=model)
    if not items:
        return {"ok": True, "embedded": 0, **kb_store.wiki_embed_stats(model)}
    vectors = kb_embed.embed_texts([kb_embed.wiki_document_text(item) for item in items])
    for item, vector in zip(items, vectors):
        kb_store.set_wiki_embedding(item["slug"], vector, model)
    stats = kb_store.wiki_embed_stats(model)
    logger.info("wiki 向量补齐 %s 页：%s/%s", len(items), stats["embedded"], stats["total"])
    return {"ok": True, "embedded": len(items), **stats}


def search(
    q: str,
    *,
    category: str = "",
    series: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    sort: str = "",
    offset: int = 0,
    limit: int = 20,
    mode: str = "auto",
) -> dict:
    keyword = (q or "").strip()
    requested = (mode or "auto").strip().lower()
    use_hybrid = requested in ("auto", "hybrid") and bool(keyword) and kb_embed.available()
    if use_hybrid:
        try:
            query_vector = kb_embed.embed_query(keyword)
            items, total = kb_store.hybrid_search(
                keyword,
                query_vector=query_vector,
                model=config.EMBED_MODEL,
                category=category,
                series=series,
                date_from=date_from,
                date_to=date_to,
                view=view,
                offset=offset,
                limit=limit,
                semantic_weight=config.HYBRID_WEIGHT_SEMANTIC,
                lexical_weight=config.HYBRID_WEIGHT_LEXICAL,
                include_wiki=not any((category, series, date_from, date_to)) and view == "all",
                ppr_weight=config.HYBRID_WEIGHT_PPR,
            )
            # 向量还没补齐（例如刚重启）时，语义结果可能为空：退回词法。
            if items or total == 0:
                return {"mode": "hybrid", "items": items, "total": total}
        except Exception as exc:  # 语义链路故障不影响可用性
            logger.warning("混合检索失败，降级为词法检索：%s", exc)
    items, total = kb_store.list_reports(
        q=keyword,
        category=category,
        series=series,
        date_from=date_from,
        date_to=date_to,
        view=view,
        sort=sort or "date_desc",
        offset=offset,
        limit=limit,
    )
    return {"mode": "lexical", "items": items, "total": total}
