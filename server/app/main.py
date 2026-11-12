"""dsh-kb 的裁剪版 FastAPI 应用（知识库报告浏览服务）。

同一套路由注册两遍，供两种访问方式复用同一份实现：

- 直连后端（`http://127.0.0.1:8916/`）：`/`、`/api/kb`、`/static/*`、`/reports/*`
- DSH 反代 / iframe（`/kb/`）：`/kb/`、`/kb/api/kb`、`/kb/static/*`、`/kb/reports/*`

这样 `lib/index.js` 的 `/kb` 反代可以原样转发 `req.url`（不剥前缀），而验收脚本
直连 8916 时用 `/kb/` 打开页面就等于复现 iframe 内的相对路径环境。

启动（在 server/ 目录下）：

    /usr/bin/python3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8816
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config
from app.api.routes import kb as kb_routes
from app.api.routes import pages as page_routes
from app.api.routes import public_site as public_routes
from app.api.routes import reports as report_routes
from app.config import ROUTE_PREFIX, STATIC_DIR
from app.services import kb_ai, kb_embed, kb_enrich, kb_search, kb_store

logger = logging.getLogger("dsh-kb")


def _enrich_worker(stop_event: threading.Event) -> None:
    """低频后台补全 AI sidecar：每轮取一小批 pending，失败自动记录并跳过。"""
    while not stop_event.wait(config.AI_INTERVAL):
        try:
            result = kb_enrich.enrich_pending(limit=config.AI_BATCH)
            if result.get("enriched") or result.get("failed"):
                logger.info(
                    "AI sidecar：enriched=%s failed=%s remaining=%s",
                    result.get("enriched"),
                    result.get("failed"),
                    result.get("remaining"),
                )
        except Exception:  # 后台线程不能把服务带崩
            logger.warning("AI sidecar 后台补全异常", exc_info=True)


def _embed_worker(stop_event: threading.Event) -> None:
    """把还没有向量的报告补齐（本地 ONNX 推理，逐批进行，重启后从断点继续）。"""
    model = config.EMBED_MODEL
    while not stop_event.is_set():
        try:
            stats = kb_store.embed_stats(model)
            if stats["pending"] <= 0:
                logger.info("向量索引已完整：%s/%s", stats["embedded"], stats["total"])
                return
            result = kb_search.build_pending(config.EMBED_BATCH)
            if not result.get("ok") or not result.get("embedded"):
                logger.warning("向量补齐未产生进展：%s", result)
                return
        except Exception:  # 后台线程不能把服务带崩
            logger.warning("向量补齐异常", exc_info=True)
            return
        stop_event.wait(config.EMBED_INTERVAL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时增量同步一次索引（首次会读全部报告，之后只读变化的文件）。
    kb_store.sync()
    stop_event = threading.Event()
    worker = None
    workers: list[threading.Thread] = []
    if config.AI_AUTO and kb_ai.is_configured():
        worker = threading.Thread(target=_enrich_worker, args=(stop_event,), name="kb-enrich", daemon=True)
        worker.start()
        workers.append(worker)
        logger.info("AI sidecar 后台补全已启动（每 %.0fs 处理 %s 篇）", config.AI_INTERVAL, config.AI_BATCH)
    if config.EMBED_ENABLED and kb_embed.available():
        embedder = threading.Thread(target=_embed_worker, args=(stop_event,), name="kb-embed", daemon=True)
        embedder.start()
        workers.append(embedder)
    else:
        logger.warning("向量索引未启用或不可用：%s", kb_embed.status().get("error") or "disabled")
    try:
        yield
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=3)


app = FastAPI(
    title="dsh-kb",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# 静态资源两份挂载（先于路由注册，避免被前缀路由吞掉）。
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="kb-static-root")
app.mount(f"{ROUTE_PREFIX}/static", StaticFiles(directory=str(STATIC_DIR)), name="kb-static-prefixed")

for prefix in ("", ROUTE_PREFIX):
    app.include_router(page_routes.router, prefix=prefix)
    app.include_router(kb_routes.router, prefix=prefix)
    app.include_router(report_routes.router, prefix=prefix)

# 公开报告站点：只注册一次（/kb-public/... 绝对路径），不随 ROUTE_PREFIX 复制，
# 因为它由 Caddy 直接访问，不经过 DSH 前缀。
app.include_router(public_routes.router)
