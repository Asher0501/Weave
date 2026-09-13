"""A1: LLM JSON 提取。

从 LLM 返回的文本中提取并解析 JSON。
处理 markdown 代码围栏、自然语言混排、格式错误。
"""
from __future__ import annotations

import json
import re
from typing import Any

# ── 单 JSON 对象提取 ─────────────────────────────────────

# 匹配 ```json ... ``` 代码块
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | list[Any]:
    """从 LLM 文本中提取 JSON 对象或数组。

    处理策略（按优先级）:
    1. 正则提取 ```json ... ``` 代码块
    2. 查找最外层 { 或 [ 边界
    3. json.loads() 解析

    Args:
        text: LLM 返回的原始文本（可能包含 markdown 围栏和自然语言）

    Returns:
        解析后的 dict 或 list

    Raises:
        JsonExtractError: 无法提取或解析 JSON 时
    """
    # 策略 1: 代码块提取
    match = _FENCE_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        try:
            return _try_parse(candidate)
        except json.JSONDecodeError:
            pass  # 代码块内的不是合法 JSON，回退到策略 2

    # 策略 2: 边界定位
    text_stripped = text.strip()

    # 找 JSON 对象
    obj_start = text_stripped.find("{")
    obj_end = text_stripped.rfind("}")
    if obj_start >= 0 and obj_end > obj_start:
        candidate = text_stripped[obj_start:obj_end + 1]
        try:
            return _try_parse(candidate)
        except json.JSONDecodeError as e:
            raise JsonExtractError(
                f"Failed to parse extracted JSON object: {e}",
                raw_preview=candidate[:200],
            )

    # 找 JSON 数组
    arr_start = text_stripped.find("[")
    arr_end = text_stripped.rfind("]")
    if arr_start >= 0 and arr_end > arr_start:
        candidate = text_stripped[arr_start:arr_end + 1]
        try:
            return _try_parse(candidate)
        except json.JSONDecodeError as e:
            raise JsonExtractError(
                f"Failed to parse extracted JSON array: {e}",
                raw_preview=candidate[:200],
            )

    raise JsonExtractError(
        "No JSON object or array found in LLM response",
        raw_preview=text[:500],
    )


def _try_parse(text: str) -> dict[str, Any] | list[Any]:
    """尝试解析 JSON，失败时尝试修复常见问题。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 修复：去除尾部逗号
        fixed = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(fixed)


def extract_json_block(text: str) -> dict[str, Any] | list[Any]:
    """从自然语言 + JSON 混合文本中提取第一个 JSON 代码块。

    适用于 LLM 返回的"解释文字 + JSON"格式。
    比 extract_json() 更严格——只提取代码块，不回退到全文搜索。

    Args:
        text: 混合文本

    Returns:
        解析后的 dict 或 list

    Raises:
        JsonExtractError: 无代码块或解析失败
    """
    match = _FENCE_RE.search(text)
    if not match:
        raise JsonExtractError(
            "No JSON code block found in text",
            raw_preview=text[:500],
        )

    candidate = match.group(1).strip()
    try:
        return _try_parse(candidate)
    except json.JSONDecodeError as e:
        raise JsonExtractError(
            f"Failed to parse JSON code block: {e}",
            raw_preview=candidate[:200],
        )


# ── 错误类型 ─────────────────────────────────────────────


class JsonExtractError(Exception):
    """JSON 提取/解析失败。

    携带错误上下文（原始消息与原文预览），供调用方在必要时构造
    面向 LLM 的反馈。反馈 Prompt 文本由调用方从 .md 模板加载
    （R2：代码中绝不硬编码 Prompt），本类只承载结构化错误数据。
    """

    def __init__(self, message: str, raw_preview: str = ""):
        super().__init__(message)
        self.raw_preview = raw_preview
