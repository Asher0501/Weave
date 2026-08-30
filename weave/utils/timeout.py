"""A3: async 超时包装。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class WeaveTimeoutError(asyncio.TimeoutError):
    """Weave 超时异常。"""
    pass


@asynccontextmanager
async def timeout(seconds: float) -> AsyncIterator[None]:
    """为 async 调用设置截止时间。

    用法:
        async with timeout(30):
            result = await llm.chat(prompt)

    Args:
        seconds: 超时秒数

    Raises:
        WeaveTimeoutError: 超时
    """
    try:
        async with asyncio.timeout(seconds):
            yield
    except asyncio.TimeoutError:
        raise WeaveTimeoutError(f"Operation timed out after {seconds}s")
