"""weave.llm —— LLM 交互（weave 唯一的功能）。

对象只有一个输入和一个输出：

    import weave

    # 最常见：直接说要用哪个模型，provider 由 weave 内部建好
    w = weave.llm(model="deepseek-chat", timeout=30, retry=3, observer=my_log)
    resp = await w.call(messages, tools=[...])       # → LLMResponse

    # 需要自己接厂商时，注入自己的哑原子（测试、私有网关、别的厂商）
    w = weave.llm(provider=MyProvider(), timeout=30)

`OpenAIHTTPProvider` 只是默认装配的那一个（零第三方依赖）；它的细节
（base_url / api_key / headers / transport / 参数过滤）都能从 `weave.llm(...)` 直接传，
不必先自己 new 一个 provider。

内部四块：`reliability`（超时/重放/退避/分类）· `decode`（输出解码归一）·
`usage`（计量归一）· `streaming`（增量聚合）；对外**只有 call / call_streaming / stream / aclose**。

完备清单与验收标准见 `docs/weave-llm-dimension.md`。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from weave.core.interfaces import LLMProvider
from weave.llm.client import LLMCallError, LLMClient
from weave.llm.decode import (
    AutoDecoder,
    DecodeResult,
    DsmlDecoder,
    JsonBlockDecoder,
    NativeDecoder,
    has_dsml,
)
from weave.llm.reliability import ReliabilityPolicy
from weave.llm.streaming import StreamAccumulator
from weave.llm.usage import merge_usage, normalize_usage

__all__ = [
    "llm",
    "coerce_provider",
    "LLMClient",
    "LLMCallError",
    "ReliabilityPolicy",
    "StreamAccumulator",
    "AutoDecoder",
    "NativeDecoder",
    "JsonBlockDecoder",
    "DsmlDecoder",
    "DecodeResult",
    "has_dsml",
    "normalize_usage",
    "merge_usage",
]

#: 默认实现要用的厂商端点（延迟 import：只走注入路径时不加载）
#: 支持的协议（决定 weave.llm(model=…) 装配哪个 provider）
PROTOCOLS = ("openai", "anthropic")


def coerce_provider(provider: Any) -> LLMProvider:
    """确认拿到的是哑原子 `LLMProvider`；不是就给一条能看懂的报错。"""
    if isinstance(provider, LLMProvider):
        return provider
    raise TypeError(
        f"{type(provider).__name__} 不是 LLMProvider：请传入实现了 "
        "complete(request) 的哑原子，或改用 weave.llm(model=...) 让 weave 自己装配"
    )


def _build_default_provider(
    *,
    protocol: str,
    model: str,
    base_url: str | None,
    api_key: str | Callable[[], str | None] | None,
    api_key_env: Sequence[str] | None,
    headers: dict[str, str] | None,
    transport: Any,
    path: str | None,
    http_timeout: float | None,
    extra_params: dict[str, Any] | None,
    param_filter: bool,
    include_usage: bool,
) -> LLMProvider:
    """按协议装配默认 provider（延迟 import：只走注入路径时不加载任何实现）。"""
    common: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "headers": headers,
        "transport": transport,
        "timeout": http_timeout,
        "extra_params": extra_params,
        "param_filter": param_filter,
    }
    if api_key_env is not None:
        common["api_key_env"] = tuple(api_key_env)
    if path is not None:
        common["path"] = path

    if protocol == "anthropic":
        from weave.providers.anthropic_http import AnthropicHTTPProvider

        return AnthropicHTTPProvider(**common)

    from weave.providers.openai_http import OpenAIHTTPProvider

    return OpenAIHTTPProvider(include_usage=include_usage, **common)


def llm(
    provider: LLMProvider | None = None,
    *,
    # ── 直接指定模型（provider 由 weave 装配） ──
    model: str | None = None,
    protocol: str = "openai",        # "openai"（默认）| "anthropic"（原生 Messages API）
    base_url: str | None = None,
    api_key: str | Callable[[], str | None] | None = None,
    api_key_env: Sequence[str] | None = None,   # None = 用该协议的默认环境变量名
    headers: dict[str, str] | None = None,
    transport: Any = None,
    path: str | None = None,          # None = 用该协议的默认路径
    http_timeout: float | None = None,
    extra_params: dict[str, Any] | None = None,
    param_filter: bool = True,
    include_usage: bool = True,
    # ── 一次动作的可靠性 ──
    timeout: float | None = 30.0,
    total_timeout: float | None = 120.0,
    retry: int = 3,
    backoff: float = 0.5,
    max_backoff: float = 8.0,
    jitter: float = 0.1,
    respect_retry_after: bool = True,
    # ── 解码与观测 ──
    observer: Any = None,
    strict_decode: bool = True,
    decoder: AutoDecoder | None = None,
    **default_opts: Any,
) -> LLMClient:
    """装配一个 LLM 交互对象（构造器形态；无全局可变状态）。

    两种用法：

    1. **直接给模型**（最常见）——provider 由 weave 装配：
       `weave.llm(model="deepseek-chat", api_key=...)`（默认 OpenAI 兼容协议）
       `weave.llm(model="claude-sonnet-4-20250514", protocol="anthropic")`（Anthropic 原生）
    2. **注入自己的 provider**——测试、私有网关、别的协议：
       `weave.llm(provider=MyProvider())`

    Args:
        provider: 哑原子；给了它就不再使用 `model` 等装配参数。
        model: 模型名；给了它就不必自己 new provider。
        protocol: `"openai"`（默认，OpenAI 兼容 `/chat/completions`）或
            `"anthropic"`（原生 `/v1/messages`：system 是顶层参数、tool 结果是内容块）。
        base_url / api_key / api_key_env / headers / transport / path / http_timeout /
        extra_params / param_filter / include_usage: 透传给默认 provider 的细节
        （`http_timeout` 是**传输层安全网**，与下面的 `timeout` 不是一回事）。
        timeout: 单次尝试超时（秒）；None = 不限。
        total_timeout: 整个动作（含重放与退避）上限；None = 不限。
        retry: 首次之外的重放次数（同一请求的重放）。
        backoff / max_backoff / jitter: 退避参数。
        respect_retry_after: 是否尊重厂商的 Retry-After。
        observer: 事件回调（同步/异步皆可；默认不上报）。
        strict_decode: True 时结构损坏的解码直接报 parse_error；False 时跳过该条。
        **default_opts: 每次调用都会带的默认参数（如 max_tokens / temperature）。
    """
    if provider is not None and model is not None:
        raise TypeError("weave.llm() 只能二选一：给 provider= 或给 model=")
    if provider is None:
        if not model:
            raise TypeError(
                "weave.llm() 需要 model=（让 weave 装配默认 provider）"
                "或 provider=（注入你自己的哑原子）"
            )
        if protocol not in PROTOCOLS:
            raise TypeError(f"未知 protocol={protocol!r}；可选：{PROTOCOLS}")
        provider = _build_default_provider(
            protocol=protocol,
            model=model,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            headers=headers,
            transport=transport,
            path=path,
            http_timeout=http_timeout,
            extra_params=extra_params,
            param_filter=param_filter,
            include_usage=include_usage,
        )
    return LLMClient(
        coerce_provider(provider),
        policy=ReliabilityPolicy(
            timeout=timeout,
            total_timeout=total_timeout,
            retries=retry,
            backoff=backoff,
            max_backoff=max_backoff,
            jitter=jitter,
            respect_retry_after=respect_retry_after,
        ),
        observer=observer,
        decoder=decoder,
        strict_decode=strict_decode,
        default_opts=default_opts,
    )
