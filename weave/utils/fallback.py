"""C2: 错误兜底 — 失败时返回指定默认值。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def fallback(
    fn: Callable[..., Any],
    *args: Any,
    default: T,
    on_error: Callable[[Exception], None] | None = None,
    **kwargs: Any,
) -> T:
    """调用 async 函数，失败时返回默认值而非抛出异常。

    Args:
        fn: async 函数
        *args: fn 的位置参数
        default: 失败时返回的默认值
        on_error: 可选的错误回调（用于日志记录）
        **kwargs: fn 的关键字参数

    Returns:
        fn 的返回值，或 default（失败时）
    """
    try:
        return await fn(*args, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if on_error:
            on_error(e)
        return default
