"""P2：本地语义向量（fastembed + ONNX，模型在 data/models 下，不依赖外部 API）。

为什么用本地 embedding：在线 embedding 需要额外的第三方 key/额度，而
460 篇报告的向量用本地小模型（bge-small-zh-v1.5，512 维）在 CPU 上建一次只要几十秒。

设计要点：
- 懒加载：只有第一次真正需要向量时才 import fastembed / 加载模型，系统 Python
  没装 fastembed 时整体降级（available() 返回 False），知识库照常可用。
- 模型/HF 缓存都在插件 data/ 下，避免沙箱外写权限问题；可用 KB_EMBED_* 覆盖。
- 文档向量文本 = 标题 + AI 摘要（没有则首个段落摘要）+ 标签 + 正文前
  EMBED_BODY_LIMIT 字；查询向量只用问题本身。
"""

from __future__ import annotations

import logging
import os
import threading

from app import config

logger = logging.getLogger("dsh-kb.embed")

_model = None
_model_error = ""
_model_lock = threading.Lock()


def _prepare_env() -> None:
    """把 HF/HF-Xet 缓存限制在插件 data/ 内（沙箱只允许写 workspace）。"""
    os.environ.setdefault("HF_HOME", config.EMBED_HF_HOME)
    os.environ.setdefault("XDG_CACHE_HOME", config.EMBED_HF_HOME)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    if config.EMBED_HF_ENDPOINT:
        os.environ.setdefault("HF_ENDPOINT", config.EMBED_HF_ENDPOINT)


def _load_model():
    global _model, _model_error
    with _model_lock:
        if _model is not None:
            return _model
        if not config.EMBED_ENABLED:
            _model_error = "KB_EMBED_ENABLED=0"
            return None
        _prepare_env()
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(
                model_name=config.EMBED_MODEL,
                cache_dir=config.EMBED_MODEL_DIR,
                threads=min(4, os.cpu_count() or 2),
            )
            _model_error = ""
            logger.info("embedding 模型已加载：%s", config.EMBED_MODEL)
        except Exception as exc:  # 缺依赖/模型缺失：降级而不是让服务起不来
            _model_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("embedding 不可用（%s），语义检索将降级为关键词检索", _model_error)
            _model = None
        return _model


def available() -> bool:
    """是否可用（会触发一次懒加载，只在该进程首次调用时慢）。"""
    return _load_model() is not None


def status() -> dict:
    return {
        "enabled": config.EMBED_ENABLED,
        "available": _model is not None,
        "model": config.EMBED_MODEL,
        "dim": DIM,
        "error": _model_error,
    }


# bge-small-zh-v1.5 输出 512 维；其他模型首次嵌入时再校正。
DIM = 512


def embed_texts(texts: list[str]) -> list[list[float]]:
    """把一批文本编码成归一化向量（float 列表）；不可用时抛 RuntimeError。"""
    model = _load_model()
    if model is None:
        raise RuntimeError(f"embedding 不可用：{_model_error or 'unknown'}")
    vectors = []
    for vector in model.embed(texts):
        values = [float(v) for v in vector]
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        vectors.append([v / norm for v in values])
    return vectors


def embed_query(text: str) -> list[float]:
    vectors = embed_texts([text or ""])
    return vectors[0] if vectors else []


def wiki_document_text(page: dict) -> str:
    """wiki 页送入模型的文本：标题 + 实体类型 + 页正文（markdown 去掉标记后截断）。"""
    body = str(page.get("body_md") or "")
    body = body.split("---", 2)[-1] if body.startswith("---") else body
    body = body.replace("#", " ").replace("[[", " ").replace("]]", " ")
    return "\n".join(part for part in [page.get("title") or "",
                                       page.get("entity_type") or "",
                                       body[: config.EMBED_BODY_LIMIT]] if part)


def document_text(item: dict) -> str:
    """送入模型的文档文本：标题 + 摘要 + 标签 + 正文前若干字。"""
    summary = item.get("summary_ai") or item.get("summary") or ""
    tags = " ".join(item.get("tags") or [])
    body = (item.get("body") or "")[: config.EMBED_BODY_LIMIT]
    parts = [item.get("title") or "", summary, tags, body]
    return "\n".join(part for part in parts if part)
