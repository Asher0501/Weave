"""C1: async 计时器。"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class Timer:
    """一次计时记录。"""
    __slots__ = ("_start", "_end")

    def __init__(self):
        self._start: float = 0.0
        self._end: float | None = None

    @property
    def elapsed(self) -> float:
        """已耗时（秒）。"""
        end = self._end if self._end is not None else time.perf_counter()
        return end - self._start

    @property
    def elapsed_ms(self) -> int:
        """已耗时（毫秒）。"""
        return int(self.elapsed * 1000)


@asynccontextmanager
async def timed() -> AsyncIterator[Timer]:
    """记录 async 操作耗时的 context manager。

    用法:
        async with timed() as timer:
            await llm.chat(prompt)
        print(f"LLM call took {timer.elapsed:.2f}s")
    """
    t = Timer()
    t._start = time.perf_counter()
    try:
        yield t
    finally:
        t._end = time.perf_counter()
