"""LLM 类型化错误。

区分不同的 LLM 错误类型，使调用方能精确判断"该不该重试"。
"""
from __future__ import annotations


class WeaveLLMError(Exception):
    """LLM 错误的基类。所有 LLM 相关异常继承自此。"""
    pass


class RateLimitError(WeaveLLMError):
    """HTTP 429 — 速率限制。

    可重试：等待 backoff 后重试。
    """
    pass


class AuthError(WeaveLLMError):
    """HTTP 401 / 403 — 认证失败。

    不可重试：API Key 无效，重试无意义。
    """
    pass


class ContextLengthError(WeaveLLMError):
    """Prompt 超出模型上下文窗口。

    不可直接重试：需要裁剪 prompt 或减少消息数量。
    """
    pass


class ServerError(WeaveLLMError):
    """HTTP 5xx — 服务端错误。

    可重试：临时性的服务端问题。
    """
    pass


class NetworkError(WeaveLLMError):
    """连接失败 / DNS 解析失败 / 超时。

    可重试：网络抖动。
    """
    pass


class BadRequestError(WeaveLLMError):
    """HTTP 400 — 请求参数错误。

    不可重试：参数本身有问题。
    """
    pass


# ── 错误分类工具 ─────────────────────────────────────────

def classify_http_error(status_code: int, message: str = "") -> WeaveLLMError:
    """根据 HTTP 状态码返回对应的类型化异常。

    Args:
        status_code: HTTP 状态码
        message: 错误描述

    Returns:
        对应的 WeaveLLMError 子类实例
    """
    if status_code == 429:
        return RateLimitError(message)
    elif status_code in (401, 403):
        return AuthError(message)
    elif status_code in (400, 422):
        return BadRequestError(message)
    elif 500 <= status_code < 600:
        return ServerError(message)
    return WeaveLLMError(message)
