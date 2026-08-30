"""B2: 结构化 LLM 调用（可选 Feature）。

一键完成: LLM 调用 → JSON 提取 → Schema 校验 → 失败反馈重试。
"""
from __future__ import annotations

import logging
from typing import Any

from weave.features._prompts import feature_prompt
from weave.utils.json_extract import extract_json
from weave.features.schema_validation import validate_schema
from weave.types import Message

logger = logging.getLogger(__name__)


def _schema_feedback(validation_err) -> tuple[str, str]:
    """从 prompts/features/structured_call_schema.md 加载反馈标签与修复指令。

    模板契约已放宽（review round-3 issue 4）：首行为 header、末行为修复指令，
    中间按标签前缀匹配各错误类别（Missing / Type / Unknown|Extra），不再依赖
    固定 5 行或严格行序——宿主增删说明行、重排标签行都不会崩溃或错位。某类别
    标签缺失时跳过该类别的反馈（不拼入，避免误导 LLM 修复方向）并记录告警。

    Returns:
        (last_error, fix_instruction)
    """
    lines = [
        ln.strip()
        for ln in feature_prompt("structured_call_schema").splitlines()
        if ln.strip()
    ]
    if len(lines) < 2:
        raise ValueError(
            "prompts/features/structured_call_schema.md must contain at least "
            "2 non-empty lines (header + fix instruction)"
        )
    header = lines[0]
    fix = lines[-1]

    # 按前缀匹配各错误类别标签，容忍额外/缺失/重排行（不依赖固定行序）。
    labels: dict[str, str] = {}
    for ln in lines[1:-1]:
        low = ln.lower()
        if low.startswith("missing"):
            labels.setdefault("missing", ln)
        elif low.startswith("type"):
            labels.setdefault("type", ln)
        elif low.startswith(("unknown", "extra")):
            labels.setdefault("extra", ln)

    feedback_parts = [header]
    for key, err_list in (
        ("missing", validation_err.missing),
        ("type", validation_err.type_errors),
        ("extra", validation_err.extra),
    ):
        if err_list:
            label = labels.get(key)
            if label:
                feedback_parts.append(f"{label} {err_list}")
            else:
                logger.warning(
                    "structured_call_schema.md has no label for '%s' errors; "
                    "skipping that feedback category (check prompts/features/"
                    "structured_call_schema.md)", key,
                )

    return "; ".join(feedback_parts), fix


async def structured_call(
    llm: Any,
    prompt: str,
    schema: type,
    messages: list[Message] | None = None,
    max_retries: int = 3,
    **llm_kwargs: Any,
) -> Any:
    """调用 LLM 获取符合 Schema 的结构化输出，失败时自动重试。

    Args:
        llm: BaseLLM 实例
        prompt: 用户的输入 prompt
        schema: Pydantic BaseModel 子类
        messages: 已有的对话消息（可选）
        max_retries: 最大重试次数
        **llm_kwargs: 传递给 llm.chat() 的额外参数

    Returns:
        schema 的实例

    Raises:
        ValueError: 所有重试失败
    """
    import json

    all_messages = list(messages) if messages else []
    all_messages.append(Message(role="user", content=prompt))

    last_error = None

    for attempt in range(max_retries + 1):
        response = await llm.chat(messages=all_messages, **llm_kwargs)

        try:
            data = extract_json(response.content)
        except Exception as e:
            last_error = str(e)
            # 把错误喂回 LLM（反馈 prompt 从 .md 模板加载，R2；
            # 模板路径与缺失引导统一由 weave.features._prompts 提供，
            # review round-3 issue 3）
            all_messages.append(Message(role="assistant", content=response.content))
            all_messages.append(Message(
                role="user",
                content=feature_prompt("structured_call_invalid", error=last_error),
            ))
            continue

        instance, validation_err = validate_schema(data, schema)
        if validation_err.is_valid:
            return instance

        # Schema 不匹配，生成反馈（标签与修复指令均来自 .md 模板，R2）
        last_error, fix_instruction = _schema_feedback(validation_err)
        all_messages.append(Message(role="assistant", content=response.content))
        all_messages.append(Message(
            role="user",
            content=f"{last_error}\n{fix_instruction}",
        ))

    raise ValueError(f"Structured call failed after {max_retries} retries. Last error: {last_error}")
