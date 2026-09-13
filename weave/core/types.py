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
    """LLM 对话消息（LLM 面往返的原子单位）。

    `content` 有两种形态：

    - **`str`（语义形态，推荐）**：provider 按厂商要求替你包成内容块，跨厂商可移植。
    - **`dict` / `list[dict]`（原样形态）**：weave **不解释任何 key**，逐字写进该角色的
      内容槽位。这是接厂商独有能力的唯一入口（多模态、`cache_control`、thinking 块回传、
      厂商新增的任意块类型），代价是这些内容**只对当前厂商有效，换 provider 不可移植**。

    原样形态的两条纪律（唯一的校验，见 `payload_content`）：

    1. 每个元素必须是 dict —— weave 不看里面的 key，但拒绝非 dict 的杂物；
    2. 原样 `content` 与 `tool_calls` 同时给出是**歧义**，直接 `TypeError`：
       要么自己把 `tool_use` 块写进 content，要么只用 `tool_calls`。

    注：`role="tool"` 的原样 `content` 是 `tool_result` **内层**内容
    （所以工具返回一张图也能表达）；其它角色的原样 `content` 就是内容数组本身。
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | dict[str, Any] | list[dict[str, Any]] = ""
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


def payload_content(message: Message) -> str | list[dict[str, Any]]:
    """`Message.content` → 厂商 payload 内容槽位的值（两种形态的**唯一定义处**）。

    - `str` → 原样返回（provider 自己包块）；
    - `dict` → 视作单块，包成 `[dict]`；
    - `list[dict]` → **浅拷贝后原样透传**（不解释 key，不排序，不改写）。

    非法输入与歧义在这里一次性拒绝（`TypeError`），不让它流到厂商那里变成难查的 400。
    """
    content = message.content
    if isinstance(content, str):
        return content
    blocks: Any = [content] if isinstance(content, dict) else content
    if not isinstance(blocks, list):
        raise TypeError(
            "Message.content 只能是 str，或 dict / list[dict]（原样形态）；"
            f"收到 {type(content).__name__}"
        )
    bad = sorted({type(block).__name__ for block in blocks if not isinstance(block, dict)})
    if bad:
        raise TypeError(f"Message.content 的原样块必须是 dict，收到：{bad}")
    if message.tool_calls:
        raise TypeError(
            "Message.content 已是原样块数组，就不要再给 tool_calls（歧义）："
            "把 tool_use 块自己写进 content，或者 content 用 str、只用 tool_calls"
        )
    return [dict(block) for block in blocks]


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
    #: 厂商**原始内容块数组**（未解释、未丢块）。厂商返回的 text/thinking/tool_use 之外的块
    #: （多模态、`server_tool_use` 等）只会出现在这里；把它原样回填成
    #: `Message(role="assistant", content=resp.raw_blocks)` 即可让下一轮无损续接
    #: （extended thinking 要求 thinking 块随 tool_use 一起回传，就靠这条闭环）。
    raw_blocks: list[dict[str, Any]] | None = None


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
