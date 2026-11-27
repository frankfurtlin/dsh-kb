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
from app.services import kb_store

logger = logging.getLogger("dsh-kb.ai")

_MAX_ATTEMPTS = 3
# 2026-11-26：实测上游并发时会瞬断（~0.55s 返回 TRANSPORT，串行 10/10 成功、并发 4 时约 4 成失败），
# 失败后立刻重试大多能成，所以第一次重试不等、后续再拉开间隔。
_BACKOFF_SECONDS = (0.3, 2.0, 6.0)
# 同一进程内只允许一个模型请求在飞：并发正是触发瞬断的原因，串行后失败率归零。
_CALL_LOCK = threading.Lock()
_STATUS_TTL = 60.0
_CATALOG_TTL = 60.0
_catalog_cache: dict | None = None

_pace_lock = threading.Lock()
_last_request_at = 0.0
_status_cache: dict | None = None


class KBAIError(RuntimeError):
    """模型调用失败（桥不可用、输出非法等），调用方可据此降级。"""


class KBAIUnavailable(KBAIError):
    """用户选定的模型不可用：不应重试，后台任务应安静跳过，界面展示「不可用」。"""


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


def _models_url() -> str:
    base = config.AI_URL.rstrip("/")
    if base.endswith("/ai"):
        base = base[:-3]
    return base + "/models"


