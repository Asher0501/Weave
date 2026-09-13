"""usage 归一：各厂商字段名 → 统一口径（清单 H1/H2/H4）。

各家的字段名不一样：
- OpenAI 兼容：`prompt_tokens` / `completion_tokens` / `total_tokens`
- 部分网关：`input_tokens` / `output_tokens`
- 推理 token：`completion_tokens_details.reasoning_tokens` 或 `reasoning_tokens`
- prompt 缓存计数（真实端点实测会遇到）：
  Anthropic `cache_read_input_tokens` / `cache_creation_input_tokens`；
  OpenAI/DeepSeek `prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens`

统一输出（缺字段就不给，不补 0——"没有"和"0"是两回事）：
`{"input": n, "output": m, "total": k, "reasoning": r?, "cache_read": x?, "cache_write": y?}`

注意：`total` 仍按厂商给的口径（通常是 input+output）；**缓存命中的 token 不计入**
`input`（Anthropic 就是这么报的），需要看缓存效果就单独读 `cache_read` / `cache_write`。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = ["normalize_usage", "merge_usage"]

_INPUT_KEYS = ("input", "input_tokens", "prompt_tokens", "promptTokens")
_OUTPUT_KEYS = ("output", "output_tokens", "completion_tokens", "completionTokens")
_TOTAL_KEYS = ("total", "total_tokens", "totalTokens")
_REASONING_KEYS = ("reasoning", "reasoning_tokens", "reasoningTokens")
_CACHE_READ_KEYS = ("cache_read", "cache_read_input_tokens", "prompt_cache_hit_tokens",
                    "cached_tokens", "cacheReadInputTokens")
_CACHE_WRITE_KEYS = ("cache_write", "cache_creation_input_tokens", "cache_write_tokens",
                     "cacheCreationInputTokens")
_DETAIL_CONTAINERS = ("completion_tokens_details", "output_tokens_details",
                      "prompt_tokens_details", "input_tokens_details", "details")


def _pick(raw: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _nested(raw: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for container in _DETAIL_CONTAINERS:
        detail = raw.get(container)
        if isinstance(detail, Mapping):
            found = _pick(detail, keys)
            if found is not None:
                return found
    return None


def normalize_usage(raw: Any) -> dict[str, int]:
    """任意厂商的 usage 结构 → 统一口径（无法识别时返回空 dict）。"""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, int] = {}
    input_tokens = _pick(raw, _INPUT_KEYS)
    output_tokens = _pick(raw, _OUTPUT_KEYS)
    total = _pick(raw, _TOTAL_KEYS)
    reasoning = _pick(raw, _REASONING_KEYS)
    if reasoning is None:
        reasoning = _nested(raw, _REASONING_KEYS)
    cache_read = _pick(raw, _CACHE_READ_KEYS)
    if cache_read is None:
        cache_read = _nested(raw, _CACHE_READ_KEYS)
    cache_write = _pick(raw, _CACHE_WRITE_KEYS)

    if input_tokens is not None:
        out["input"] = input_tokens
    if output_tokens is not None:
        out["output"] = output_tokens
    if total is not None:
        out["total"] = total
    elif input_tokens is not None and output_tokens is not None:
        out["total"] = input_tokens + output_tokens
    if reasoning is not None:
        out["reasoning"] = reasoning
    if cache_read:                      # 0 与"没有"一样对待：不塞噪音字段
        out["cache_read"] = cache_read
    if cache_write:
        out["cache_write"] = cache_write
    return out


def merge_usage(base: Mapping[str, int], extra: Mapping[str, int]) -> dict[str, int]:
    """累加两份归一后的 usage（对象级累计 H3 用）。"""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, int):
            merged[key] = merged.get(key, 0) + value
    return merged
