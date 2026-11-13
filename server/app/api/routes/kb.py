"""知识库接口：列表、层次树、全文检索、报告详情、已读/收藏状态、AI sidecar。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import config
from app.services import (kb_ai, kb_ask, kb_digest as kb_digest_service, kb_embed, kb_enrich,
                          kb_public, kb_search as kb_search_service, kb_store)

router = APIRouter(prefix="/api")

MAX_LIMIT = 500
MAX_ENRICH = 50


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


@router.get("/kb")
def list_kb(
    q: str = "",
    category: str = "",
    series: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    sort: str = "date_desc",
    offset: int = 0,
    limit: int = 0,
    refresh: bool = False,
) -> dict:
    stats = kb_store.sync(force=refresh)
    items, total = kb_store.list_reports(
        q=q,
        category=category,
        series=series,
        date_from=date_from,
        date_to=date_to,
        view=view,
        sort=sort,
        offset=max(0, offset),
        limit=max(0, min(MAX_LIMIT, limit)),
    )
    return {"items": items, "total": total, "scan_total": stats["scan_total"]}


@router.get("/kb/tree")
def kb_tree(
    q: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    view: str = "all",
    refresh: bool = False,
) -> dict:
    stats = kb_store.sync(force=refresh)
    payload = kb_store.tree(
        q=q, category=category, date_from=date_from, date_to=date_to, view=view
    )
    payload["scan_total"] = stats["scan_total"]
    return payload


@router.get("/kb/search")
def kb_search(
    q: str = "",
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
    """检索：默认 auto（有向量就混合排序，否则词法）；可显式 lexical/hybrid。"""
    if not q.strip():
        return {"q": q, "items": [], "total": 0, "mode": "lexical"}
    kb_store.sync()
    result = kb_search_service.search(
        q,
        category=category,
        series=series,
        date_from=date_from,
        date_to=date_to,
        view=view,
        sort=sort,
        offset=max(0, offset),
        limit=max(1, min(100, limit)),
        mode=mode,
    )
    return {"q": q, "items": result["items"], "total": result["total"], "mode": result["mode"]}


@router.get("/kb/embed/stats")
def kb_embed_stats() -> dict:
    status = kb_embed.status()
    status["stats"] = kb_store.embed_stats(config.EMBED_MODEL)
    return status


class KbAskBody(BaseModel):
    question: str = ""
    limit: int = 6


@router.post("/kb/ask")
def kb_ask_question(body: KbAskBody) -> dict:
    """RAG 问答：语义检索 + 模型作答 + 出处（经 DSH AI 桥，无需单独配置密钥）。"""
    if not kb_ai.is_configured():
        return _error(503, "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）")
    return kb_ask.answer(body.question, limit=max(1, min(6, int(body.limit or 6))))


class KbEmbedBody(BaseModel):
    limit: int = 16
    force: bool = False


@router.post("/kb/embed")
def kb_embed_build(body: KbEmbedBody | None = None) -> dict:
    """手动补齐向量（后台线程也会自动补）。"""
    limit = max(1, min(128, int((body.limit if body else None) or config.EMBED_BATCH)))
    return kb_search_service.build_pending(limit)


@router.get("/kb/report/{path:path}")
def kb_report(path: str) -> dict:
    kb_store.sync()
    item = kb_store.get_report(path)
    if item is None:
        return _error(404, "报告不存在或尚未建立索引")
    return {"item": item}


class KbBulkBody(BaseModel):
    paths: list[str] = []
    read: bool | None = None
    starred: bool | None = None
    public: bool | None = None


class KbStateBody(BaseModel):
    path: str
    read: bool | None = None
    starred: bool | None = None


@router.post("/kb/state")
def kb_set_state(body: KbStateBody):
    rel = (body.path or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/") or not rel.lower().endswith((".html", ".htm")):
        return _error(400, "无效的报告路径")
    result = kb_store.set_state(rel, read=body.read, starred=body.starred)
    if result is None:
        return _error(404, "报告不存在或尚未建立索引")
    return result


@router.post("/kb/bulk")
def kb_bulk(body: KbBulkBody):
    """批量标记已读/收藏/公开（最多 500 篇，重复路径自动去重）。"""
    if body.read is None and body.starred is None and body.public is None:
        return _error(400, "没有要更新的字段")
    if not body.paths:
        return _error(400, "paths 不能为空")
    if len(body.paths) > 500:
        return _error(400, "一次最多批量更新 500 篇")
    valid: list[str] = []
    for raw in body.paths:
        rel = (raw or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/") or not rel.lower().endswith((".html", ".htm")):
            continue
        valid.append(rel)
    if not valid:
        return _error(400, "无效的报告路径")
    result = kb_store.bulk_update(
        valid, read=body.read, starred=body.starred, public=body.public
    )
    return {"ok": True, **result}


# ---------- P1：AI sidecar ----------


@router.get("/kb/ai/stats")
def kb_ai_stats() -> dict:
    model = kb_ai.model_label()
    return {"configured": kb_ai.is_configured(), "model": model, **kb_store.ai_stats()}


class KbEnrichBody(BaseModel):
    path: str = ""
    limit: int = 3
    force: bool = False
    retry_failed: bool = False


@router.post("/kb/enrich")
def kb_enrich_reports(body: KbEnrichBody):
    """手动补 AI sidecar（默认小批量，适合前端「生成 AI 摘要」按钮和外部脚本）。"""
    if not kb_ai.is_configured():
        return _error(503, "AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）")
    path = (body.path or "").strip().lstrip("/")
    if path:
        if ".." in path.split("/") or not path.lower().endswith((".html", ".htm")):
            return _error(400, "无效的报告路径")
        return kb_enrich.enrich_report(path, force=body.force)
    limit = max(1, min(MAX_ENRICH, int(body.limit or 3)))
    return kb_enrich.enrich_pending(limit=limit, include_failed=body.retry_failed or body.force)


# ---------- P5：周报 / 主题聚合 ----------


@router.get("/kb/digest")
def kb_digest(days: int = 7, refresh: bool = False) -> dict:
    """窗口内报告的主题聚合 + 模型周报；结果有缓存，refresh=true 重算。"""
    digest = kb_digest_service.build(days=days, refresh=refresh)
    return {"ok": True, **digest}


# ---------- P6：公开报告（对外只读共享） ----------


class KbPublicBody(BaseModel):
    path: str = ""
    public: bool = True


@router.get("/kb/public")
def kb_public_state() -> dict:
    """当前公开集合 + 外链基址，供前端显示「公开」徽标与复制链接。"""
    return {"ok": True, "base": config.PUBLIC_BASE_URL, "paths": sorted(kb_public.flags())}


@router.post("/kb/public")
def kb_set_public(body: KbPublicBody):
    """切换一篇报告的公开状态；只有真实存在的 .html/.htm 报告可以公开。"""
    rel = kb_public.normalize_rel(body.path)
    if rel is None or not kb_public.is_report_rel(rel):
        return _error(400, "无效的报告路径")
    if not kb_public.report_exists(rel):
        return _error(404, "报告不存在")
    try:
        result = kb_public.set_public(rel, bool(body.public))
    except ValueError as exc:
        return _error(409, str(exc))
    return {"ok": True, **result, "url": kb_public.public_url(rel) if body.public else ""}
