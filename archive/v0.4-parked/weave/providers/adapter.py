"""把**旧形态**的 Provider 适配成哑原子 `LLMProvider`。

为什么需要：v0.4 的 `OpenAICompatibleProvider` 是「messages/tools 入参 + 内嵌重试」的旧形态，
而 LLM 交互维度要求原子是哑的（一次调用、失败即抛）。适配器让旧实现能立刻被新对象使用。

⚠️ 注意：旧实现自己会重试。使用本适配器时建议把对象的 `retry` 设为 0，
否则会出现"两层重放"（虽然都是同一请求的重放，不会出错，但耗时和次数会翻倍）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from weave.core.envelopes import CallRequest
from weave.core.interfaces import LLMProvider, Provider
from weave.core.types import LLMResponse, StreamChunk

__all__ = ["LegacyProviderAdapter"]


class LegacyProviderAdapter(LLMProvider):
    """旧 Provider → 哑 LLMProvider（只做参数搬运，不改变语义）。"""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider
        self.model = getattr(provider, "model", "")

    async def complete(self, request: CallRequest) -> LLMResponse:
        opts = dict(request.opts or {})
        max_tokens = opts.pop("max_tokens", 4096)
        temperature = opts.pop("temperature", 0.7)
        return await self._provider.complete(
            request.messages,
            request.schemas or None,
            max_tokens=max_tokens,
            temperature=temperature,
            **opts,
        )

    def stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        opts = dict(request.opts or {})
        max_tokens = opts.pop("max_tokens", 4096)
        temperature = opts.pop("temperature", 0.7)
        return self._provider.stream(
            request.messages,
            request.schemas or None,
            max_tokens=max_tokens,
            temperature=temperature,
            **opts,
        )

    async def aclose(self) -> None:
        for name in ("aclose", "close"):
            closer = getattr(self._provider, name, None)
            if closer is None:
                continue
            result = closer()
            if hasattr(result, "__await__"):
                await result
            return

    def __getattr__(self, item: str) -> Any:      # 透传旧实现的属性（如 model）
        return getattr(self._provider, item)
