"""B4: Prompt 注入防御（可选 Feature）。

截断 + 正则检测注入模式 + 防御策略。
"""
from __future__ import annotations

import re
from typing import Any

from weave_agent_sdk.features._prompts import feature_prompt_raw

# 注入检测模式（可按需扩展）。
# 注意：不再包含 r"system\s*:" 这类过宽的模式——任何含 "system:" 的正常用户
# 文本都会被误判为注入（defend 策略会误追加防御指令、reject 策略会误拦截合法
# 输入），误报率偏高（review round-2 issue 1）。
_INJECTION_PATTERNS = [
    r"ignore\s+(all|previous|above)\s+(instructions?|prompts?)",
    r"you\s+are\s+now\s+a\s+(different|new)\s+(role|persona|assistant)",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\[INST\]",
    r"\[/INST\]",
    r"forget\s+(all|everything)\s+(you\s+were\s+told|above)",
]


def sanitize(
    text: str,
    max_length: int = 2000,
    strategy: str = "defend",
    extra_patterns: list[str] | None = None,
) -> tuple[str, bool]:
    """检测并防御 prompt 注入。

    Args:
        text: 用户输入文本
        max_length: 最大长度（超出截断）
        strategy: "defend"（检测到后加防御指令）| "reject"（拒绝输入）| "strip"（移除注入内容）
        extra_patterns: 额外的自定义检测正则
        **llm_kwargs: 传递给 llm.chat() 的额外参数

    Returns:
        (处理后的文本, 是否检测到注入)
    """
    # 1. 截断
    if len(text) > max_length:
        text = text[:max_length]

    # 2. 检测
    patterns = _INJECTION_PATTERNS + (extra_patterns or [])
    detected = False
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            detected = True
            break

    if not detected:
        return text, False

    # 3. 策略
    if strategy == "reject":
        raise ValueError("Prompt injection detected. Input rejected.")
    elif strategy == "strip":
        for pattern in patterns:
            text = re.sub(pattern, "[removed]", text, flags=re.IGNORECASE)
        return text.strip(), True
    else:  # "defend"
        # 防御指令从 .md 模板加载（R2：代码中不硬编码 Prompt；模板路径与
        # 缺失引导统一由 weave.features._prompts 提供，review round-3 issue 3）
        defense = "\n" + feature_prompt_raw("prompt_defense")
        return text + defense, True
