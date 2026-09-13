"""示例：业务自有的翻译循环（调用方代码，不在 weave 包内）。

weave 只提供原子件（Provider / StateStore / ToolRegistry / EventSink）；
"要不要循环、怎么循环、轮次写哪里"由调用方自行组装。下面是最小示例：
调度 + 工具翻译 +（可选手选位置）轮次持久化，返回示例结果对象。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from weave.core.codec import msg_to_dict
from weave.core.errors import ToolError, WeaveError
from weave.core.interfaces import EventSink, Provider, StateStore, ToolRegistry
from weave.core.types import Message
from .context_builder import ContextBuilder


def _serialize(result: Any) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError:
        return str(result)


@dataclass(slots=True)
class Outcome:
    output: str = ""
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    writes: dict[tuple[str, str], int] = field(default_factory=dict)


async def run_turn(
    *,
    agent_id: str,
    provider: Provider,
    state: StateStore,
    context_builder: ContextBuilder,
    tools: ToolRegistry | None,
    initial_input: str,
    sink: EventSink | None = None,
    locate_history: Callable[[str], tuple[str, str]] | None = None,
    max_iterations: int = 10,
    max_consecutive_tool_errors: int = 3,
    **kwargs: Any,
) -> Outcome:
    """一次完整回合：持久化 user(若选址) → 投影 → complete → 工具翻译 → 结束。"""
    location: tuple[str, str] | None = (
        locate_history(agent_id) if locate_history is not None else None
    )
    writes: dict[tuple[str, str], int] = {}
    usage: dict[str, int] = {"input": 0, "output": 0}

    async def _append(msg: Message) -> None:
        if location is None:
            return
        ns, key = location
        await state.append(ns, key, msg_to_dict(msg))
        writes[location] = writes.get(location, 0) + 1

    if sink:
        await sink.emit("loop_start", {"agent_id": agent_id, "input": initial_input})
    await _append(Message(role="user", content=initial_input))

    messages: list[Message] = await context_builder.build(
        agent_id=agent_id, state=state, current_input=initial_input, **kwargs
    )
    schemas = tools.get_schemas() if tools is not None else None
    iterations = 0
    output = ""
    consecutive_errors = 0

    while True:
        if sink:
            await sink.emit("iteration_start", {"iteration": iterations})
        response = await provider.complete(messages, schemas, **kwargs)
        iterations += 1
        for k, v in (response.usage or {}).items():
            usage[k] = usage.get(k, 0) + (v or 0)

        assistant = Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        await _append(assistant)
        messages.append(assistant)
        if sink:
            await sink.emit(
                "llm",
                {
                    "model": getattr(provider, "model", "") or response.model,
                    "finish_reason": response.finish_reason,
                    "tool_calls": len(response.tool_calls or []),
                },
            )

        tool_calls = response.tool_calls or []
        if not tool_calls or iterations >= max_iterations:
            output = response.content
            break

        for tc in tool_calls:
            if tools is None:
                break
            try:
                result = await tools.execute(tc.name, tc.arguments)
                consecutive_errors = 0
                tool_text, tool_error = _serialize(result), None
            except ToolError as e:
                consecutive_errors += 1
                tool_error = str(e)
                tool_text = f"[tool error] {tool_error}"
            tool_msg = Message(role="tool", content=tool_text, name=tc.name, tool_call_id=tc.id)
            await _append(tool_msg)
            messages.append(tool_msg)
            if sink:
                await sink.emit("tool", {"name": tc.name, "ok": tool_error is None, "error": tool_error})
            if consecutive_errors > max_consecutive_tool_errors:
                raise WeaveError(f"too many consecutive tool errors (>{max_consecutive_tool_errors})")

    if sink:
        await sink.emit("loop_end", {"output": output, "iterations": iterations})
    return Outcome(output=output, iterations=iterations, usage=usage, writes=writes)
