"""FakeProvider — 离线确定性 Provider（测试用）。

哑原子：按脚本顺序逐次返回预设结果，**不重试、不计时、不解码**。
用法：
    FakeProvider(turns=[
        FakeTurn(tool_calls=[ToolCall(id="1", name="add", arguments='{"a":1,"b":2}')]),
        FakeTurn(content="结果是 3"),
    ])
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from weave.core.envelopes import CallRequest
from weave.core.interfaces import LLMProvider
from weave.core.types import LLMResponse, Message, StreamChunk, ToolCall


@dataclass(slots=True)
class FakeTurn:
    """一次 complete() 的预设结果。"""

    content: str = ""
    tool_calls: list[ToolCall] | None = None
    reasoning: str | None = None
    finish_reason: str = "stop"
    usage: dict[str, int] | None = None


class FakeProvider(LLMProvider):
    """按脚本逐次返回预设结果；脚本耗尽后重复最后一条。"""

    def __init__(self, *, model: str = "fake-model", turns: list[FakeTurn] | None = None) -> None:
        self._model = model
        self._turns: list[FakeTurn] = turns or [FakeTurn(content="ok")]
        if not self._turns:
            raise ValueError("turns must not be empty")
        self._index = 0
        #: 记录每次调用收到的请求（测试断言用）
        self.calls: list[CallRequest] = []

    @property
    def model(self) -> str:
        return self._model

    def _next(self) -> FakeTurn:
        turn = self._turns[min(self._index, len(self._turns) - 1)]
        self._index += 1
        return turn

    async def complete(self, request: CallRequest) -> LLMResponse:
        self.calls.append(request)
        turn = self._next()
        return LLMResponse(
            content=turn.content,
            reasoning=turn.reasoning,
            tool_calls=turn.tool_calls,
            model=self._model,
            usage=turn.usage or {"input": 0, "output": len(turn.content)},
            finish_reason=turn.finish_reason,
        )

    def stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        self.calls.append(request)
        turn = self._next()

        async def gen() -> AsyncIterator[StreamChunk]:
            for token in turn.content:
                yield StreamChunk(kind="token", text=token)
            if turn.reasoning:
                yield StreamChunk(kind="reasoning", reasoning=turn.reasoning)
            yield StreamChunk(kind="finish", finish_reason=turn.finish_reason)

        return gen()


__all__ = ["FakeProvider", "FakeTurn"]
