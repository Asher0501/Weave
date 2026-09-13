"""weave.core.errors — 类型化错误。

只要**厂商调用真的会产生**的那些：限流 / 服务端 / 网络 / 认证 / 请求非法 /
业务拒绝 / 超时（含"可重试"与"不可重试"两条分叉）。
归档维度的错误类型（工具/目录/存储）与已删除的 legacy 重试耗尽，都随那批代码一起归档。
"""
from __future__ import annotations


class WeaveError(Exception):
    """weave 异常基类。"""


# ── Provider 错误 ───────────────────────────────────────


class ProviderError(WeaveError):
    """Provider 相关错误基类。"""


class RetryableProviderError(ProviderError):
    """可重试的 Provider 错误（429 / 5xx / 网络）。重放由 `weave.llm` 对象执行。"""


class RateLimitError(RetryableProviderError):
    """HTTP 429 — 限流（可能带 Retry-After）。"""


class ServerError(RetryableProviderError):
    """HTTP 5xx — 服务端错误。"""


class NetworkError(RetryableProviderError):
    """连接失败 / DNS / 连接重置类网络错误。"""


class NonRetryableProviderError(ProviderError):
    """不可重试的 Provider 错误（重放无意义）。"""


class AuthError(NonRetryableProviderError):
    """HTTP 401/403 — 认证失败。"""


class BadRequestError(NonRetryableProviderError):
    """HTTP 400/422 — 请求参数错误。"""


class ProviderRejectedError(NonRetryableProviderError):
    """业务性拒绝（HTTP 402 等）：模型侧的业务裁决，直接抛出。"""


class ProviderTimeoutError(ProviderError):
    """调用超时（单次尝试被对象层的 wait_for 掐断，或传输层超时）。"""


# ── 解码错误（对象内部会产生） ──────────────────────────


class ParseError(WeaveError):
    """模型输出无法解码成结构化调用（结构损坏到无法继续）。"""


# ── 分类工具 ────────────────────────────────────────────


def classify_http_error(status_code: int, message: str = "") -> ProviderError:
    """HTTP 状态码 → 类型化错误。"""
    if status_code == 429:
        return RateLimitError(message)
    if status_code in (401, 403):
        return AuthError(message)
    if status_code in (400, 422):
        return BadRequestError(message)
    if status_code == 402:
        return ProviderRejectedError(message)
    if 500 <= status_code < 600:
        return ServerError(message)
    return ProviderError(message or f"HTTP {status_code}")
