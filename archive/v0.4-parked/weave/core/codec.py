"""weave.core.codec — 消息与工具的数据结构编解码（存储/传输边界用）。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from weave.core.types import Message, ToolCall


def msg_to_dict(msg: Message) -> dict[str, Any]:
    """Message → JSON 可序列化 dict（tool_calls 一并转换）。"""
    out: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
    }
    if msg.name is not None:
        out["name"] = msg.name
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.tool_calls:
        out["tool_calls"] = [asdict(tc) for tc in msg.tool_calls]
    return out


def dict_to_msg(raw: dict[str, Any]) -> Message:
    """JSON 可序列化 dict → Message（tool_calls dict → ToolCall）。"""
    tool_calls: list[ToolCall] | None = None
    raw_calls = raw.get("tool_calls")
    if raw_calls:
        tool_calls = [
            ToolCall(
                id=str(tc.get("id", "")),
                name=str(tc.get("name", "")),
                arguments=dict(tc.get("arguments") or {}),
            )
            for tc in raw_calls
        ]
    return Message(
        role=str(raw.get("role", "user")),
        content=str(raw.get("content", "")),
        name=raw.get("name"),
        tool_call_id=raw.get("tool_call_id"),
        tool_calls=tool_calls,
    )
