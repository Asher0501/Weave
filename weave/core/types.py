"""weave.core.types — 共享类型（核心契约的一部分）。

约定：
- 消息/响应/结果全部类型化（先验：不退回裸 dict）；
- 本模块零外部依赖，仅标准库。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── 消息与工具 ──────────────────────────────────────────


@dataclass(slots=True)
class Message:
    """LLM 对话消息（LLM 面往返的原子单位）。"""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    name: str | None = None            # role="tool" 时的工具名
    tool_call_id: str | None = None    # role="tool" 时关联的 tool_call.id
    tool_calls: list["ToolCall"] | None = None  # role="assistant" 时的调用请求


@dataclass(slots=True)
class ToolCall:
    """LLM 发出的工具调用请求。

    `arguments` 保持**厂商原样**（OpenAI 兼容端点给的是 JSON 字符串）：
    解码成 dict 是 LLM 交互对象（`weave.llm.decode`）的职责，原子不解码（D13）。
    """

    id: str
    name: str
    arguments: Any


@dataclass(slots=True)
class ToolSchema:
    """厂商中立工具描述（格式转换在 Provider 侧）。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON Schema object


# ── Provider 产出 ───────────────────────────────────────


@dataclass(slots=True)
class LLMResponse:
    """Provider.complete() 的非流式结果（也是 LLM 交互维度的**唯一输出**）。

    对象层在返回前会补齐：`usage` 归一、`tool_calls` 解码归一、
    `attempts` / `elapsed_ms` 填充；若流式中途失败，`failure` 带上类型化失败，
    同时保留已产出的 `content`（不静默吞掉）。
    """

    content: str = ""
    reasoning: str | None = None       # 推理内容（DeepSeek reasoner 等），绝不回传（T2）
    tool_calls: list[ToolCall] | None = None
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)  # 归一后 {"input": N, "output": M}
    finish_reason: str = "stop"        # "stop" | "tool_calls" | "length" | "error"
    attempts: int = 1                  # 这个动作实际尝试了几次（含重放）
    elapsed_ms: float = 0.0            # 这个动作的总耗时（含退避）
    failure: Any | None = None         # TypedFailure | None（仅"部分成功"时出现）
    raw: dict[str, Any] | None = None  # 厂商原始响应片段，排查用


@dataclass(slots=True)
class StreamChunk:
    """流式事件。kind ∈ {"token","reasoning","tool_call_delta","usage","finish"}。

    - `token` / `reasoning`：文本增量
    - `tool_call_delta`：工具调用增量（按 `index` 归并 id/name/arguments 片段）
    - `usage`：部分厂商只在末尾给用量
    - `finish`：结束（`finish_reason`）

    注：工具型流程用 `complete()`（流式无法安全回放 tool_calls）；逐 token 呈现由调用方消费本类型。
    """

    kind: str = "token"
    text: str = ""
    reasoning: str = ""
    finish_reason: str = ""
    index: int = 0                     # tool_call_delta：第几个调用
    tool_call_id: str = ""             # tool_call_delta：id 片段（通常只在首块出现）
    tool_call_name: str = ""           # tool_call_delta：函数名片段
    arguments_delta: str = ""          # tool_call_delta：arguments JSON 片段
    usage: dict[str, int] | None = None
    model: str = ""
