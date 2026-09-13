"""OpenAICompatibleProvider — OpenAI 兼容 API（含 DeepSeek）参考实现。

- 内嵌可靠性（T2）：重试 / 单次与整序列超时 / 类型化错误，独立使用亦可靠；
- 推理内容（reasoning_content）解析进 LLMResponse.reasoning，**绝不回传**；
- deepseek-reasoner 自动过滤不支持的参数（temperature/top_p）；
- 工具 schema 厂商中立 → 在此转换为 OpenAI 格式（T1.1）。

依赖：openai（可选 extra）。
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from weave.core.errors import (
    AuthError,
    classify_http_error,
)
from weave.core.interfaces import Provider
from weave.core.types import (
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    ToolSchema,
)
from weave.providers._reliability import (
    RETRYABLE,
    RetryPolicy,
    call_with_reliability,
    stream_with_reliability,
)

# deepseek-reasoner 不支持采样参数
_REASONER_MODELS = ("deepseek-reasoner",)


def _is_reasoner(model: str) -> bool:
    return any(model.startswith(m) for m in _REASONER_MODELS)


class OpenAICompatibleProvider(Provider):
    """经 OpenAI SDK 调用任何 OpenAI 兼容端点（openai / deepseek / 各类网关）。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "",
        base_url: str | None = None,
        retry: RetryPolicy | None = None,
        default_max_tokens: int = 4096,
        default_temperature: float = 0.7,
    ) -> None:
        if not model:
            model = os.environ.get("WEAVE_MODEL") or os.environ.get("OPENAI_MODEL") or os.environ.get("DEEPSEEK_MODEL") or ""
        if not model:
            raise ValueError(
                "model must be specified (arg or WEAVE_MODEL/OPENAI_MODEL/DEEPSEEK_MODEL env)"
            )
        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise AuthError(
                "no api_key: pass api_key or set OPENAI_API_KEY / DEEPSEEK_API_KEY"
            )
        if base_url is None and model.startswith("deepseek"):
            base_url = "https://api.deepseek.com/v1"

        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._retry = retry or RetryPolicy()
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature

    # ── Provider ─────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **extra: Any,
    ) -> LLMResponse:
        client = self._client()
        kwargs = _build_request_kwargs(
            model=self._model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            temperature=temperature if temperature is not None else self._default_temperature,
            extra=extra,
        )

        async def _once() -> LLMResponse:
            resp = await client.chat.completions.create(**kwargs)
            return _parse_openai_response(resp)

        return await call_with_reliability(self._retry, _once, retryable=RETRYABLE)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **extra: Any,
    ) -> AsyncIterator[StreamChunk]:
        client = self._client()
        kwargs = _build_request_kwargs(
            model=self._model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            temperature=temperature if temperature is not None else self._default_temperature,
            extra=extra,
        )

        def _open_stream() -> Any:
            return client.chat.completions.create(**kwargs, stream=True)

        raw = await stream_with_reliability(self._retry, _open_stream, retryable=RETRYABLE)
        async for chunk in raw:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                yield StreamChunk(kind="token", text=delta.content)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield StreamChunk(kind="reasoning", reasoning=reasoning)
            finish = getattr(chunk.choices[0], "finish_reason", None)
            if finish:
                yield StreamChunk(kind="finish", finish_reason=finish)

    # ── 内部 ─────────────────────────────────────────

    def _client(self) -> Any:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package required for OpenAICompatibleProvider. "
                "Install with: pip install 'weave[openai]'"
            ) from None
        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        return openai.AsyncOpenAI(**client_kwargs)


# ── 纯函数（可单测） ────────────────────────────────────


def _build_request_kwargs(
    *,
    model: str,
    messages: list[Message],
    tools: list[ToolSchema] | None,
    max_tokens: int,
    temperature: float,
    extra: dict[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages_to_openai(messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
        **extra,
    }
    if _is_reasoner(model):
        # reasoner 不支持采样参数：移除（T2/DeepSeek 特性）
        kwargs.pop("temperature", None)
        kwargs.pop("top_p", None)
    if tools:
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
    return kwargs


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Message → OpenAI messages 格式。

    注意：Message 模型不含 reasoning 字段 → 推理内容天然不回传（T2）。
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.role == "assistant" and msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in msg.tool_calls
            ]
        elif msg.role == "tool":
            entry["tool_call_id"] = msg.tool_call_id or ""
        if msg.name:
            entry["name"] = msg.name
        out.append(entry)
    return out


def _parse_openai_response(resp: Any) -> LLMResponse:
    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    message = choice.message if choice else None

    content = (getattr(message, "content", None) or "") if message else ""
    reasoning = (getattr(message, "reasoning_content", None) or "") if message else None

    tool_calls: list[ToolCall] = []
    if message and getattr(message, "tool_calls", None):
        for tc in message.tool_calls:
            arguments = {}
            if getattr(tc.function, "arguments", None):
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": tc.function.arguments}
            tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
            )

    usage: dict[str, int] = {}
    if getattr(resp, "usage", None):
        usage = {
            "input": getattr(resp.usage, "prompt_tokens", 0) or 0,
            "output": getattr(resp.usage, "completion_tokens", 0) or 0,
        }

    return LLMResponse(
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls or None,
        model=getattr(resp, "model", "") or "",
        usage=usage,
        finish_reason=(getattr(choice, "finish_reason", None) or "stop") if choice else "stop",
    )


__all__ = ["OpenAICompatibleProvider", "_build_request_kwargs", "messages_to_openai", "_is_reasoner"]
