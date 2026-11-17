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
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.api.routes import kb as kb_routes
from app.api.routes import pages as page_routes
from app.api.routes import public_site as public_routes
from app.api.routes import wiki as wiki_routes
from app.api.routes import reports as report_routes
from app.config import ROUTE_PREFIX, STATIC_DIR
from app.services import kb_ai, kb_classify, kb_embed, kb_enrich, kb_search, kb_store, kb_wiki

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


def _classify_worker(stop_event: threading.Event) -> None:
    """低频后台补分类：每轮取一小批 pending，失败记录后跳过，下一轮重试。"""
    while not stop_event.wait(config.CLASSIFY_INTERVAL):
        try:
            result = kb_classify.classify_pending(limit=config.CLASSIFY_BATCH)
            if result.get("ok") and (result.get("classified") or result.get("failed")):
                logger.info(
                    "AI 分类：本批 %s 篇，失败 %s，剩余 %s",
                    result.get("classified"),
                    result.get("failed"),
                    result.get("remaining"),
                )
        except Exception as exc:  # 后台线程不因单次失败退出
            logger.warning("AI 分类批次失败：%s", exc)


def _embed_worker(stop_event: threading.Event) -> None:
    """把还没有向量的报告补齐（本地 ONNX 推理，逐批进行，重启后从断点继续）。"""
    model = config.EMBED_MODEL
    while not stop_event.is_set():
        try:
            stats = kb_store.embed_stats(model)
            wiki = kb_store.wiki_embed_stats(model)
            if stats["pending"] <= 0 and wiki["pending"] <= 0:
                logger.info(
                    "向量索引已完整：报告 %s/%s，wiki %s/%s",
                    stats["embedded"], stats["total"], wiki["embedded"], wiki["total"],
                )
                return
            result = (
                kb_search.build_pending(config.EMBED_BATCH)
                if stats["pending"] > 0
                else kb_search.build_pending_wiki(config.EMBED_BATCH)
            )
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
    # P10：按内容 hash 增量重编 wiki（hash 命中的页直接跳过写盘）。
    try:
        summary = kb_wiki.build()
        logger.info("Wiki 编译完成：%s 页（写 %s / 跳过 %s）", summary.get("pages"),
                    summary.get("written"), summary.get("skipped"))
    except Exception:  # 编译失败不影响检索与浏览
        logger.warning("Wiki 编译失败", exc_info=True)
    stop_event = threading.Event()
    worker = None
    workers: list[threading.Thread] = []
    if config.AI_AUTO and kb_ai.is_configured():
        worker = threading.Thread(target=_enrich_worker, args=(stop_event,), name="kb-enrich", daemon=True)
        worker.start()
        workers.append(worker)
        logger.info("AI sidecar 后台补全已启动（每 %.0fs 处理 %s 篇）", config.AI_INTERVAL, config.AI_BATCH)
    if config.CLASSIFY_AUTO and kb_ai.is_configured():
        classifier = threading.Thread(
            target=_classify_worker, args=(stop_event,), name="kb-classify", daemon=True
        )
        classifier.start()
        workers.append(classifier)
        logger.info(
            "AI 分类后台补全已启动（每 %.0fs 处理 %s 篇）",
            config.CLASSIFY_INTERVAL,
            config.CLASSIFY_BATCH,
        )
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

# 大响应压缩（kb.js 33KB、报告正文/JSON 列表），跳过小响应。
app.add_middleware(GZipMiddleware, minimum_size=500)


# 慢请求阈值：超过就记一条 warning 日志，给排查「面板卡住」留线索。
SLOW_REQUEST_MS = float(os.environ.get("KB_SLOW_REQUEST_MS", "300"))
http_logger = logging.getLogger("dsh-kb.http")


@app.middleware("http")
async def process_time_header(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Process-Time"] = f"{elapsed_ms:.1f}"
    if elapsed_ms >= SLOW_REQUEST_MS:
        http_logger.warning(
            "[slow] %s %s %s %.1fms", request.method, request.url.path, response.status_code, elapsed_ms
        )
    return response


# 静态资源缓存策略：模板里的 URL 带 ?v=<ASSET_VERSION> 指纹，命中长缓存；
# 不带指纹的（直连调试、旧链接）只缓存 5 分钟，避免升级后长期拿到旧 JS/CSS。
_STATIC_PREFIXES = ("/static/", f"{ROUTE_PREFIX}/static/")


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _STATIC_PREFIXES):
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=300"
    return response


# 统一错误体：框架自产的 404/405/422 与路由里的 HTTPException 都收敛成
# {"ok": false, "error": "..."}，前端只解析一种形状（老站 errors.register 的做法）。
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else "请求失败"
    return JSONResponse({"ok": False, "error": detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(part) for part in first.get("loc", ())[1:]) or "参数"
    return JSONResponse(
        {"ok": False, "error": f"参数不合法：{where}"},
        status_code=422,
    )


# 静态资源两份挂载（先于路由注册，避免被前缀路由吞掉）。
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="kb-static-root")
app.mount(f"{ROUTE_PREFIX}/static", StaticFiles(directory=str(STATIC_DIR)), name="kb-static-prefixed")

for prefix in ("", ROUTE_PREFIX):
    app.include_router(page_routes.router, prefix=prefix)
    app.include_router(kb_routes.router, prefix=prefix)
    app.include_router(report_routes.router, prefix=prefix)
    app.include_router(wiki_routes.router, prefix=prefix)

# 公开报告站点：只注册一次（/kb-public/... 绝对路径），不随 ROUTE_PREFIX 复制，
# 因为它由 Caddy 直接访问，不经过 DSH 前缀。
app.include_router(public_routes.router)
