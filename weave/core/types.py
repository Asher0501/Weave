"""weave.core.types — 共享类型（核心契约的一部分）。

约定：
- 消息/响应/结果全部类型化（先验：不退回裸 dict）；
- 本模块零外部依赖，仅标准库。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ── 内容块（厂商中立的**语义**，不是厂商形状） ──────────


@dataclass(frozen=True, slots=True)
class TextBlock:
    """一段文本。"""

    text: str


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """一张图：给 `url`（远程），或 `data` + `media_type`（base64，**不含 `data:` 前缀**）。

    构造时就校验 —— 不合法立刻 `ValueError`，不让它在发请求时才变成厂商的 400。
    """

    url: str | None = None
    data: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        if bool(self.url) == bool(self.data):
            raise ValueError("ImageBlock 必须且只能给一个：url，或 data（+ media_type）")
        if self.data and not self.media_type:
            raise ValueError("ImageBlock 用 data（base64）时必须给 media_type，如 'image/png'")
        if self.data and self.data.startswith("data:"):
            raise ValueError("ImageBlock.data 只放 base64 本身，不要带 'data:…;base64,' 前缀")


#: 厂商中立内容块 —— **封闭集合**（当前只有文本与图片）。封闭带来两个好处：
#: 写错类型在 IDE/构造时就报；且它永远不可能和 `tool_calls` 撞车（所以两者可以共存）。
Block = TextBlock | ImageBlock


# ── 消息与工具 ──────────────────────────────────────────


@dataclass(slots=True)
class Message:
    """LLM 对话消息（LLM 面往返的原子单位）。

    `content` 有三种形态：

    - **`str`（纯文本，最常用）**：provider 按厂商要求包成文本块，跨厂商可移植。
    - **`Block` / `list[Block]`（中立块，多模态推荐）**：`TextBlock("…")` / `ImageBlock(url=…)`
      —— 你写的是**语义**，形状由适配层生成，所以同一份输入能喂给任何厂商。
    - **`dict` / `list[dict]`（原样形态）**：weave **不解释任何 key**，逐字写进该角色的内容槽位。
      这是接厂商独有能力的入口（`cache_control`、thinking 块回传、厂商新块），代价是这些内容
      **只对当前厂商有效，换 provider 不可移植**。

    三条纪律（唯一定义处见 `payload_content`）：

    1. 列表里**不能混用**中立块与厂商原样块（语义不明 → `TypeError`）；
    2. 原样 `content` 与 `tool_calls` 同时给出是**歧义**（原样块可能本身就是工具调用）→ `TypeError`；
       中立块与 `tool_calls` 可以共存——`Block` 是封闭集合，只含 text / image；
    3. 原样块每个元素必须是 dict —— weave 不看里面的 key，但拒绝非 dict 的杂物。

    注：`role="tool"` 的内容是 `tool_result` **内层**内容（所以工具返回一张图也表达得出来）；
    其它角色的内容就是内容数组本身。
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | Block | list[Block] | dict[str, Any] | list[dict[str, Any]] = ""
    name: str | None = None            # role="tool" 时的工具名
    tool_call_id: str | None = None    # role="tool" 时关联的 tool_call.id
    tool_calls: list["ToolCall"] | None = None  # role="assistant" 时的调用请求

    # ── 回填构造器：把"这次的结果"变成"下次的输入"（纯数据；不执行、不循环、不做策略）──

    @staticmethod
    def tool_result(call: "ToolCall", output: Any = "") -> "Message":
        """构造工具结果消息（`role="tool"`），`name` / `tool_call_id` 自动取自 `call`。

        这是工具往返里最容易写错的一步（`tool_call_id` 写错 → 厂商直接 400），所以只此一处：

        - `output` 是 `str` → 原样（文本内容由调用方决定）；
        - 其它可 JSON 序列化的值 → `json.dumps(..., ensure_ascii=False)`；
          **不对 dict 做隐式结构解释**（那是厂商原样块的活，见类文档）；
        - 不可序列化 → 让 `json.dumps` 抛 `TypeError`（不静默 `str()` 掉）；
        - 工具要返回**图片等原样块**时不要用本方法，显式写
          `Message(role="tool", content=[...块...], tool_call_id=call.id)`。
        """
        content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return Message(role="tool", content=content, name=call.name,
                       tool_call_id=call.id)


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


def payload_content(message: Message) -> str | list["Block"] | list[dict[str, Any]]:
    """`Message.content` → 适配器消费的**规范形态**（三种形态的**唯一定义处**）。

    - `str` → 原样返回（适配器自己包成文本块）；
    - `Block` / `list[Block]` → 返回 `list[Block]`（中立块，适配器翻译成厂商形状）；
    - `dict` / `list[dict]` → **浅拷贝后原样透传**（厂商原样块，不解释 key）。

    非法输入与歧义在这里一次性拒绝（`TypeError`），不让它流到厂商那里变成难查的 400。
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, (TextBlock, ImageBlock)):
        blocks: list[Any] = [content]
    elif isinstance(content, dict):
        blocks = [content]
    elif isinstance(content, list):
        blocks = list(content)
    else:
        raise TypeError(
            "Message.content 只能是 str、Block / list[Block]，或 dict / list[dict]（原样形态）；"
            f"收到 {type(content).__name__}"
        )

    neutral = [block for block in blocks if isinstance(block, (TextBlock, ImageBlock))]
    raw = [block for block in blocks if isinstance(block, dict)]
    if neutral and raw:
        raise TypeError(
            "同一个 content 列表里不能混用「中立块（Block）」与「厂商原样块（dict）」："
            "语义不明——要么全用 Block（可移植），要么全用 dict（绑定当前厂商）"
        )
    unknown = sorted({type(block).__name__ for block in blocks
                      if not isinstance(block, (TextBlock, ImageBlock, dict))})
    if unknown:
        raise TypeError(f"content 列表里只允许 Block 或 dict，收到：{unknown}")
    if raw:
        if message.tool_calls:
            raise TypeError(
                "原样 content 与 tool_calls 同时给出是歧义（原样块可能本身就是工具调用）："
                "要么把工具调用自己写进 content，要么 content 用 str / Block、只用 tool_calls"
            )
        return [dict(block) for block in raw]
    return neutral


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

    def as_message(self) -> Message:
        """把这次响应回填成一条 `assistant` 消息（工具往返的"请求回填"那一步）。

        与 `Message.tool_calls` 是**同一个类型**，所以不需要任何转换；漏掉这次回填，
        下一轮 `role="tool"` 的 `tool_call_id` 就无处对应（Anthropic 会直接 400）。

        给的是**语义形态**（`content` + `tool_calls`），跨厂商可移植。若这一轮用了厂商原样块
        （如 extended thinking 要求 thinking 块随 `tool_use` 一起回传），要保留原始块请**显式**回填
        `Message(role="assistant", content=resp.raw_blocks)`——本方法**不做隐式切换**：
        那会静默改变可移植性，并与"原样块 + `tool_calls` 是歧义"的规则冲突。
        """
        return Message(role="assistant", content=self.content or "", tool_calls=self.tool_calls)


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
