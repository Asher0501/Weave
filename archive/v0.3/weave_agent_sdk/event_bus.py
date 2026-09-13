"""A5: 进程内异步事件总线。

Subscribe via async generator; publish to all subscribers.
阻塞式 backpressure（await queue.put()），不丢消息。

事件类型与 schema 遵循 basic.md §5 / public-api.md §1.6 的不可变契约：
    token       — {"text": str, "index": int}
    tool_call   — {"name": str, "arguments": dict}
    tool_result — {"name": str, "result": Any, "error": bool}
    done        — {"output": str, "elapsed_ms": int, "iterations": int}
    error       — {"message": str, "exception": str}

用法:
    # 发布
    await bus.emit("tool_call", {"name": "search_kb"})

    # 订阅
    async for event in bus.subscribe("token", "tool_call", "tool_result", "done", "error"):
        print(event)
"""
from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import AsyncIterator
from typing import Any

from weave_agent_sdk.types import WeaveEvent

logger = logging.getLogger(__name__)


class EventBus:
    """In-process async pub/sub bus."""

    def __init__(self, max_queue_size: int = 0):
        """
        Args:
            max_queue_size: 每个订阅者队列最大容量。0 = 无限制（阻塞式背压）
        """
        self._subscribers: dict[str, list[asyncio.Queue[WeaveEvent]]] = {}
        self._max_queue_size = max_queue_size

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """向所有订阅者广播事件。阻塞直到所有队列写入完成。

        Args:
            event_type: 事件类型
            data: 事件数据
        """
        event = WeaveEvent(
            type=event_type,
            data=data or {},
            timestamp=time.time(),
        )

        queues = self._subscribers.get(event_type, [])
        if not queues:
            return

        # 并行写入所有订阅者队列，记录异常而非静默吞掉
        results = await asyncio.gather(
            *(q.put(event) for q in queues),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(
                    "EventBus.emit(%s): subscriber queue %d put failed: %s",
                    event_type, i, r,
                )

    def subscribe(self, *event_types: str) -> AsyncIterator[WeaveEvent]:
        """订阅一个或多个事件类型。

        用法:
            async for event in bus.subscribe("token", "tool_call", "tool_result", "done", "error"):
                print(event.type, event.data)

        急切注册：队列创建与订阅注册在调用时立即完成（而非延迟到首次
        anext()），保证订阅对 emit 立即可见——避免"后台任务先于订阅注册、
        同步失败的错误事件被丢弃"的注册竞态（review round-1 issue 1）。
        返回的异步生成器只负责取事件与退订（aclose() 时清理）。

        生命周期防护（review round-8 issue 5）：宿主持有返回的生成器而不
        消费 / 不 aclose（如 `gen = weave.on("done")` 后长期挂起）时，队列
        恒注册于 EventBus，每次 emit 都向该无消费者队列广播、无限累积——
        退订原本依赖 CPython async generator 终结（GC/loop 调度 aclose），
        时机不可控。此处通过 weakref.finalize 在生成器对象被垃圾回收时
        **确定性**同步退订（不依赖 async generator 的 aclose 调度），
        消除被遗弃订阅的广播目标累积与内存增长。
        """
        queue: asyncio.Queue[WeaveEvent] = asyncio.Queue(maxsize=self._max_queue_size)

        # 注册（急切）：调用时立即完成，emit 立即可见
        for et in event_types:
            if et not in self._subscribers:
                self._subscribers[et] = []
            self._subscribers[et].append(queue)

        def _unsubscribe() -> None:
            """将本订阅的队列从 EventBus 退订（幂等，可被 aclose finally 与
            weakref.finalize 重复调用）。"""
            for et in event_types:
                subs = self._subscribers.get(et, [])
                if queue in subs:
                    subs.remove(queue)
                if not subs:
                    self._subscribers.pop(et, None)

        async def _generator() -> AsyncIterator[WeaveEvent]:
            try:
                while True:
                    event = await queue.get()
                    yield event
            except asyncio.CancelledError:
                raise
            finally:
                # 显式退订（aclose() 触发）
                _unsubscribe()

        gen = _generator()
        # GC 退订兜底：宿主丢弃生成器（不迭代 / 不 aclose）时，对象被垃圾
        # 回收即确定性退订，而非依赖 CPython async generator 终结调度
        # （review round-8 issue 5）。finalize 持弱引用，不延长生成器生命周期。
        try:
            weakref.finalize(gen, _unsubscribe)
        except TypeError:
            # 极端环境（生成器对象不支持弱引用）下退化为依赖 aclose/GC 终结
            pass
        return gen

    @property
    def subscriber_count(self) -> dict[str, int]:
        """返回各事件类型的订阅者数量。"""
        return {k: len(v) for k, v in self._subscribers.items()}
