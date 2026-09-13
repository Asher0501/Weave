"""单动作可靠性（清单 C/D/E）：超时、重放、退避、限流、错误分类。

这是 weave 的核心增值之一，也是**判据 D15** 的落点：
> 同一请求的重放 = 本维度；变更了输入/上下文的再一次动作 = 应用。

本模块不知道"模型"是什么——它只负责把一个可能失败的异步动作跑成一次可靠的动作。
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from weave.core.envelopes import RETRYABLE_KINDS, TypedFailure, classify_exception

__all__ = ["ReliabilityPolicy", "Attempt", "run_with_reliability", "retry_after_of", "emit_event"]

Emitter = Callable[[str, dict[str, Any]], Any]


@dataclass(slots=True)
class ReliabilityPolicy:
    """单动作可靠性参数（构造器形态，无全局可变状态）。"""

    timeout: float | None = 30.0        # 单次尝试超时（秒）；None = 不限
    total_timeout: float | None = 120.0  # 整个动作（含所有重放与退避）的上限
    retries: int = 3                    # 首次之外的重放次数
    backoff: float = 0.5                # 退避基数
    max_backoff: float = 8.0
    jitter: float = 0.1                 # 抖动比例，避免同时重放
    respect_retry_after: bool = True    # 尊重厂商的 Retry-After
    max_retry_after: float = 60.0       # 厂商给的天文数字也封顶

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """第 attempt 次失败后等待多久（attempt 从 1 开始）。"""
        if retry_after is not None and self.respect_retry_after:
            return max(0.0, min(retry_after, self.max_retry_after))
        base = min(self.max_backoff, self.backoff * (2 ** (attempt - 1)))
        if self.jitter:
            base *= 1 + random.uniform(0.0, self.jitter)
        return base


@dataclass(slots=True)
class Attempt:
    """一次可靠动作的结果（成功或失败都返回它，由调用方决定怎么呈现）。"""

    value: Any = None
    failure: TypedFailure | None = None
    attempts: int = 0
    elapsed_ms: float = 0.0
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def retry_after_of(exc: BaseException) -> float | None:
    """从异常上取厂商给的 Retry-After（秒）。原子负责填，本模块负责读。"""
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


async def emit_event(on_event: Emitter | None, event: str, data: dict[str, Any]) -> None:
    """可观测性回调（I1–I4）：同步或异步都支持；回调异常绝不影响主流程。"""
    if on_event is None:
        return
    try:
        result = on_event(event, data)
        if asyncio.iscoroutine(result):
            await result
    except Exception:            # noqa: BLE001 - 观测不得干扰主流程
        pass


async def run_with_reliability(
    op: Callable[[], Awaitable[Any]],
    policy: ReliabilityPolicy,
    *,
    origin: str = "llm",
    on_event: Emitter | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> Attempt:
    """把 `op()` 跑成一次可靠动作。

    - 每次尝试超时 = `policy.timeout`
    - 只重放可重试类（`RETRYABLE_KINDS`）
    - 退避 = 指数 + 抖动；厂商给了 `Retry-After` 就听它的
    - 总时长受 `policy.total_timeout` 约束：退避后必然超预算时就不再重放
    - `CancelledError` 不是失败，原样上抛（F7）
    """
    started = clock()
    attempt = 0
    last_retry_after: float | None = None

    while True:
        attempt += 1
        try:
            if policy.timeout is None:
                value = await op()
            else:
                value = await asyncio.wait_for(op(), timeout=policy.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                    # noqa: BLE001 - 分类后统一处理
            failure = classify_exception(exc, origin=origin)
            elapsed = clock() - started
            retry_after = retry_after_of(exc)
            last_retry_after = retry_after if retry_after is not None else last_retry_after
            retryable = failure.kind in RETRYABLE_KINDS
            within_retries = attempt <= policy.retries
            delay = policy.delay_for(attempt, retry_after)
            within_budget = (
                policy.total_timeout is None
                or (elapsed + delay) <= policy.total_timeout
            )
            will_retry = retryable and within_retries and within_budget
            await emit_event(on_event, "llm.retry" if will_retry else "llm.failure", {
                "attempt": attempt,
                "kind": failure.kind,
                "message": failure.message,
                "will_retry": will_retry,
                "delay": delay if will_retry else 0.0,
                "retry_after": retry_after,
                "elapsed_ms": elapsed * 1000,
            })
            if not will_retry:
                return Attempt(failure=failure, attempts=attempt,
                               elapsed_ms=elapsed * 1000, retry_after=last_retry_after)
            await sleep(delay)
            continue

        elapsed = clock() - started
        return Attempt(value=value, attempts=attempt, elapsed_ms=elapsed * 1000,
                       retry_after=last_retry_after)
