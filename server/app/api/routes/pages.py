"""知识库页面与健康检查：`/`、`/kb/`、`/healthz`、`/kb/healthz`。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.config import APP_VERSION, ASSET_VERSION, TEMPLATES_DIR
from app.services import kb_ai, kb_embed, kb_store

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_STARTED_AT = time.time()


@router.get("/")
def kb_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_title": "知识库", "app_version": APP_VERSION, "asset_version": ASSET_VERSION},
        # 页面本身不缓存：静态资源用 ?v=<指纹> 走长缓存，页面取到的一定是新 HTML。
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/healthz")
def healthz():
    """轻量健康检查：进程活着 + 索引可读 + AI/向量能力状态（不触发模型加载）。"""
    try:
        stats = kb_store.overview()
        ok = True
        error = ""
    except Exception as exc:  # 索引损坏/被锁时给出可诊断信息，而不是 500 堆栈
        stats = {"total": 0, "unread": 0, "starred": 0, "public": 0}
        ok = False
        error = f"{type(exc).__name__}: {exc}"[:200]
    body = {
        "ok": ok,
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "reports": stats,
        "ai": {"configured": kb_ai.is_configured()},
        "embed": {
            "enabled": config.EMBED_ENABLED,
            "available": kb_embed.status().get("available", False),
        },
    }
    if error:
        body["error"] = error
    return JSONResponse(body, status_code=200 if ok else 503)
