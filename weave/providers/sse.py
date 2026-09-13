"""SSE 解析（清单 F1/F2）：把任意分片的文本流切成完整的事件。

两个层次：
- `SSEBuffer`：**跨分片半行缓冲**——网络给你的不是"一行一行"，而是任意切开的字节，
  一行可能被切成两半（甚至断在 UTF-8 字符中间，所以解码要按字节累积）。
- `iter_sse_events(fragments)`：按 SSE 规范组装事件（`data:` 多行拼接、忽略注释/空行、`[DONE]` 结束）。

F2 是这里最容易被忽略、也最容易出线上事故的一条：如果不缓冲，
"arguments" 片段被切开时会静默丢字段。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

__all__ = ["SSEBuffer", "SSEEvent", "iter_sse_events", "DONE"]

DONE = "[DONE]"


class SSEBuffer:
    """把文本分片切成完整行（去掉行尾换行；兼容 \\n 与 \\r\\n）。"""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, fragment: str) -> list[str]:
        """喂一个分片，返回其中已经完整的行。"""
        if not fragment:
            return []
        self._buffer += fragment
        *lines, self._buffer = self._split(self._buffer)
        return lines

    def flush(self) -> list[str]:
        """流结束时把残留的半行也交出来（否则最后一行会被丢掉）。"""
        if not self._buffer:
            return []
        remainder, self._buffer = self._buffer, ""
        return [remainder.rstrip("\r")]

    @staticmethod
    def _split(text: str) -> list[str]:
        if "\n" not in text:
            return [text]
        parts = text.split("\n")
        parts[-1] = parts[-1]              # 末段是不完整部分，保留
        complete = [p.rstrip("\r") for p in parts[:-1]]
        return [*complete, parts[-1]]


@dataclass(slots=True)
class SSEEvent:
    """一个 SSE 事件（`event:` 默认 message；`data` 是多行拼接后的内容）。"""

    data: str
    event: str = "message"


async def iter_sse_events(fragments: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """文本分片 → SSE 事件（忽略注释行与心跳，遇到 `[DONE]` 结束）。"""
    buffer = SSEBuffer()
    data_lines: list[str] = []
    event_name = "message"

    def take() -> SSEEvent | None:
        nonlocal data_lines, event_name
        if not data_lines:
            event_name = "message"
            return None
        event = SSEEvent(data="\n".join(data_lines), event=event_name)
        data_lines, event_name = [], "message"
        return event

    async for fragment in fragments:
        lines = buffer.feed(fragment)
        for line in lines:
            if line == "":
                event = take()
                if event is not None:
                    yield event
                continue
            if line.startswith(":"):
                continue                    # 心跳/注释（keep-alive）
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                if value == DONE:
                    return
                data_lines.append(value)
            elif field == "event":
                event_name = value or "message"

    for line in buffer.flush():
        if line and not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data" and value != DONE:
                data_lines.append(value)
    event = take()
    if event is not None:
        yield event
