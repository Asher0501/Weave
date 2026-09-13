"""Provider 可靠性内部工具（T2）：重试策略 + 超时语义。

仅 providers 子包使用；不进入公共契约。
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, TypeVar, Callable

from weave.core.errors import (
    NetworkError,
    ProviderTimeoutError,
    RateLimitError,
    RetryExhaustedError,
    ServerError,
)

T = TypeVar("T")

# 可重试集合（T2.1）
RETRYABLE: tuple[type[BaseException], ...] = (
    RateLimitError,
    ServerError,
    NetworkError,
)


@dataclass(slots=True)
class RetryPolicy:
    """可靠性策略。

    Attributes:
        max_attempts: 最大尝试次数（含首次）。
        backoff: 指数退避基数（延迟 = backoff ** (attempt-1) 秒）。
        jitter: 随机抖动比例。
        single_timeout: 单次调用超时（秒）；None=不限。触发即 ProviderTimeoutError（不重试）。
        overall_timeout: 整重试序列硬上限（秒）；None=不限。超时抛 ProviderTimeoutError。
    """

    max_attempts: int = 3
    backoff: float = 2.0
    jitter: float = 0.1
    single_timeout: float | None = None
    overall_timeout: float | None = 120.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff <= 0:
            raise ValueError("backoff must be > 0")


def _delay_for(attempt: int, policy: RetryPolicy) -> float:
    delay = policy.backoff ** (attempt - 1)
    delay += random.uniform(-policy.jitter, policy.jitter) * delay
    return max(0.05, delay)


async def call_with_reliability(
    policy: RetryPolicy,
    fn: Callable[..., Any],
    *args: Any,
    retryable: tuple[type[BaseException], ...] = RETRYABLE,
    **kwargs: Any,
) -> T:
    """带可靠性地执行一次 Provider 调用。

    - overall_timeout 用 asyncio.timeout 作整段硬上限（含在途调用），触发抛 ProviderTimeoutError；
    - 单次调用超时（single_timeout，经 wait_for）→ ProviderTimeoutError，不重试；
    - 可重试错误指数退避重试；业务/不可重试错误立即抛出；耗尽抛 RetryExhaustedError。
    """

    async def _one() -> T:
        if policy.single_timeout is None:
            return await fn(*args, **kwargs)
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), policy.single_timeout)
        except asyncio.TimeoutError:
            raise ProviderTimeoutError(
                f"provider call timed out after {policy.single_timeout}s"
            ) from None

    async def _loop() -> T:
        attempt = 0
        last: BaseException | None = None
        while True:
            attempt += 1
            try:
                return await _one()
            except retryable as e:  # type: ignore[assignment]
                last = e
                if attempt >= policy.max_attempts:
                    break
                await asyncio.sleep(_delay_for(attempt, policy))
            except BaseException:
                raise  # 不可重试/业务错误直接抛
        raise RetryExhaustedError(
            f"all {policy.max_attempts} attempts failed", last_error=last
        )

    if policy.overall_timeout is None:
        return await _loop()
    try:
        async with asyncio.timeout(policy.overall_timeout):
            return await _loop()
    except asyncio.TimeoutError:
        raise ProviderTimeoutError(
            f"provider retry sequence exceeded overall timeout "
            f"{policy.overall_timeout}s"
        ) from None


async def stream_with_reliability(
    policy: RetryPolicy,
    fn: Callable[..., Any],
    *args: Any,
    retryable: tuple[type[BaseException], ...] = RETRYABLE,
    **kwargs: Any,
) -> Any:
    """流式可靠性（T2.3）：仅"首个 chunk 之前"的失败可重试；开始产出后不再重试。

    返回可 async for 的迭代器。连接/首 chunk 前失败按策略重试（受 overall_timeout
    约束）；首 chunk 之后的错误原样上抛（已吐出的内容不可撤回）。
    """

    async def _connect() -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await fn(*args, **kwargs)
            except retryable as e:  # type: ignore[assignment]
                if attempt >= policy.max_attempts:
                    raise RetryExhaustedError(
                        f"all {policy.max_attempts} attempts failed", last_error=e
                    ) from None
                await asyncio.sleep(_delay_for(attempt, policy))
            except BaseException:
                raise

    if policy.overall_timeout is None:
        return await _connect()
    try:
        async with asyncio.timeout(policy.overall_timeout):
            return await _connect()
    except asyncio.TimeoutError:
        raise ProviderTimeoutError(
            f"stream retry sequence exceeded overall timeout "
            f"{policy.overall_timeout}s"
        ) from None