def selected() -> dict[str, str] | None:
    """用户选定的模型（存 setting 表；未选过则 None，表示用 DSH 默认模型）。"""
    raw = kb_store.get_setting("ai_selection", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    return {"provider": provider, "model": model} if provider and model else None


def save_selection(provider: str, model: str) -> dict[str, str]:
    global _status_cache
    sel = {"provider": str(provider or "").strip(), "model": str(model or "").strip()}
    kb_store.set_setting("ai_selection", json.dumps(sel, ensure_ascii=False))
    _status_cache = None
    return sel


def catalog(force: bool = False) -> dict:
    """DSH 侧可用模型目录（60s 缓存）；拿不到就返回空列表，不抛异常。"""
    global _catalog_cache
    now = time.monotonic()
    if not force and _catalog_cache and now - _catalog_cache["at"] < _CATALOG_TTL:
        return dict(_catalog_cache["value"])
    value = {"providers": [], "default": None}
    if is_configured():
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(_models_url(), headers=_headers())
            data = response.json() if response.status_code == 200 else {}
            if isinstance(data, dict) and data.get("ok"):
                value = {
                    "providers": data.get("providers") or [],
                    "default": data.get("default"),
                }
        except (httpx.HTTPError, ValueError):
            pass
    _catalog_cache = {"at": now, "value": value}
    return dict(value)


def _status_url() -> str:
    base = config.AI_URL
    if base.endswith("/ai"):
        return base + "/status"
    return base.rstrip("/") + "/status"


def status(force: bool = False) -> dict:
    """模型状态：是否配置、是否可用、实际使用的 provider/model（60s 缓存）。

    可用性以 DSH 侧为准：用户选定的模型若已从 DSH 模型列表里消失，返回
    available=False + reason，后台任务据此安静跳过，不产生错误日志。
    """
    global _status_cache
    now = time.monotonic()
    if not force and _status_cache and now - _status_cache["at"] < _STATUS_TTL:
        return dict(_status_cache["value"])

    sel = selected()
    value: dict = {
        "configured": is_configured(),
        "available": False,
        "provider": "",
        "model": "",
        "mode": "dsh-agent",
        "selected": sel,
        "reason": "",
    }
    if not value["configured"]:
        value["reason"] = "未配置 AI 桥密钥（server/.ai-secret）"
    else:
        params = {"provider": sel["provider"], "model": sel["model"]} if sel else None
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(_status_url(), params=params, headers=_headers())
            data = response.json() if response.status_code == 200 else {}
            if isinstance(data, dict) and data.get("ok"):
                value.update(
                    {
                        "provider": str(data.get("provider", "")),
                        "model": str(data.get("model", "")),
                        "mode": str(data.get("mode", "dsh-agent")),
                        "reason": str(data.get("reason") or ""),
                    }
                )
                # 旧版桥没有 available 字段：有 provider/model 就视为可用。
                value["available"] = bool(data.get("available", bool(value["provider"] and value["model"])))
                if not value["provider"] or not value["model"]:
                    value["available"] = False
                    value["reason"] = value["reason"] or "DSH 没有可用的模型"
            else:
                value["reason"] = "AI 桥不可用"
        except (httpx.HTTPError, ValueError):
            value["reason"] = "AI 桥不可达"
    _status_cache = {"at": now, "value": value}
    return dict(value)


def available() -> bool:
    return bool(status().get("available"))


_last_avail_state: bool | None = None


def guard() -> bool:
    """后台任务入口：模型不可用返回 False。

    只在「可用 ↔ 不可用」切换时记一条 info 日志，避免每轮刷屏。
    """
    global _last_avail_state
    ok = available()
    if _last_avail_state is not None and _last_avail_state != ok:
        info = status()
        logger.info(
            "模型可用性变化：%s（%s/%s%s）",
            "可用" if ok else "不可用",
            info.get("provider", ""),
            info.get("model", ""),
            f" · {info.get('reason')}" if info.get("reason") else "",
        )
    _last_avail_state = ok
    return ok


def model_label() -> str:
    """给接口/界面展示的模型标签。"""
    if not is_configured():
        return ""
    sel = selected()
    if sel:
        return f"{sel['provider']}/{sel['model']} · dsh-agent"
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


def _stream_url() -> str:
    base = config.AI_URL.rstrip("/")
    if base.endswith("/ai"):
        return base + "/stream"
    return base + "/ai/stream"


def chat_stream(system: str, user: str, *, max_tokens: int | None = None, temperature: float = 0.1):
    """流式调用模型：逐段 yield (kind, payload)。

    kind 取值：thinking（已思考字数）/ delta（text）/ done（{provider, model}）/ error（error）。
    生成器内部吞掉 HTTP 异常并转成 KBAIError，由调用方决定如何提示。
    """
    if not is_configured():
        raise KBAIError("AI 未配置：缺少 DSH AI 桥密钥（server/.ai-secret）")
    sel = selected()
    if sel:
        info = status()
        if not info.get("available"):
            raise KBAIUnavailable(info.get("reason") or f"模型不可用：{sel['provider']}/{sel['model']}")
    payload = {
        "system": system,
        "user": user,
        "max_tokens": max_tokens or config.AI_MAX_TOKENS,
        # 问答只需要摘要级推理：低推理强度能显著降低首 token 时间。
        "reasoning_effort": "low",
    }
    if sel:
        payload["provider"] = sel["provider"]
        payload["model"] = sel["model"]
    if temperature is not None and temperature != 0.1:
        payload["temperature"] = temperature
    _pace()
    try:
        with httpx.stream(
            "POST", _stream_url(), json=payload, headers=_headers(), timeout=config.AI_TIMEOUT
        ) as response:
            if response.status_code == 404:
                raise KBAIError("AI 桥未就绪：DSH 插件未加载或密钥不匹配")
            if response.status_code != 200:
                response.read()
                raise KBAIError(f"AI 桥返回 HTTP {response.status_code}：{response.text[:200]}")
            event = ""
            for raw_line in response.iter_lines():
                line = raw_line.rstrip("\r")
                if line.startswith("event: "):
                    event = line[7:].strip()
                    continue
                if not line.startswith("data: "):
                    continue
                try:
                    data = json.loads(line[6:])
                except ValueError:
                    continue
                if event == "delta":
                    text = str(data.get("text") or "")
                    if text:
                        yield ("delta", text)
                elif event == "thinking":
                    try:
                        chars = int(data.get("chars") or 0)
                    except (TypeError, ValueError):
                        chars = 0
                    yield ("thinking", chars)
                elif event == "done":
                    yield ("done", data)
                elif event == "error":
                    raise KBAIError(str(data.get("error") or "流式调用失败")[:200])
    except httpx.HTTPError as exc:
        raise KBAIError(f"AI 桥请求失败：{exc}") from exc


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
    sel = selected()
    if sel:
        payload["provider"] = sel["provider"]
        payload["model"] = sel["model"]

    last_error = ""
    for attempt in range(_MAX_ATTEMPTS):
        _pace()
        try:
            with _CALL_LOCK, httpx.Client(timeout=config.AI_TIMEOUT) as client:
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
                error_text = str(data.get("error") or "AI 桥调用失败")[:200]
                if data.get("code") == "MODEL_UNAVAILABLE":
                    # 用户选的模型已不在 DSH 列表里：不重试、不刷日志，直接交给调用方跳过。
                    raise KBAIUnavailable(error_text)
                # 模型侧失败（超时/空输出/未配置模型）：重试一次仍失败就如实上报。
                last_error = error_text
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
