"""示例：业务自有的上下文投影器（组装成 list[Message]，供示例循环使用）。

weave 不提供默认 builder——"上下文放什么、system 写什么、历史读哪里"都是调用方代码。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from weave.core.codec import dict_to_msg
from weave.core.interfaces import StateStore
from weave.core.types import Message

# 示例默认 system（本示例自带的业务文案，不是 weave 的）
EXAMPLE_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer concisely."
)


class ContextBuilder:
    """把 历史（可选手选位置）+ 当前输入 组装为消息列表。"""

    def __init__(
        self,
        *,
        system_prompt: str | None = EXAMPLE_SYSTEM_PROMPT,
        history_limit: int | None = 40,
        locate_history: Callable[[str], tuple[str, str]] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._history_limit = history_limit
        self._locate_history = locate_history

    async def build(
        self,
        *,
        agent_id: str,
        state: StateStore,
        current_input: str | None,
        **context: Any,
    ) -> list[Message]:
        messages: list[Message] = []
        if self._system_prompt:
            messages.append(Message(role="system", content=self._system_prompt))
        if self._locate_history is not None:
            namespace, key = self._locate_history(agent_id)
            stored = await state.get(namespace, key)
            if stored:
                history = [dict_to_msg(raw) for raw in stored if isinstance(raw, dict)]
                if self._history_limit is not None and len(history) > self._history_limit:
                    history = history[-self._history_limit :]
                messages.extend(history)
        tail_is_same_user = bool(
            messages
            and messages[-1].role == "user"
            and current_input is not None
            and messages[-1].content == current_input
        )
        if current_input and not tail_is_same_user:
            messages.append(Message(role="user", content=current_input))
        return messages
