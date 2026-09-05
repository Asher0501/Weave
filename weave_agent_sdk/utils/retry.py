"""A2: async 重试 — 指数退避 + jitter。"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    backoff: float = 2.0,
    jitter: float = 0.1,
    retryable: tuple[type[BaseException], ...] | None = None,
    **kwargs: Any,
) -> T:
    """重试 async 调用（指数退避 + 随机 jitter）。

    Args:
        fn: 要重试的 async 函数
        *args: fn 的位置参数
        max_attempts: 最大尝试次数（含首次）
        backoff: 退避倍率（延迟 = backoff ** attempt 秒）
        jitter: 随机抖动比例（0.1 = ±10%）
        retryable: 可重试的异常类型元组，None 表示所有异常都重试
        **kwargs: fn 的关键字参数

    Returns:
        fn 的返回值

    Raises:
        RetryExhaustedError: 所有重试失败
    """
    if retryable is None:
        retryable = (Exception,)

    last_exception: BaseException | None = None

    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except retryable as e:
            last_exception = e
            if attempt == max_attempts - 1:
                break
            delay = backoff ** attempt
            delay += random.uniform(-jitter, jitter) * delay
            delay = max(0.1, delay)  # 最少 100ms
            await asyncio.sleep(delay)
        except BaseException:
            raise  # 不可重试的异常，直接抛出

    raise RetryExhaustedError(
        f"All {max_attempts} attempts failed. Last error: {last_exception}",
        last_exception=last_exception,
    )


class RetryExhaustedError(Exception):
    """所有重试均已耗尽。"""

    def __init__(self, message: str, last_exception: BaseException | None = None):
        super().__init__(message)
        self.last_exception = last_exception
