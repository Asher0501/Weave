"""weave.core — 契约层（LLM 交互维度唯一的稳定面）。

- `types`       基础词汇：Message / ToolCall / ToolSchema / LLMResponse / StreamChunk
- `envelopes`   信封：CallRequest / Invocation / TypedFailure（+ 再导出基础词汇）
- `errors`      类型化错误（只保留厂商调用真的会产生的那几种）
- `interfaces`  接口：LLMProvider（模型）· StateStore（KV）

本层零第三方依赖，且不 import 任何上层模块（CI 门禁）。
"""
from weave.core.envelopes import (
    NON_RETRYABLE_KINDS,
    RETRYABLE_KINDS,
    CallRequest,
    Invocation,
    TypedFailure,
    classify_exception,
)
from weave.core.errors import (
    AuthError,
    BadRequestError,
    NetworkError,
    NonRetryableProviderError,
    ParseError,
    ProviderError,
    ProviderRejectedError,
    ProviderTimeoutError,
    RateLimitError,
    RetryableProviderError,
    ServerError,
    WeaveError,
    classify_http_error,
)
from weave.core.interfaces import LLMProvider, StateStore
from weave.core.types import (
    Block,
    ImageBlock,
    LLMResponse,
    Message,
    StreamChunk,
    TextBlock,
    ToolCall,
    ToolSchema,
)

__all__ = [
    # types
    "Message",
    "Block",
    "TextBlock",
    "ImageBlock",
    "ToolCall",
    "ToolSchema",
    "LLMResponse",
    "StreamChunk",
    # envelopes
    "CallRequest",
    "Invocation",
    "TypedFailure",
    "RETRYABLE_KINDS",
    "NON_RETRYABLE_KINDS",
    "classify_exception",
    # errors
    "WeaveError",
    "ProviderError",
    "RetryableProviderError",
    "RateLimitError",
    "ServerError",
    "NetworkError",
    "NonRetryableProviderError",
    "AuthError",
    "BadRequestError",
    "ProviderRejectedError",
    "ProviderTimeoutError",
    "ParseError",
    "classify_http_error",
    # interfaces
    "LLMProvider",
    "StateStore",
]
