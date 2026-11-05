"""AI 客户端：经 host 插件的内部 AI 桥使用 DSH 自己的模型（dsh-agent 默认模型）。

- 后端不持有任何模型 API key。每次调用 POST 到 `config.AI_URL`
  （宿主 `dsh-kb` 的 `/kb/_internal/ai`，仅回环可访问），带共享密钥
  `x-kb-ai-secret`（读取 `config.AI_SECRET_FILE`，由 host 半生成，0600）。
- 桥背后是 DSH 的 `llm` 服务 + `agentDefaultModel`，所以模型/额度都跟随
  用户在 DSH 里选中的模型；插件不保存任何第三方模型密钥。
- 只暴露一个同步入口 `chat_json()`：要求模型输出严格 JSON，失败有限次重试。
- 输出解析故意宽松：模型偶尔前后多话，先按宽松 JSON 解析，再退回「第一个 {
  开始的 raw_decode」。
- 不记录密钥，也不在异常信息里回显请求头。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import httpx

from app import config

logger = logging.getLogger("dsh-kb.ai")

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.5, 4.0)
_STATUS_TTL = 60.0

_pace_lock = threading.Lock()
_last_request_at = 0.0
_status_cache: dict | None = None


class KBAIError(RuntimeError):
    """模型调用失败（桥不可用、输出非法等），调用方可据此降级。"""


def _secret() -> str:
    try:
        return config.AI_SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_configured() -> bool:
    """桥密钥存在即视为可用（DSH 侧模型选择在调用时再校验）。"""
    return bool(_secret())


def _headers() -> dict[str, str]:
    return {"content-type": "application/json", "x-kb-ai-secret": _secret()}


def _status_url() -> str:
    base = config.AI_URL
    if base.endswith("/ai"):
        return base + "/status"
    return base.rstrip("/") + "/status"


def status() -> dict:
    """查询 DSH 侧当前默认模型（60s 缓存）；失败返回空信息，不抛异常。"""
    global _status_cache
    now = time.monotonic()
    if _status_cache and now - _status_cache["at"] < _STATUS_TTL:
        return dict(_status_cache["value"])
    value = {"provider": "", "model": "", "mode": "dsh-agent"}
    if is_configured():
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(_status_url(), headers=_headers())
            data = response.json() if response.status_code == 200 else {}
            if isinstance(data, dict) and data.get("ok"):
                value = {
                    "provider": str(data.get("provider", "")),
                    "model": str(data.get("model", "")),
                    "mode": str(data.get("mode", "dsh-agent")),
                }
        except (httpx.HTTPError, ValueError):
            pass
    _status_cache = {"at": now, "value": value}
    return dict(value)


def model_label() -> str:
    """给接口/界面展示的模型标签。"""
    if not is_configured():
        return ""
    info = status()
    if info.get("provider") and info.get("model"):
        return f"{info['provider']}/{info['model']} · {info.get('mode', 'dsh-agent')}"
    return info.get("mode", "dsh-agent")


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return match.group(1).strip() if match else text


def _parse_json_object(content: str) -> dict:
    """尽量从模型输出里抠出一个 JSON 对象。"""
    text = _strip_code_fence(content or "")
    candidates = [text]
    start = text.find("{")
    if start > 0:
        candidates.append(text[start:])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        brace = candidate.find("{")
        if brace >= 0:
            try:
                parsed, _ = json.JSONDecoder(strict=False).raw_decode(candidate[brace:])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
    raise KBAIError("模型输出不是合法 JSON 对象")


def _pace() -> None:
    """进程内最小请求间隔（默认 0：限流由 DSH/模型侧决定）。"""
    global _last_request_at
    interval = config.AI_MIN_INTERVAL
    if interval <= 0:
        return
    with _pace_lock:
        wait = _last_request_at + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def chat_json(
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    temperature: float = 0.1,
) -> dict:
    """经 DSH AI 桥调用模型并解析 JSON 对象；失败抛 KBAIError。"""
    if not is_configured():
        raise KBAIError("AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）")

    payload = {
        "system": system,
        "user": user,
        "max_tokens": max_tokens or config.AI_MAX_TOKENS,
    }
    # 温度只在显式传参时下发：DSH 适配器要求「已准备的调用配置」不变，
    # 多带字段会被判成配置漂移（prepared LLM call config changed）。
    if temperature is not None and temperature != 0.1:
        payload["temperature"] = temperature

    last_error = ""
    for attempt in range(_MAX_ATTEMPTS):
        _pace()
        try:
            with httpx.Client(timeout=config.AI_TIMEOUT) as client:
                response = client.post(config.AI_URL, json=payload, headers=_headers())
            if response.status_code == 404:
                raise KBAIError("AI 桥未就绪：DSH 插件未加载或密钥不匹配")
            if response.status_code >= 500:
                last_error = f"AI 桥返回 HTTP {response.status_code}"
                raise _Retryable(last_error)
            if response.status_code != 200:
                raise KBAIError(f"AI 桥返回 HTTP {response.status_code}：{response.text[:200]}")
            data = response.json()
            if not data.get("ok"):
                # 模型侧失败（超时/空输出/未配置模型）：重试一次仍失败就如实上报。
                last_error = str(data.get("error") or "AI 桥调用失败")[:200]
                raise _Retryable(last_error)
            parsed = _parse_json_object(str(data.get("text") or ""))
            return parsed
        except _Retryable as exc:
            last_error = str(exc)
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
        except (httpx.HTTPError, ValueError, KBAIError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:200]
            if isinstance(exc, KBAIError) and "未就绪" in str(exc):
                break
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
    logger.warning("模型调用失败（经 DSH AI 桥，已重试 %s 次）：%s", _MAX_ATTEMPTS - 1, last_error)
    raise KBAIError(last_error or "模型调用失败")


class _Retryable(Exception):
    """内部标记：本次失败可重试。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
