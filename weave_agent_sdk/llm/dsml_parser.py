"""DSML (DeepSeek Makeup Language) Tool Call Parser.

DeepSeek V4 Pro 返回的 tool call 使用 DSML XML 风格格式而非标准
OpenAI tool_calls JSON。本模块提供纯函数将 DSML 文本解析为 Weave
ToolCall 对象，并从原始内容中移除 DSML 标记。

适配器无关：任何 LLM 适配器（openai、anthropic、未来适配器）在响应
content 中检测到 DSML 标记时均可调用本模块。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from weave_agent_sdk.types import ToolCall

# DSML 使用全角竖线 ｜ (U+FF5C) 作为分隔符，但也兼容 ASCII | (U+007C)
_DSML_SEP = r"[｜|]"  # 全角或半角竖线

# ── DSML 块提取 ──────────────────────────────────────────

# 匹配整个 <｜DSML｜tool_calls> ... </｜DSML｜tool_calls> 块
_TOOL_CALLS_BLOCK_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}tool_calls>\s*"
    r"(.*?)"
    rf"\s*</{_DSML_SEP}DSML{_DSML_SEP}tool_calls>",
    re.DOTALL,
)

# 匹配单个 <｜DSML｜invoke name="..."> ... </｜DSML｜invoke>
_INVOKE_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}invoke\s+name=\"([^\"]+)\">\s*"
    r"(.*?)"
    rf"\s*</{_DSML_SEP}DSML{_DSML_SEP}invoke>",
    re.DOTALL,
)

# 匹配 <｜DSML｜parameter name="..." string="true|false">value</｜DSML｜parameter>
_PARAM_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}parameter\s+"
    r'name="([^"]+)"\s+'
    r'string="(true|false)"'
    r">\s*"
    r"(.*?)"
    rf"\s*</{_DSML_SEP}DSML{_DSML_SEP}parameter>",
    re.DOTALL,
)

# ── Public API ────────────────────────────────────────────

def has_dsml(text: str) -> bool:
    """检测文本中是否包含 DSML tool_calls 标记。"""
    return bool(_TOOL_CALLS_BLOCK_RE.search(text))


def parse_dsml_tool_calls(text: str) -> tuple[list[ToolCall], str]:
    """从文本中解析 DSML tool_calls 并移除标记。

    Args:
        text: LLM 返回的原始 content 文本

    Returns:
        (tool_calls, cleaned_text) 元组：
        - tool_calls: 解析出的 ToolCall 列表
        - cleaned_text: 移除 DSML 标记后的干净文本
    """
    tool_calls: list[ToolCall] = []

    # 1. 提取所有 <DSML|tool_calls> 块
    block = _TOOL_CALLS_BLOCK_RE.search(text)
    if not block:
        return tool_calls, text

    # 2. 块内提取每个 invoke
    for invoke_match in _INVOKE_RE.finditer(block.group(1)):
        tool_name = invoke_match.group(1)
        invoke_body = invoke_match.group(2)
        arguments: dict[str, Any] = {}

        # 3. invoke 内提取每个 parameter
        for param_match in _PARAM_RE.finditer(invoke_body):
            param_name = param_match.group(1)
            is_string = param_match.group(2) == "true"
            raw_value = param_match.group(3).strip()

            if is_string:
                arguments[param_name] = raw_value
            else:
                try:
                    arguments[param_name] = json.loads(raw_value)
                except json.JSONDecodeError:
                    # JSON 解析失败，退回字符串
                    arguments[param_name] = raw_value

        tool_calls.append(ToolCall(
            id=f"dsml_{uuid.uuid4().hex[:12]}",
            name=tool_name,
            arguments=arguments,
        ))

    # 4. 从原文移除整个 DSML 块（含内容），仅保留 DSML 前后的文本
    cleaned = _TOOL_CALLS_BLOCK_RE.sub("", text)
    # 去掉多余空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return tool_calls, cleaned
