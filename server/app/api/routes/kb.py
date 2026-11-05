"""知识库接口：列表、层次树、全文检索、报告详情、已读/收藏状态、AI sidecar。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import config
from app.services import kb_ai, kb_ask, kb_digest as kb_digest_service, kb_embed, kb_enrich, kb_search as kb_search_service, kb_store

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
def kb_tree(refresh: bool = False) -> dict:
    stats = kb_store.sync(force=refresh)
    payload = kb_store.tree()
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
