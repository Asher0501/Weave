"""weave.core.envelopes — 冻结信封（接口之间传递的公共数据结构）。

weave 只有一个功能，所以信封也只有一套：**进去的是请求、出来的是响应**，
失败走统一信封。

| 信封 | 方向 | 说明 |
|---|---|---|
| `CallRequest` | 对象 → 哑原子 | 一次动作的输入 |
| `LLMResponse` | 哑原子 → 对象 → 应用 | 一次动作的输出（对象会补齐 usage/解码/attempts） |
| `StreamChunk` | 哑原子 → 对象 | 流式增量 |
| `Invocation` | 对象内部 | 解码后的工具调用结构（不对外暴露成接口） |
| `TypedFailure` | 任意 → 应用 | 跨层的唯一失败信封 |

基础词汇（`Message` / `ToolCall` / `ToolSchema` / `LLMResponse` / `StreamChunk`）
定义在 `weave.core.types`，这里统一再导出，避免调用方记两个模块。

本模块零第三方依赖，仅标准库。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from weave.core.types import (
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    ToolSchema,
)

__all__ = [
    # 信封
    "CallRequest",
    "Invocation",
    "TypedFailure",
    # 基础词汇（再导出）
    "Message",
    "ToolCall",
    "ToolSchema",
    "LLMResponse",
    "StreamChunk",
    # 常量与工具
    "RETRYABLE_KINDS",
    "NON_RETRYABLE_KINDS",
    "classify_exception",
]


@dataclass(slots=True)
class CallRequest:
    """单次模型动作请求（对象 → 哑原子）。

    `schemas` 传给厂商做工具声明；`opts` 是本次动作的参数
    （`max_tokens` / `temperature` / `tool_choice` …，原样透传）。
    """

    messages: list[Message] = field(default_factory=list)
    schemas: list[ToolSchema] = field(default_factory=list)
    opts: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Invocation:
    """一次要执行的动作（**解码产物**：由 `weave.llm.decode` 生成）。

    它只是"把模型的回答读成结构化形状"，对象**不执行**它；
    要不要执行、怎么执行，由应用决定。
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    raw: str | None = None


# kind → 是否可重试。判据由协议固定，实现不得自行解释。
RETRYABLE_KINDS: frozenset[str] = frozenset(
    {"rate_limit", "server", "network", "timeout"}
)
NON_RETRYABLE_KINDS: frozenset[str] = frozenset(
    {"auth", "bad_request", "rejected", "parse_error"}
)


@dataclass(slots=True, frozen=True)
class TypedFailure:
    """类型化失败（跨层的唯一失败信封）。"""

    kind: str
    retryable: bool = False
    message: str = ""
    origin: str = ""  # 失败来源标识，例如 "provider:openai_compat"
    detail: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        known = RETRYABLE_KINDS | NON_RETRYABLE_KINDS
        if self.kind not in known:
            raise ValueError(
                f"TypedFailure.kind={self.kind!r} 不在已知集合内：{sorted(known)}"
            )
        expected = self.kind in RETRYABLE_KINDS
        if self.retryable != expected:
            raise ValueError(
                f"TypedFailure.retryable 与 kind 不一致：kind={self.kind!r} "
                f"要求 retryable={expected}"
            )


def classify_exception(exc: BaseException, origin: str = "") -> TypedFailure:
    """把任意异常归类成 TypedFailure（"失败不抛穿分层"的落地实现）。

    未知异常保守归为 `server`（可重试），由对象的可靠性策略决定是否真的重放。
    """
    from weave.core import errors as E

    kind = "server"
    if isinstance(exc, E.RateLimitError):
        kind = "rate_limit"
    elif isinstance(exc, E.NetworkError):
        kind = "network"
    elif isinstance(exc, (E.ProviderTimeoutError, TimeoutError)):
        kind = "timeout"
    elif isinstance(exc, E.ServerError):
        kind = "server"
    elif isinstance(exc, E.AuthError):
        kind = "auth"
    elif isinstance(exc, E.BadRequestError):
        kind = "bad_request"
    elif isinstance(exc, E.ProviderRejectedError):
        kind = "rejected"
    elif isinstance(exc, E.ParseError):
        kind = "parse_error"
    return TypedFailure(
        kind=kind,
        retryable=kind in RETRYABLE_KINDS,
        message=str(exc) or exc.__class__.__name__,
        origin=origin,
    )
