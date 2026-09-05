"""离线 FakeLLM：无 API Key 也能跑通整条链路。

实现 weave.llm.base.BaseLLM 的 chat / chat_stream。不写死任何人设数据——
人设名与角色从已渲染的 system prompt 里提取，问题从 user 消息里提取。
"""
from __future__ import annotations

import re
from typing import Any

from weave_agent_sdk.llm.base import BaseLLM, LLMResponse


class FakeLLM(BaseLLM):
    """离线桩 LLM。真实模型请用 main.py 的 --real 切换。"""

    async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7) -> LLMResponse:
        system = messages[0].content if messages else ""
        name = _extract(system, r"你是(.+?)。") or "AI"
        role = _extract(system, r"人设定位：(.+?)。") or ""

        question = ""
        for m in reversed(messages):
            if getattr(m, "role", None) == "user":
                question = m.content
                break

        content = (
            f"[{name}] 我（{role}）收到客观任务「{question}」。"
            f"我的判断：{question}（离线桩回答，加 --real 使用真实模型）。"
        )
        return LLMResponse(content=content)

    async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        resp = await self.chat(messages, tools, max_tokens, temperature)
        for ch in resp.content:
            yield ch


def _extract(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None
