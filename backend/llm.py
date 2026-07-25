"""LLM 统一适配层:所有 LLM 调用的唯一入口。

按 settings.llm_binding_type 分发:
  openai     OpenAI 兼容 /chat/completions(阿里云百炼等)
  anthropic  Anthropic messages 协议(opencode zen 等中转站)

三个接口:
  complete(system, user, ...)       -> str          非流式补全
  complete_json(system, user, ...)  -> dict         补全并解析 JSON
  stream(system, user, ...)         -> AsyncIterator[str]  流式增量

LightRAG 内部的 llm_model_func 也走这里(见 rag_client.py)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator

from backend.config import settings

_MAX_TOKENS = 8192  # anthropic 协议必填
logger = logging.getLogger(__name__)


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: Exception) -> bool:
    """识别限流、网络故障和模型服务端的短暂失败。"""
    status = _status_code(exc)
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True
    text = str(exc).lower()
    # 部分中转站把内部 500 包在 InvalidParameter/400 外层。
    transient_markers = (
        "batching backend",
        "internalerror.algo",
        "model serving",
        "timed out",
        "timeout",
        "connection error",
        "connection reset",
        "temporarily unavailable",
        "rate limit",
    )
    return any(marker in text for marker in transient_markers)


async def _retry_delay(attempt: int, exc: Exception) -> None:
    delay = settings.llm_retry_base_seconds * (2 ** attempt)
    delay += random.uniform(0, max(0.05, delay * 0.2))
    logger.warning(
        "LLM 临时失败，将在 %.2fs 后进行第 %d/%d 次重试: %s",
        delay,
        attempt + 1,
        settings.llm_max_retries,
        exc,
    )
    await asyncio.sleep(delay)


def _messages(user: str, history: list[dict] | None = None) -> list[dict]:
    return [*(history or []), {"role": "user", "content": user}]


# ---------------- OpenAI 协议 ----------------

def _openai_client():
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.llm_binding_api_key,
        base_url=settings.llm_binding_host,
    )


async def _openai_complete(
    system: str | None, user: str, *, model: str,
    history: list[dict] | None = None,
) -> str:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs += _messages(user, history)
    for attempt in range(settings.llm_max_retries + 1):
        try:
            resp = await _openai_client().chat.completions.create(
                model=model, messages=msgs,
                extra_body={"enable_thinking": False},
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            if attempt >= settings.llm_max_retries or not _is_retryable(exc):
                raise
            await _retry_delay(attempt, exc)
    raise RuntimeError("unreachable")


async def _openai_stream(
    system: str | None, user: str, *, model: str,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs += _messages(user, history)
    for attempt in range(settings.llm_max_retries + 1):
        yielded = False
        try:
            stream = await _openai_client().chat.completions.create(
                model=model, messages=msgs, stream=True,
                extra_body={"enable_thinking": False},
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yielded = True
                    yield delta
            return
        except Exception as exc:  # noqa: BLE001
            # 已输出的流不能自动重跑，否则调用方会收到重复正文。
            if yielded or attempt >= settings.llm_max_retries or not _is_retryable(exc):
                raise
            await _retry_delay(attempt, exc)


# ---------------- Anthropic 协议 ----------------

def _anthropic_client():
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(
        api_key=settings.llm_binding_api_key,
        base_url=settings.llm_binding_host,
    )


async def _anthropic_complete(
    system: str | None, user: str, *, model: str,
    history: list[dict] | None = None,
) -> str:
    for attempt in range(settings.llm_max_retries + 1):
        try:
            resp = await _anthropic_client().messages.create(
                model=model,
                max_tokens=_MAX_TOKENS,
                system=system or "",
                messages=_messages(user, history),
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        except Exception as exc:  # noqa: BLE001
            if attempt >= settings.llm_max_retries or not _is_retryable(exc):
                raise
            await _retry_delay(attempt, exc)
    raise RuntimeError("unreachable")


async def _anthropic_stream(
    system: str | None, user: str, *, model: str,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    for attempt in range(settings.llm_max_retries + 1):
        yielded = False
        try:
            client = _anthropic_client()
            async with client.messages.stream(
                model=model,
                max_tokens=_MAX_TOKENS,
                system=system or "",
                messages=_messages(user, history),
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        yielded = True
                        yield text
            return
        except Exception as exc:  # noqa: BLE001
            if yielded or attempt >= settings.llm_max_retries or not _is_retryable(exc):
                raise
            await _retry_delay(attempt, exc)


# ---------------- 统一入口 ----------------

def _is_anthropic() -> bool:
    return settings.llm_binding_type.lower() == "anthropic"


async def complete(
    system: str | None, user: str, *,
    model: str | None = None, history: list[dict] | None = None,
) -> str:
    m = model or settings.llm_model
    if _is_anthropic():
        return await _anthropic_complete(system, user, model=m, history=history)
    return await _openai_complete(system, user, model=m, history=history)


async def complete_json(
    system: str | None, user: str, *, model: str | None = None,
) -> dict[str, Any]:
    """补全并解析 JSON(容忍 markdown 代码块包裹)。"""
    text = (await complete(system, user, model=model)).strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    return json.loads(text)


def stream(
    system: str | None, user: str, *, model: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    m = model or settings.llm_model
    if _is_anthropic():
        return _anthropic_stream(system, user, model=m, history=history)
    return _openai_stream(system, user, model=m, history=history)


async def preflight(timeout_seconds: float | None = None) -> dict[str, Any]:
    """Perform a minimal, explicitly requested provider/model permission check."""
    timeout = timeout_seconds or settings.model_preflight_timeout_seconds

    async def probe() -> str:
        if _is_anthropic():
            response = await _anthropic_client().messages.create(
                model=settings.llm_model,
                max_tokens=4,
                system="",
                messages=[{"role": "user", "content": "Reply OK"}],
            )
            return "".join(block.text for block in response.content if block.type == "text")
        response = await _openai_client().chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "Reply OK"}],
            max_tokens=4,
            extra_body={"enable_thinking": False},
        )
        return response.choices[0].message.content or ""

    started = asyncio.get_running_loop().time()
    text = await asyncio.wait_for(probe(), timeout=timeout)
    elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
    return {
        "ok": True,
        "binding": settings.llm_binding_type,
        "model": settings.llm_model,
        "latency_ms": elapsed,
        "response_received": bool(text.strip()),
    }
