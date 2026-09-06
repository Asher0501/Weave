"""Weave 共享类型定义。

所有模块依赖的基础类型，零外部依赖。
"""
from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any

# ── Message ──────────────────────────────────────────────


@dataclass(slots=True)
class Message:
    """LLM 对话消息。"""
    role: str          # "system" | "user" | "assistant" | "tool"
    content: str       # 文本内容
    name: str | None = None       # tool name（role="tool" 时）
    tool_call_id: str | None = None  # tool call 关联 ID
    tool_calls: list["ToolCall"] | None = None  # assistant 消息的 tool 调用请求


# ── ToolCall ─────────────────────────────────────────────


@dataclass(slots=True)
class ToolCall:
    """LLM 返回的 tool 调用请求。"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    """Tool 执行结果（成功或失败）。"""
    tool_call_id: str
    name: str
    result: Any = None       # 成功时的返回值
    error: str | None = None  # 失败时的错误信息


# ── LoopResult ───────────────────────────────────────────

# memory_updated: {"stream": {"session:abc:stream": 5}, "state": {...}}
# 记录本次 run 中各 namespace 的写入条数
LoopResult = namedtuple("LoopResult", [
    "output",            # str — 最终文本输出
    "elapsed_ms",        # int — 总耗时（毫秒）
    "iterations",        # int — LLM 调用次数
    "memory_updated",    # dict[str, dict[str, int]] — access_type → namespace → 写入条数
])


# ── SearchResult ─────────────────────────────────────────

SearchResult = namedtuple("SearchResult", [
    "id",        # str — 唯一标识
    "content",   # str — 匹配内容
    "score",     # float — 相关度分数 (0.0 ~ 1.0)
    "metadata",  # dict — 附加信息
])


# ── Memory Entry ─────────────────────────────────────────


@dataclass(slots=True)
class MemoryEntry:
    """一条记忆记录。"""
    id: str
    namespace: str          # 隔离键 "{scope}:{scope_id}:{access_type}"
    access_type: str        # "stream" | "state" | "knowledge"
    key: str | None         # state 模式下的键
    content: str            # 存储层表示（JSON 序列化字符串）；用户取值走 state.get / stream.last，不直接消费此字段
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float | None = None  # None = 永不过期


# ── Event Types ──────────────────────────────────────────


@dataclass(slots=True)
class WeaveEvent:
    """事件总线事件。

    stream() 事件类型与 schema 遵循 basic.md §5 / public-api.md §1.6
    的不可变契约：
        token       — {"text": str, "index": int}
        tool_call   — {"name": str, "arguments": dict}
        tool_result — {"name": str, "result": Any, "error": bool}
        done        — {"output": str, "elapsed_ms": int, "iterations": int}
        error       — {"message": str, "exception": str}
    """
    type: str               # "token" | "tool_call" | "tool_result" | "done" | "error"
    data: dict[str, Any]
    timestamp: float = 0.0


# ── Config Types ─────────────────────────────────────────

# 默认 LLM provider——单点声明，避免 types/config/factory 多处硬编码
# "anthropic"；切换默认 provider 只需改这一处。
DEFAULT_PROVIDER = "anthropic"


@dataclass(slots=True)
class LLMConfig:
    """LLM 配置。

    model 从 weave.yaml 或环境变量读取，代码中不硬编码模型名（R3）。
    """
    provider: str = DEFAULT_PROVIDER       # anthropic | openai
    model: str = ""                   # 从 weave.yaml 或环境变量读取，不硬编码默认值
    max_tokens: int = 4096
    temperature: float = 0.7
    api_key: str | None = None       # 不存文件，运行时从 env 注入
    base_url: str | None = None


@dataclass(slots=True)
class LoopConfig:
    """Loop 配置。

    Attributes:
        type: 循环策略类型（simple | iterative | scheduled）
        max_iterations: 最大迭代次数
        stop_conditions: 停止条件列表
        schedule: cron 表达式（scheduled loop）
        timeout: stream() 等待后台任务收尾的宽限时长（秒）——仅在收到
            done/error 事件后生效，不作为整次运行期限
        stream_timeout: stream()/WS 空闲超时兜底（秒），默认 None 表示
            不启用。以"自上次事件的时间"计时：每次等待事件都以
            stream_timeout 为上限、事件到达即重置。仅当长时间无任何事件
            （LLM 调用挂起）时取消在途调用并 emit error 事件，防止消费者
            永久阻塞。非流式 LLM 调用（含 tools）在途时视为"活动"，不触发。
            注意与 timeout 语义区分：timeout 是收到 done/error 后的宽限时长，
            stream_timeout 是"空闲超时"，不是整次运行总时长上限——持续 emit
            事件（token / tool / done）的健康长运行不会被误杀
        tool_timeout: 单个 tool 执行超时时间（秒），默认 30
        llm_timeout: "在途"非流式 LLM 调用被视为"活动"的最长时长（秒），
            默认 120。非流式调用（含 tools）期间按设计零事件产出，消费端
            空闲超时据此将健康慢速调用视为"活动"而非"挂起"。但若调用真正
            挂起（HTTP 无响应、永不返回），在途标记会一直保持——因此设此
            上限：超过该时长仍无任何事件返回，视为可能挂起，允许空闲超时
            触发取消，防止消费者永久阻塞（review round-15 issue 1）
        memory_context_limit: before_think 注入 system prompt 的 stream 历史条数上限，
            默认 20；可通过 weave.yaml 的 loop.memory_context_limit 配置，
            防止长历史/多 tool 运行后 system prompt 撑爆模型上下文窗口
        memory_knowledge_topk: before_think 知识检索注入条数上限（knowledge
            search 的 top_k），默认 5；可通过 weave.yaml 的
            loop.memory_knowledge_topk 配置。与 memory_context_limit /
            messages_window 同属"上下文预算"旋钮——宿主可据此调节知识段
            注入量，而无需改代码（review round-8 issue 3）
        messages_window: iterative LLM 消息窗口条数上限（超过则裁剪最旧的
            对话消息），默认 20，与模块级 _MAX_CONTEXT_MESSAGES 一致；可通过
            weave.yaml 的 loop.messages_window 配置。与 memory_context_limit
            同属"上下文预算"旋钮——后者约束 memory 注入条数，前者约束注入
            LLM 的 messages 窗口，两者解耦（review round-2 issue 4）。
            最小值为 3：小于 3 时裁剪逻辑退化（窗口-2 为 0/负，切片失效使
            messages 无界增长），iterative 运行时会回退默认 20
            （review round-3 issue 2）
        event_min_interval: 事件驱动 scheduled 的最小触发间隔（秒），默认 1.0。
            防止 data_change 突发时背靠背执行（放大成本 / 触发限流），
            可通过 weave.yaml 的 loop.event_min_interval 配置
    """
    type: str = "iterative"          # simple | iterative | scheduled
    max_iterations: int = 10
    stop_conditions: list[dict[str, Any]] = field(default_factory=list)
    schedule: str | None = None      # cron 表达式（scheduled loop）
    timeout: float = 5.0             # stream() 后台任务收尾宽限时长（秒）
    stream_timeout: float | None = None  # stream()/WS 空闲超时兜底（秒），None=不启用
    tool_timeout: float = 30.0       # 单个 tool 执行超时（秒）
    llm_timeout: float = 120.0       # 在途非流式 LLM 调用被视为"活动"的最长时长（秒）
    memory_context_limit: int = 20   # before_think 注入 stream 历史条数上限
    memory_knowledge_topk: int = 5   # before_think 知识检索注入条数上限（top_k）
    messages_window: int = 20        # iterative LLM 消息窗口条数上限（裁剪最旧对话）
    event_min_interval: float = 1.0  # 事件驱动 scheduled 最小触发间隔（秒）
    llm_retry_attempts: int = 3      # 非流式 LLM 调用重试次数（1=不重试），非功能（可靠性）
    llm_retry_backoff: float = 2.0   # 重试退避倍率（非功能）
    llm_call_timeout: float = 120.0  # 单次非流式 LLM 调用的硬超时（秒），非功能（时效性）


@dataclass(slots=True)
class MemoryScopeConfig:
    """单个 Memory Scope 的配置。

    backend: scope 级后端类型（sqlite | file | chroma），作为该 scope 下各
        access 类型未单独配置 backend 时的回退值；access 级 backend
        （session.stream.backend）优先。最终回退 MemoryConfig.default_backend。
        此前 backend/default_backend 配置被静默忽略、恒创建 SQLiteBackend
        （review round-6 issue 2）。
    ttl: scope 级统一过期时间（秒），作为各 access 类型未单独配置 ttl 时的
        回退值；None=永不过期。weave.yaml 中写在 scope 级（session.ttl）。
    ttl_stream / ttl_state / ttl_knowledge: 各 access 类型（stream / state /
        knowledge）各自的过期时间（秒），weave.yaml 中写在对应 access 配置块
        内（session.stream.ttl / session.state.ttl / session.knowledge.ttl）。
        允许同一 scope 下不同 access 类型配置不同过期时间，而非折叠为单一
        scope 级值；未单独配置时回退 scope 级 ttl（review round-6 issue 1）。
    """
    priority: int = 0                # 越小越"窄"，同 key 窄覆盖宽
    backend: str | None = None       # scope 级后端类型（sqlite | file | chroma）
    ttl: float | None = None         # scope 级记忆过期时间（秒），None=永不过期
    ttl_stream: float | None = None  # stream 类型专属过期时间（秒）
    ttl_state: float | None = None   # state 类型专属过期时间（秒）
    ttl_knowledge: float | None = None  # knowledge 类型专属过期时间（秒）
    stream: dict[str, Any] | None = None   # stream 后端配置
    state: dict[str, Any] | None = None    # state 后端配置
    knowledge: dict[str, Any] | None = None  # knowledge 后端配置


@dataclass(slots=True)
class MemoryConfig:
    """Memory 总配置。"""
    scopes: dict[str, MemoryScopeConfig] = field(default_factory=dict)
    default_backend: str = "sqlite"
    # 默认数据路径。相对路径在 load_config 中解析为相对配置文件目录
    # （而非 CWD），见 docs/issues/012 路径规范化。
    default_path: str = "./.weave/memory.db"


@dataclass(slots=True)
class PromptConfig:
    """Prompt 配置。"""
    system: str = ""                  # system prompt 文件路径
    loop_instruction: str = ""        # loop 指令文件路径
    memory_use: str = ""              # memory 使用说明文件路径


@dataclass(slots=True)
class FeatureConfig:
    """可选 Feature 开关。"""
    schema_validation: bool = False
    structured_call: bool = False
    two_stage_pipeline: bool = False
    prompt_defense: bool = False


@dataclass(slots=True)
class ServerConfig:
    """REST Server 配置。"""
    host: str = "127.0.0.1"
    port: int = 48080
    cors_origins: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentConfig:
    """Agent 标识配置。"""
    name: str = "default"


@dataclass(slots=True)
class LoggingConfig:
    """日志配置。"""
    level: str = "INFO"
    file: str | None = None


@dataclass(slots=True)
class CheckpointConfig:
    """状态回滚（checkpoint/rollback）配置。

    enabled: 是否启用自动打点。默认 False（零开销，向后兼容）。启用后 Loop
        在每次 tool 调用后（或按 trigger 指定时机）自动打快照。
    trigger: 自动打点时机。"after_each_tool"（默认）| "after_each_llm" | "manual"。
        "manual" 表示仅宿主显式调用 weave.checkpoint() 时才打点。
    keep: 每个 session 保留最近 N 个快照（超出自动淘汰最旧的）。
        快照本身 append-only（fork 语义），淘汰仅是回收旧快照，不破坏回滚能力。
    """
    enabled: bool = False
    trigger: str = "after_each_tool"
    keep: int = 10


@dataclass(slots=True)
class WeaveConfig:
    """Weave 顶层配置。"""
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    prompts: PromptConfig = field(default_factory=PromptConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)
