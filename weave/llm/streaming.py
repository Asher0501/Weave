"""流式聚合（清单 F3/F4/F5/F8）：把增量 chunk 拼成一次完整响应。

为什么要聚合而不是让应用自己拼：`tool_calls` 的增量是**按 index 归并的片段流**
（id 只在首块、name 可能被切开、arguments 是逐字符 JSON 片段），
这是厂商协议的产物，属于 LLM 交互维度，不应让每个应用重踩一遍。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from weave.core.types import LLMResponse, StreamChunk, ToolCall

__all__ = ["StreamAccumulator"]


@dataclass(slots=True)
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class StreamAccumulator:
    """增量 → LLMResponse。

    用法：
        acc = StreamAccumulator()
        async for chunk in provider.stream(request):
            acc.feed(chunk)
        response = acc.result()

    `snapshot()` 用于流中途失败时保留已产出的部分（F6）。
    """

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, _PartialCall] = {}
        self._usage: dict[str, int] = {}
        self._finish: str = ""
        self._model: str = ""

    # ── 输入 ─────────────────────────────────────────

    def feed(self, chunk: StreamChunk) -> None:
        kind = chunk.kind or "token"
        if chunk.model:
            self._model = chunk.model
        if kind == "token":
            self._content.append(chunk.text or "")
        elif kind == "reasoning":
            self._reasoning.append(chunk.reasoning or chunk.text or "")
        elif kind == "tool_call_delta":
            self._feed_tool_call(chunk)
        elif kind == "usage":
            if chunk.usage:
                self._usage.update(chunk.usage)
        elif kind == "finish":
            self._finish = chunk.finish_reason or self._finish
            if chunk.usage:
                self._usage.update(chunk.usage)

    def _feed_tool_call(self, chunk: StreamChunk) -> None:
        partial = self._calls.setdefault(chunk.index, _PartialCall())
        if chunk.tool_call_id:
            partial.id = chunk.tool_call_id
        if chunk.tool_call_name:
            partial.name += chunk.tool_call_name
        if chunk.arguments_delta:
            partial.arguments.append(chunk.arguments_delta)

    # ── 输出 ─────────────────────────────────────────

    @property
    def content(self) -> str:
        return "".join(self._content)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self._calls)

    def snapshot(self) -> LLMResponse:
        """当前已产出内容的快照（失败时保留部分成果）。"""
        calls = self._tool_calls(raw_arguments=True)
        return LLMResponse(
            content=self.content,
            reasoning="".join(self._reasoning) or None,
            tool_calls=calls or None,
            model=self._model,
            usage=dict(self._usage),
            finish_reason=self._finish or ("tool_calls" if calls else "stop"),
        )

    def result(self) -> LLMResponse:
        calls = self._tool_calls(raw_arguments=False)
        return LLMResponse(
            content=self.content,
            reasoning="".join(self._reasoning) or None,
            tool_calls=calls or None,
            model=self._model,
            usage=dict(self._usage),
            finish_reason=self._finish or ("tool_calls" if calls else "stop"),
        )

    def _tool_calls(self, *, raw_arguments: bool) -> list[ToolCall] | None:
        if not self._calls:
            return None
        out: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            text = "".join(partial.arguments)
            if raw_arguments:
                arguments: object = text
            else:
                arguments = _arguments_or_raw(text)
            out.append(ToolCall(
                id=partial.id or f"call_{index}",
                name=partial.name,
                arguments=arguments,      # type: ignore[arg-type]
            ))
        return out


def _arguments_or_raw(text: str) -> object:
    """流式拼出来的 arguments 可能是半截 JSON：不在这里报错（G5 由解码器判定），
    解不开就把原始字符串原样带上，绝不丢内容。"""
    import json

    stripped = (text or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    return parsed
