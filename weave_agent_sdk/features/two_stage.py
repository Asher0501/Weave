"""B3: 两阶段 LLM 流水线（可选 Feature）。

Stage 1: NL 理解（无 Schema 约束 → 自由表达）
Stage 2: 翻译（Schema 约束 → 精准 JSON）
"""
from __future__ import annotations

from typing import Any

from weave_agent_sdk.features._prompts import feature_prompt
from weave_agent_sdk.features.structured_call import structured_call
from weave_agent_sdk.types import Message


async def two_stage_call(
    llm: Any,
    understand_prompt: str,
    translate_schema: type,
    user_input: str,
    understand_system: str | None = None,
    translate_system: str | None = None,
    max_retries: int = 3,
    **llm_kwargs: Any,
) -> Any:
    """两阶段流水线：先 NL 理解，再 Schema 翻译。

    Args:
        llm: BaseLLM 实例
        understand_prompt: Stage 1 的 prompt（描述"要理解什么"）
        translate_schema: Stage 2 的目标 Pydantic model
        user_input: 原始用户输入
        understand_system: Stage 1 的 system prompt；缺省（None）时从
            prompts/features/two_stage_understand.md 加载（R2：不硬编码，
            宿主可传入覆盖）
        translate_system: Stage 2 的 system prompt；缺省（None）时从
            prompts/features/two_stage_translate.md 加载（R2：不硬编码，
            宿主可传入覆盖）
        max_retries: 最大重试次数
        **llm_kwargs: 传递给 llm.chat() 的额外参数

    Returns:
        translate_schema 的实例
    """
    # 默认 Prompt 从 .md 模板加载（R2：代码中不硬编码完整可执行 Prompt；
    # 模板路径与缺失引导统一由 weave.features._prompts 提供，
    # review round-3 issue 3）。
    if understand_system is None:
        understand_system = feature_prompt("two_stage_understand")
    if translate_system is None:
        translate_system = feature_prompt("two_stage_translate")

    # Stage 1: 意图理解（自由文本，不约束 Schema）
    stage1_messages = [
        Message(role="system", content=understand_system),
        Message(role="user", content=f"{understand_prompt}\n\nUser input: {user_input}"),
    ]
    stage1_response = await llm.chat(messages=stage1_messages, **llm_kwargs)
    understanding = stage1_response.content

    # Stage 2: 翻译为结构化 JSON
    # 阶段 2 的 user 桥接指令从 prompts/features/two_stage_bridge.md 模板加载
    # （R2：代码中绝不硬编码可执行 Prompt；round-2 R2 修复只迁移了两个 system
    #  prompt，该 user 桥接指令属修复范围遗漏——与 round-3 issue 1 的
    #  json_extract.to_feedback 同类，review round-8 issue 2），用
    # {{ understanding }} 注入 Stage 1 的理解结果。
    stage2_messages = [
        Message(role="system", content=translate_system),
        Message(role="user", content=feature_prompt("two_stage_bridge", understanding=understanding)),
    ]
    return await structured_call(
        llm=llm,
        prompt=stage2_messages[-1].content,
        schema=translate_schema,
        messages=[stage2_messages[0]],
        max_retries=max_retries,
        **llm_kwargs,
    )
