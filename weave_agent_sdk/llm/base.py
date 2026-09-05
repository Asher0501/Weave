"""LLM 适配器抽象接口。

定义 BaseLLM 抽象类和 LLMResponse 类型。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from weave_agent_sdk.types import Message, ToolCall


# ── LLMResponse ──────────────────────────────────────────


from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LLMResponse:
    """LLM 调用结果。"""
    content: str                        # LLM 文本输出
    tool_calls: list[ToolCall] | None = None  # tool 调用请求
    model: str = ""                     # 实际使用的模型名
    usage: dict[str, int] = field(default_factory=dict)  # {"input": N, "output": M}
    finish_reason: str = "stop"         # "stop" | "tool_calls" | "length"


# ── BaseLLM ──────────────────────────────────────────────


class BaseLLM(ABC):
    """LLM 适配器抽象接口。

    所有 LLM 后端（Anthropic、OpenAI 等）的实现基类。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """非流式 LLM 调用。

        Args:
            messages: 对话消息列表
            tools: Tool JSON Schema 列表（可选）
            max_tokens: 最大输出 token 数
            temperature: 采样温度

        Returns:
            LLMResponse
        """
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """流式 LLM 调用，逐 token yield。

        Args:
            messages: 对话消息列表
            tools: Tool JSON Schema 列表（可选）
            max_tokens: 最大输出 token 数
            temperature: 采样温度

        Yields:
            每个 token 的文本片段
        """
        ...
