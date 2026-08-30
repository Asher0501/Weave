"""SimpleLoop — 一次请求-响应。

不调用 Tool，不做迭代。用于不需要 tool-use 的简单 Agent 场景。
"""
from __future__ import annotations

import time
from typing import Any

from weave.loop.base import BaseLoop, call_llm, format_memory_context, persist_user_message
from weave.types import LoopResult, Message


class SimpleLoop(BaseLoop):
    """单次 LLM 调用，直接返回结果。

    对应 myKG/bePM 当前的行为模式。
    不涉及 Tool 调用和迭代。
    """

    async def run(self, agent: Any, user_input: str) -> LoopResult:
        t_start = time.perf_counter()

        await self.on_start(agent, user_input)

        # 构建 messages
        system_msg = Message(role="system", content=agent._system_prompt)
        user_msg = Message(role="user", content=user_input)

        # 注入 memory context
        memory_ctx = await self.before_think(agent, user_input)

        # 将 memory context 拼入 system message
        if memory_ctx:
            ctx_text = format_memory_context(memory_ctx)
            system_msg.content = system_msg.content + "\n\n" + ctx_text

        # LLM 调用（流式模式下逐 token emit 事件）
        response = await call_llm(agent, [system_msg, user_msg], None)

        # 延迟提交 user 消息：仅在成功获得 assistant 回复后落盘，避免运行被
        # 超时/取消打断（LLM 挂起被 cancel）时遗留"有 user 无 assistant"的
        # 悬空消息污染后续记忆注入（与 iterative 的延迟提交模式一致，
        # review round-14 issue 2）。
        await persist_user_message(agent, user_input)

        # 将 assistant response 持久化到 stream Memory（after_think）
        await self.after_think(agent, response)

        t_end = time.perf_counter()

        # 从 after_think 记录的写入计数汇总 memory_updated（真实写入，非虚构零值）
        memory_updated: dict[str, dict[str, int]] = {}
        memory_writes = getattr(agent, "__dict__", {}).get("_memory_writes") or {}
        if memory_writes.get("stream"):
            memory_updated["stream"] = memory_writes["stream"]
        if memory_writes.get("state"):
            memory_updated["state"] = memory_writes["state"]

        result = LoopResult(
            output=response.content,
            elapsed_ms=int((t_end - t_start) * 1000),
            iterations=1,
            memory_updated=memory_updated,
        )

        await self.on_end(agent, result)
        return result
