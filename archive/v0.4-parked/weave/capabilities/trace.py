"""TraceSink — 可观测性参考实现（注入 EventSink 钩子收集执行过程，T4）。

业务无关：仅按事件名/时间收集；事件集随 04-capability 定稿。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from weave.core.interfaces import EventSink


@dataclass(slots=True)
class TraceEvent:
    event_type: str
    data: dict[str, Any]
    timestamp: float


class TraceSink(EventSink):
    """内存版事件收集器。注入方式：``await agent.call(q, sink=trace_sink)``。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        self.events.append(TraceEvent(event_type, data or {}, time.time()))

    def by_type(self, event_type: str) -> list[TraceEvent]:
        return [e for e in self.events if e.event_type == event_type]

    def last(self, event_type: str) -> TraceEvent | None:
        found = self.by_type(event_type)
        return found[-1] if found else None

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e.event_type] = out.get(e.event_type, 0) + 1
        return out


__all__ = ["TraceSink", "TraceEvent"]
