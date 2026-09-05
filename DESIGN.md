# Weave — Design Document

## 1. 项目定位

**Weave** 是一个非侵入式、基于 SDK 的 Agent 插件框架。它为任意 Python 项目提供**最基础的 Loop（循环）和 Memory（记忆）机制**，并以 SDK 的形式对外提供服务。

### 1.1 核心价值

> 把一个纯"请求-响应"的项目，变成一个"能迭代、能记住"的 Agent，只需 5 行代码。

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| **非侵入式** | `pip install` + 配置 + 导入即可，不修改宿主项目架构 |
| **SDK 即接口** | Python SDK 和 REST API 是同一抽象层级，SDK 是主要入口 |
| **零硬编码** | 无硬编码 API Key、无硬编码 Prompt、无硬编码模型名 |
| **可插拔** | Loop 策略、Memory 后端、LLM 适配器均通过接口抽象 |
| **最基础** | 只做 Loop + Memory，不做复杂 Agent 框架 |

---

## 2. 痛点分析（来自 myKG 和 bePM）

### 2.1 myKG（知识图谱学习助手）

myKG 帮助用户通过自然语言对话构建知识图谱，学习任意领域。做了：
- LLM 驱动的图操作（解析、投影、验证、标签）
- 拓扑排序 + 循环检测
- 知识库持久化（单 JSON 文件）
- Cytoscape.js 可视化

**缺失的 Loop 和 Memory：**

| 现象 | 根因 |
|------|------|
| 用户通过 quiz 点亮节点后，系统不会主动建议下一步学什么 | 无自主 Loop |
| Quiz session 存于内存 dict，重启即丢失 | 无持久化 Working Memory |
| 每次 API 调用重新读取整个 kb.json，无缓存 | 无 Memory 层抽象 |
| 无法跨 session 追踪学习进度趋势 | 无 Long-term Memory |
| Prompt 以 Python f-string 写在引擎文件里 | 硬编码 Prompt |
| 无 pip 包，无法被其他项目导入 | 无 SDK |

### 2.2 bePM（AI 项目管理）

bePM 通过自然语言解析项目任务、计算关键路径、跟踪进度。做了：
- LLM 驱动的 NL → DAG 解析
- CPM 关键路径 + 风险分析（8 维度）
- WebSocket 实时推送
- 分层配置（env → claude settings → config.json → 默认值）
- Prompt 外置为 `.md` 文件

**缺失的 Loop 和 Memory：**

| 现象 | 根因 |
|------|------|
| 风险分析只在用户提交进度后执行一次，不会定期重扫 | 无 Scheduled Loop |
| 历史项目数据不作为新项目估算参考 | 无跨项目 Long-term Memory |
| LLM 不可用时直接返回空，无降级重试 | 无 Loop 中的错误恢复 |
| 消息历史仅用最近 20 条，超出裁剪丢失 | 无结构化 Memory |
| 无 pip 包，其他项目只能调 REST API | 无 SDK |

### 2.3 共性痛点总结

```
┌──────────────────────────────────────────────────────┐
│               myKG 和 bePM 的核心缺口                  │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐                  │
│  │  无 Loop 机制  │  │ 无 Memory 机制 │                 │
│  │              │  │              │                  │
│  │ · 纯响应式     │  │ · JSON 文件存储 │                │
│  │ · 无自主迭代   │  │ · 易失内存状态  │                │
│  │ · 无定时任务   │  │ · 无语义搜索   │                 │
│  │ · 无错误重试   │  │ · 无跨会话记忆  │                │
│  └──────┬───────┘  └──────┬───────┘                  │
│         │                 │                          │
│         └────────┬────────┘                          │
│                  │                                   │
│     ┌────────────┴────────────┐                      │
│     │     无 SDK / 包         │                      │
│     │                         │                      │
│     │ · 无 pip install         │                      │
│     │ · 只能调 REST API        │                      │
│     │ · 无类型化客户端          │                      │
│     │ · Prompt 硬编码（myKG）   │                      │
│     └─────────────────────────┘                      │
└──────────────────────────────────────────────────────┘
```

---

## 3. 架构总览

### 3.1 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│                     Integration Layer（集成层）                 │
│  ┌──────────────────┐  ┌──────────────────┐                   │
│  │   Python SDK      │  │   REST Server     │                  │
│  │   weave.run(...)  │  │   weave serve     │                  │
│  │   (主要入口)       │  │   (可选)           │                  │
│  └────────┬─────────┘  └────────┬─────────┘                   │
│           │                     │                             │
│           └──────────┬──────────┘                             │
│                      │                                        │
├──────────────────────┼────────────────────────────────────────┤
│                      │          Core Layer（核心层）            │
│  ┌───────────────────┴───────────────────────────────────┐    │
│  │                     Weave (Agent)                      │    │
│  │                                                       │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌───────────────┐  │    │
│  │  │ Loop Engine  │  │Memory Manager│  │ Tool Registry │  │    │
│  │  │             │  │             │  │               │  │    │
│  │  │ · simple    │  │ · stream     │  │ @weave.tool   │  │    │
│  │  │ · iterative │  │ · state      │  │               │  │    │
│  │  │ · scheduled │  │ · knowledge  │  │               │  │    │
│  │  └──────┬──────┘  └──────┬──────┘  └───────────────┘  │    │
│  └─────────┼────────────────┼─────────────────────────────┘    │
│            │                │                                  │
├────────────┼────────────────┼──────────────────────────────────┤
│            │                │       Adapter Layer（适配层）       │
│  ┌─────────┴────────────────┴──────────────────────────────┐   │
│  │  ┌──────────┐  ┌──────────────┐  ┌──────────────────┐   │   │
│  │  │   LLM    │  │    Store     │  │  Prompt Loader   │   │   │
│  │  │ Adapters │  │   Adapters   │  │  (file/template) │   │   │
│  │  │          │  │              │  │                  │   │   │
│  │  │·anthropic│  │ · sqlite     │  │ · .md loader     │   │   │
│  │  │· openai  │  │ · chromadb   │  │ · variable sub   │   │   │
│  │  │· factory │  │ · file       │  │ · registry       │   │   │
│  │  └──────────┘  └──────────────┘  └──────────────────┘   │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 核心原则

**1. SDK 即接口**

Python SDK 和 REST API 暴露**同一抽象层级**的概念。你在代码里写的 `weave.run("...")` 和在 HTTP 里调 `POST /agents/default/run` 是等价操作。

```python
# SDK 方式 - 在宿主项目中
from weave_agent_sdk import Weave
weave = Weave("weave.yaml")
result = weave.run("我应该学什么?")
```

```bash
# REST 方式 - 远程调用
curl -X POST http://localhost:48080/agents/default/run \
  -H "Content-Type: application/json" \
  -d '{"input": "我应该学什么?"}'
```

**2. 零硬编码**

```yaml
# weave.yaml — 所有配置的唯一来源
llm:
  provider: anthropic           # 可选: openai
  model: ${WEAVE_MODEL}        # 从环境变量注入
  # 绝不在代码中写 api_key

loop:
  type: iterative
  max_iterations: ${WEAVE_MAX_ITER:-10}

memory:
  scopes:
    session:                     # priority: 0（最窄，值优先采用）
      stream:
        backend: sqlite
        path: ${WEAVE_DATA_DIR:-./data}/memory.db
      state:
        backend: sqlite

prompts:
  system: prompts/system.md     # Prompt 从文件加载
  loop_instruction: prompts/loop.md
```

**3. 非侵入式**

宿主项目不需要为了接入 Weave 而改变自身架构：

```
宿主项目（myKG / bePM / 你的项目）
│
├── 原有的 API / Engine / Models    ← 完全不动
├── 原有的 frontend / data          ← 完全不动
│
├── weave.yaml              ← 只需加一个配置文件
├── prompts/                ← 加 Prompt 文件
│   ├── system.md
│   └── loop.md
│
└── agent.py                ← 10 行胶水代码，定义 tools
```

---

## 4. 核心模块设计

### 4.1 Loop Engine（循环引擎）

Loop 回答一个问题：**Agent 跑一次就停，还是持续跑？**

#### 接口抽象

```python
# weave/loop/base.py
from abc import ABC, abstractmethod

class BaseLoop(ABC):
    """循环策略的抽象接口"""

    @abstractmethod
    async def run(
        self,
        agent: "Weave",
        user_input: str,
    ) -> LoopResult:
        """执行循环，返回最终结果"""
        ...
```

#### 三种内置策略

**SimpleLoop**（对标 myKG/bePM 当前行为 — 一次请求一次响应）

```
用户输入 → LLM 思考 → 响应 → 结束

状态机: IDLE → THINKING → DONE
```

```yaml
loop:
  type: simple
```

**IterativeLoop**（Agent 持续迭代，直到满足停止条件）

```
用户输入 → LLM 思考 → 调用 Tool → LLM 思考 → 调用 Tool → ... → 完成

状态机:
  IDLE → THINKING → TOOL_CALLING → THINKING → ... → DONE
    ↑                                                    │
    └────────────────────────────────────────────────────┘
```

两套停止机制：
- **`max_iterations`**：硬上限（10 = 最多 10 次 LLM 调用），防止无限循环。无论 LLM 是否想继续，到达后强制终止
- **`stop_conditions`**：语义停止条件（可组合，任一满足即停），由 LLM 主动触发

```yaml
loop:
  type: iterative
  max_iterations: 10              # 硬上限，防止无限循环（必配）
  stop_conditions:                # 语义停止条件（可选组合）
    - type: tool_call
      name: finish                # Agent 主动调用 finish() 工具
    - type: no_tool_calls         # LLM 不再调工具时停止
    - type: text_pattern
      pattern: "TASK_COMPLETE"   # 输出中包含特定标记
```

**ScheduledLoop**（定时触发 — 后台持续运行）

```
定时器触发 → Agent 运行 → 更新 Memory → 等待下一次触发

状态机:
  IDLE → (wait) → THINKING → DONE → IDLE → (wait) → ...
```

```yaml
loop:
  type: scheduled
  schedule: "0 */6 * * *"         # 每 6 小时运行一次
  # 或事件驱动:
  schedule: "@on_data_change"     # 由宿主项目手动触发（Phase 3 设计）
                                  # 宿主项目调用 weave.trigger("data_change") → event_bus
                                  # → ScheduledLoop 收到事件后执行一次迭代
```

#### Loop 生命周期钩子

```python
class BaseLoop(ABC):
    # 钩子：Loop 开始前
    async def on_start(self, agent: "Weave", input: str) -> None: ...

    # 钩子：每次 LLM 调用前（从 Memory 加载 context）
    # current_input: 当前轮用户输入（用于 KnowledgeMemory 语义搜索）
    # 返回的 dict 直接注入 LLM system prompt 的 memory 段，包含:
    #   {"stream": [...], "state": {...}, "knowledge": [...]}
    async def before_think(self, agent: "Weave", current_input: str) -> dict: ...

    # 钩子：每次 LLM 调用后（将 assistant response 持久化到 Memory）
    async def after_think(self, agent: "Weave", response: LLMResponse) -> None: ...

    # 钩子：Loop 结束后
    async def on_end(self, agent: "Weave", result: LoopResult) -> None: ...
```

---

### 4.2 Memory Manager（记忆管理器）

Memory 回答两个问题：**Agent 记住什么，怎么取出来？**

#### 设计原则

- **作用域由项目自定义**：Weave 不内置 system/project/session 层级。接入项目在配置中定义自己的作用域。
- **访问模式决定接口**：stream（时序流）、state（键值）、knowledge（搜索）——不是按时间分，是按取法分。
- **时间维度 = TTL**：每条记忆可配过期时间，不是独立的记忆类型。
- **Namespace 隔离**：所有数据存同一张表，通过 namespace 列行级隔离。

#### 三种访问模式

```
┌─────────────────────────────────────────────────────────┐
│                    Memory 访问模式                       │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │          stream（时序消息流）                      │    │
│  │                                                 │    │
│  │  追加写入，按时间顺序读取最近 N 条                 │    │
│  │  user: "我想学推荐系统"                           │    │
│  │  assistant: "好的，从协同过滤开始..."              │    │
│  │  user: "我不懂SVD"                               │    │
│  │                                                 │    │
│  │  接口: append() / last(n)                       │    │
│  │  SQL:  ORDER BY created_at DESC LIMIT n         │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │          state（键值状态）                         │    │
│  │                                                 │    │
│  │  按 key 精确读写，新值覆盖旧值                     │    │
│  │  "current_topic"    → "矩阵分解"                  │    │
│  │  "quiz_in_progress" → true                       │    │
│  │  "questions_asked"  → 3                          │    │
│  │                                                 │    │
│  │  接口: set(key, val) / get(key)                  │    │
│  │  SQL:  WHERE key = ? (UPSERT)                   │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │          knowledge（知识搜索）                     │    │
│  │                                                 │    │
│  │  追加写入，不覆盖；语义/全文搜索                   │    │
│  │  "协同过滤的核心是用户-物品矩阵..."                 │    │
│  │  "冷启动问题可以用基于内容的推荐解决..."            │    │
│  │  search("冷启动 怎么办") → top_k 结果             │    │
│  │                                                 │    │
│  │  接口: add(content) / search(query, top_k)       │    │
│  │  SQL:  WHERE content MATCH ? (FTS5)             │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

#### 作用域（Scope）——由项目自定义

Weave 不内置 system/project/session 层级。接入项目在 `weave.yaml` 中定义自己的作用域：

```yaml
# myKG 的作用域定义。priority 越小越窄，同 key 时窄 scope 覆盖宽 scope
memory:
  scopes:
    kb_global:                    # 全局知识库（所有领域共享）priority: 20
      state:
        backend: sqlite
      knowledge:
        backend: chromadb
    domain:                       # 学习领域（如"推荐系统"）priority: 10
      state:
        backend: sqlite
      knowledge:
        backend: sqlite
        fts: true                 # SQLite FTS5 全文搜索
    quiz_session:                 # 一次 quiz 会话 priority: 0（最窄）
      stream:
        backend: sqlite
        ttl: 3600                 # 1 小时后自动清理
      state:
        ttl: 3600
```

```yaml
# bePM 的作用域定义。priority 越小越窄，同 key 时窄 scope 覆盖宽 scope
memory:
  scopes:
    workspace:                    # 跨项目工作空间 priority: 20
      knowledge:
        backend: chromadb
    project:                      # 单个项目 priority: 10
      state:
        backend: sqlite
      knowledge:
        backend: sqlite
        fts: true
    edit_session:                 # 一次编辑操作 priority: 0（最窄）
      stream:
        backend: sqlite
        ttl: 300                  # 5 分钟
```

#### Namespace 隔离实现

所有数据存一张表，通过 namespace 列行级隔离：

```sql
CREATE TABLE memory_entries (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,           -- 隔离键
    access_type TEXT NOT NULL,         -- stream / state / knowledge（有意冗余：namespace 已含此信息，但独立列方便建索引和按类型过滤）
    key TEXT,                          -- state 模式的 key
    content TEXT NOT NULL,
    metadata JSON,
    created_at REAL NOT NULL,
    expires_at REAL                    -- NULL = 永不过期
);

CREATE INDEX idx_ns_at ON memory_entries(namespace, access_type);
CREATE INDEX idx_ns_key ON memory_entries(namespace, access_type, key);
```

Namespace 格式：`{scope_name}:{scope_id}:{access_type}`

```
例：
  quiz_session:abc123:stream     → session abc123 的对话流
  domain:recsys:state            → "推荐系统" 领域的状态
  kb_global:default:knowledge    → 全局知识库
```

**TTL 与清理策略**：过期数据在**读取时过滤**（`WHERE expires_at IS NULL OR expires_at > now()`），确保不返回过期数据。同时 `MemoryManager` 在每次写入时被动清理（`DELETE WHERE expires_at <= now()` LIMIT 100，分批避免写放大）。Stream 的 `max_items` 在 append 时生效——超出则 DELETE 最旧的条目，max_items 优先于 TTL。

#### 一次 run() 的 Memory 加载流程

```python
# Weave 内部
# active_scopes 来源：config 中 memory.scopes 下所有定义的作用域自动激活。
# 按 priority 升序排列（值小的在前 = 窄 scope 优先覆盖宽 scope）
# scope_id 从 scope_hints 获取，命名约定: {scope_name}_id
# 例如 scope 名 "quiz_session" → scope_hints key 为 "quiz_session_id"
# 未提供时自动生成 uuid4()（注意 Python 热切求值，即使 .get() 命中 uuid4() 也会执行，实现时需惰性处理）
active_scopes = sorted(
    [(name, scope.priority, scope_hints.get(f"{name}_id", uuid4()))
     for name, scope in config.memory.scopes.items()],
    key=lambda x: x[1]  # 按 priority 升序
)
# → [("session", 0, "abc123"), ("project", 10, "my_proj")]

# 1. 根据激活的 scope 构造三种 access_type 的 namespace 列表
stream_ns    = [f"{scope}:{sid}:stream"    for scope, _, sid in active_scopes]
state_ns     = [f"{scope}:{sid}:state"     for scope, _, sid in active_scopes]
knowledge_ns = [f"{scope}:{sid}:knowledge" for scope, _, sid in active_scopes]
# → stream_ns:    ["session:abc123:stream", "project:my_proj:stream"]
# → state_ns:     ["session:abc123:state",  "project:my_proj:state"]
# → knowledge_ns: ["session:abc123:knowledge", ...]

# 2. 并行加载，按 scope 窄→宽 合并（session 覆盖 project 覆盖 global）
stream_msgs = memory.stream.last(20, namespaces=stream_ns)
state_kvs   = memory.state.get_all(namespaces=state_ns)
knowledge   = memory.knowledge.search(query, namespaces=knowledge_ns, top_k=5)

# 3. 拼入 LLM context
context = f"""
## 对话历史 (stream)
{format_messages(stream_msgs)}

## 当前状态 (state)
{format_kv(state_kvs)}

## 相关知识 (knowledge)
{format_search_results(knowledge)}
"""
```

#### 接口定义

```python
# weave/memory/base.py

class StreamMemory(ABC):
    """时序消息流"""
    async def append(self, entry: dict, namespace: str) -> str: ...
    async def last(self, n: int = 20, namespaces: list[str] = None) -> list[dict]: ...
    # namespaces=None 表示查询所有激活的 namespace（按 created_at 合并排序）
    async def trim(self, max_items: int, namespace: str) -> None: ...

class StateMemory(ABC):
    """键值状态"""
    async def get(self, key: str, namespace: str) -> Any | None: ...
    async def set(self, key: str, value: Any, namespace: str) -> str: ...  # 返回 entry ID
    async def delete(self, key: str, namespace: str) -> None: ...
    async def get_all(self, namespaces: list[str] = None) -> dict[str, Any]: ...
    # 多个 namespace 时按键合并，较窄 scope 的值覆盖较宽 scope

class KnowledgeMemory(ABC):
    """知识搜索"""
    async def add(self, content: str, namespace: str, metadata: dict = None) -> str: ...
    async def search(self, query: str, namespaces: list[str] = None, top_k: int = 5) -> list[SearchResult]: ...
    # namespaces=None 表示搜索所有激活的 knowledge 类 namespace
```

`SearchResult` 是一个 namedtuple：
```python
SearchResult = namedtuple("SearchResult", ["id", "content", "score", "metadata"])
# id: str       — 唯一标识
# content: str  — 匹配内容
# score: float  — 相关度分数 (0.0 ~ 1.0)
# metadata: dict — 附加信息
```

详见 `docs/issues/003-memory-model.md`。

#### Scope ID 解析

配置中的 `{session_id}`、`{project_id}` 等占位符在运行时解析。来源有两处：

```python
# 方式 1: weave.run() 显式传入
result = weave.run("...", scope_hints={"session_id": "abc123", "project_id": "my_project"})

# 方式 2: 自动生成（未传入时）
session_id = str(uuid4())   # session 级自动生成
project_id = "default"      # project 级默认值
```

解析流程：

```
weave.run(input, scope_hints={...})
  │
  ├── 对每个 scope 配置:
  │     path: ./data/sessions/{session_id}.db
  │                        │
  │                        ▼
  │     1. 检查 scope_hints 中是否有 key "session_id" → 用
  │     2. 没有 → auto_session_id = uuid4()
  │     3. 替换 → ./data/sessions/abc123.db
  │
  └── 实际打开的后端路径由解析后的 namespace 决定
```

---

### 4.3 LLM Adapter（LLM 适配器）

#### 抽象接口

```python
# weave/llm/base.py
from dataclasses import dataclass

@dataclass
class Message:
    """对话消息，OpenAI 兼容格式"""
    role: str                            # "system" | "user" | "assistant" | "tool"
    content: str                         # 消息文本
    tool_call_id: str | None = None      # tool 消息专用：对应的 tool_call id
    tool_calls: list["ToolCall"] | None = None  # assistant 消息专用：LLM 请求调用的 tools

@dataclass
class ToolCall:
    """LLM 请求的一次 tool 调用"""
    id: str                              # tool_call 唯一 ID
    name: str                            # tool 名称
    arguments: dict                      # 解析后的参数

@dataclass
class LLMResponse:
    content: str                         # LLM 文本输出
    tool_calls: list[ToolCall] | None    # tool 调用请求（如 LLM 决定调 tool）
    model: str                           # 实际使用的模型名
    usage: dict                          # token 用量 {"input": N, "output": M}
    finish_reason: str                   # "stop" | "tool_calls" | "length"

class BaseLLM(ABC):
    """LLM 适配器抽象接口"""
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] = None,        # tool JSON Schema 列表
        max_tokens: int = 4096,
    ) -> LLMResponse: ...

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict] = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]: ...         # 逐 token 流式输出文本
    # 注：流式模式下 tool_calls 不在 chat_stream 中逐 delta 产出。
    # 各家 LLM API 的流式 tool_call 格式差异大（Anthropic content_block_start、
    # OpenAI delta.tool_calls），统一方案为：流结束后由 Loop Engine 从累积文本
    # 中通过 A1 extract_json 解析 tool_calls，再由事件总线 emit "tool_call" 事件。
    # 如果 LLM 支持原生 streaming tool_use，adapter 实现可在流结束后附加 tool_calls。
```

> **⚠️ Adapter 协议差异**：不同 SDK 对多 tool result 的消息格式要求不同（OpenAI 独立消息 vs. Anthropic 合并到同一条 user 消息）。格式转换是 Adapter 层的专属职责，新增 Adapter 时必须确认此行为。详见 `docs/issues/004-llm-adapter-protocol.md`。

#### 自动检测与配置

复用 myKG 和 bePM 的成熟模式。工厂函数 `create_llm()` 按以下优先级确定后端：

1. `weave.yaml` 中显式指定的 `provider`
2. 环境变量 `LLM_PROVIDER`
3. 智能推断：`ANTHROPIC_API_KEY` 存在 → anthropic；`OPENAI_API_KEY` 存在 → openai
4. 从 `~/.claude/settings.json` 读取（兼容 Claude Code 生态）

```yaml
llm:
  provider: anthropic            # anthropic | openai
  model: claude-sonnet-5-20251001
  max_tokens: 4096
  temperature: 0.7
  # api_key 绝不在此出现，从环境变量读取
```

---

### 4.4 Prompt Loader（Prompt 管理器）

零硬编码 Prompts。支持两种模式：

**模式 A：单文件（简单场景，向下兼容）**

```yaml
prompts:
  system: prompts/system.md       # 一个文件 = 一个完整 Prompt
  loop_instruction: prompts/loop.md
```

**模式 B：Schema 组装（复杂场景，Prompt 由多部件按规则拼成）**

```yaml
prompts:
  system: prompts/coach.schema.yaml  # Schema 描述 Prompt 的结构和组装规则
```

**Schema 格式（.schema.yaml）**：

```yaml
# prompts/coach.schema.yaml
prompt:
  description: "学习教练 System Prompt"     # 可选的描述
  separator: "\n\n---\n\n"                  # 部件间分隔符，默认 "\n\n"

  composition:
    # 固定文件 — 永远加载
    - file: coach/role.md
    # 条件切换 — 根据变量值选择文件
    - switch:
        variable: strategy
        cases:
          topo: coach/strategy_topo.md
          adaptive: coach/strategy_adaptive.md
        default: coach/strategy_topo.md
    # 行内模板 — 运行时渲染变量
    - template: "## 当前上下文\n领域: {{ domain }}\n策略: {{ strategy }}"
```

**Schema 的 Part 类型**：

| 类型 | 写法 | 说明 |
|------|------|------|
| `file` | `file: path/to/file.md` | 加载文件内容 |
| `template` | `template: "文本 {{ var }}"` | 行内文本，运行时变量替换 |
| `switch` | `switch: {variable, cases, default}` | 根据变量值选择不同文件 |

**Schema 设计哲学**：

> Prompt 不是文本文件，是带签名的函数。Schema = Prompt 的结构说明书。变量不是"要校验的参数"，而是驱动组装的输入——变量取不同值，拼出不同的 Prompt。

**兼容性**：`prompts.system` 指向 `.md` → 走模式 A（单文件）。指向 `.schema.yaml` → 走模式 B（Schema）。PromptRegistry 自动检测。

**模板变量解析优先级**（从高到低）：

```
1. weave.run(context={"domain": "推荐系统"})    # 调用时显式传入 → {{ domain }}
   scope_hints 的值也合并到 context 命名空间       # scope_hints={"session_id":"abc"} → {{ session_id }}
2. 环境变量                                      # {{ env.WEAVE_MODEL }}
3. weave.yaml 顶层配置                            # {{ config.llm.model }}
4. 默认值                                        # {{ key | default("未设置") }}
```

如果变量在所有来源都不存在且未指定默认值 → 渲染时报错，不静默吞变量。

---

### 4.5 Tool Registry（工具注册）

Tools 由宿主项目定义，Weave 不内置任何 Tool：

```python
from weave_agent_sdk import Weave

weave = Weave("weave.yaml")

# 方式 1: 装饰器
@weave.tool
def search_knowledge_base(query: str) -> list[dict]:
    """搜索知识库中的节点"""
    return kb.search(query)

# 方式 2: 函数引用
def create_learning_plan(topic: str) -> dict:
    """为指定主题创建学习计划"""
    ...

weave.register_tool(create_learning_plan)
```

Tool 定义自动生成 JSON Schema（从函数签名 + docstring），传给 LLM。

**Tool 的 async/sync 约定**：Tool 可以是 sync 或 async 函数，Weave 统一处理：
- sync tool → 内部通过 `asyncio.to_thread()` 在线程池中执行，不阻塞事件循环
- async tool → 直接 await
- 宿主项目不需要为了 Weave 把已有 sync 函数改成 async

#### Tool 执行错误处理

Tool 抛异常时，Loop 不终止。错误被标准化后作为 `tool_result` 返回给 LLM，让 LLM 决定下一步：

```python
# Weave 内部 Tool 执行包装
try:
    result = await tool.execute(args)
except Exception as e:
    result = {
        "error": True,
        "type": type(e).__name__,
        "message": str(e)
    }
# 将 result 作为 tool_result 消息追加到 conversation，LLM 看到后可以:
# - 用不同参数重试
# - 换一个 tool
# - 告知用户操作失败
```

---

### 4.6 内置原子能力（Built-in Utilities）

以下能力来自 myKG 和 bePM 代码库的深层分析。它们在两个项目中以 ad-hoc 方式重复出现，本质上是**业务无关的原子操作**，应作为 Weave 的基础设施提供。

#### 能力分层

```
┌──────────────────────────────────────────────┐
│  Feature（可选启用）                           │
│  ┌──────────┐ ┌────────────┐ ┌─────────────┐ │
│  │ Schema   │ │ 结构化 LLM  │ │ 两阶段      │ │
│  │ 校验     │ │ 调用        │ │ LLM 流水线   │ │
│  └──────────┘ └────────────┘ └─────────────┘ │
│  ┌──────────┐                                │
│  │ Prompt   │                                │
│  │ 注入防御  │                                │
│  └──────────┘                                │
├──────────────────────────────────────────────┤
│  核心基础设施（始终可用）                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ JSON     │ │ async    │ │ async        │ │
│  │ 提取     │ │ 重试     │ │ 超时         │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ LLM 错误 │ │ 事件总线  │ │              │ │
│  │ 类型     │ │          │ │              │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
├──────────────────────────────────────────────┤
│  内部工具（weave 自用，不暴露给外部）            │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ 计时器   │ │ 错误兜底  │ │ 组件注册表    │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
└──────────────────────────────────────────────┘
```

#### 核心基础设施

**A1. LLM JSON 提取 (`extract_json`)**

从 LLM 返回的文本中提取 JSON。处理 markdown 代码围栏、自然语言混排、格式错误。两个参考项目各自复制粘贴了 4 次。

```python
from weave_agent_sdk.llm import extract_json

# LLM 返回: "好的，结果如下:\n```json\n{"city":"北京"}\n```\n希望对你有帮助"
data = extract_json(response)  # → {"city": "北京"}
```

**A2. async 重试 (`async_retry`)**

指数退避 + 随机 jitter，可配置哪些异常可重试。两个项目都没有此能力，LLM 调用失败要么 crash 要么静默返回错误数据。

```python
from weave_agent_sdk.utils import retry

@retry(max_attempts=3, backoff=2.0, jitter=0.1, retryable={RateLimitError, ServerError})
async def call_llm(prompt): ...
```

**重试耗尽后的行为**：所有重试都失败时，调用已配置的 fallback（C2 错误兜底）。如果未配置 fallback，抛出 `RetryExhaustedError`（包含原始异常链）供调用方处理。在 Loop 中，Loop Engine 捕获此异常并终止当前迭代，记录到 StateMemory，不会静默丢失。

**A3. async 超时 (`async_timeout`)**

为外部调用设置截止时间。两个项目的 LLM 调用都没有 timeout，网络挂掉会无限等待。

```python
from weave_agent_sdk.utils import timeout

async with timeout(30):  # 30 秒
    result = await llm.chat(prompt)
```

**A4. LLM 错误类型 (`llm_errors`)**

类型化异常层次，支持精确的重试/不重试判断。两个项目都只抛字符串异常。

```python
# weave/llm/errors.py
class WeaveLLMError(Exception): ...
class RateLimitError(WeaveLLMError): ...      # 429 — 可重试
class AuthError(WeaveLLMError): ...           # 401 — 不可重试
class ContextLengthError(WeaveLLMError): ...  # prompt 太长
class ServerError(WeaveLLMError): ...         # 5xx — 可重试
class NetworkError(WeaveLLMError): ...        # 连接失败 — 可重试
```

**A5. 事件总线 (`event_bus`)**

进程内 async pub/sub。Weave 在 Loop 执行过程中 emit 事件（token、tool_call、error），订阅方通过 `weave.on(event)` 注册消费。两个项目各手写了一套——myKG 的 StreamLogger + bePM 的 ConnectionManager。

```python
# 订阅方
async with weave.on("tool_called") as stream:
    async for event in stream:
        print(f"Tool: {event['name']}")

# Weave 内部 emit
await weave.emit("tool_called", {"name": "search_kb", "args": {...}})
```

实现：`dict[event_name, list[asyncio.Queue]]`，生产者 `queue.put()`（阻塞式防背压丢消息），消费者 `queue.get()`（挂起等待），退订自动清理。

详见 `docs/issues/002-event-bus.md`。

#### 可选 Feature

**B1. Schema 校验 (`validate_schema`)**

用 Pydantic model 校验 LLM 提取出的 dict，返回结构化错误。两个项目都用手写 `.get()` 兜底，静默吞错。

```python
from weave_agent_sdk.features import extract_and_validate

class WeatherQuery(BaseModel):
    city: str
    date: str

result = extract_and_validate(llm_response, WeatherQuery)
# → WeatherQuery(city="北京", date="2026-07-29")
# → 或 ValidationError(missing=["city"], type_errors={"date": "expected str, got int"})
```

配置启用：
```yaml
features:
  schema_validation: true
```

**B2. 结构化 LLM 调用 (`structured_call`)**

A1 + B1 + A2 的组合：调 LLM → 提取 JSON → Schema 校验 → 失败时把错误喂回 LLM 重试。bePM 将这个循环复制粘贴了 4 次。

```python
from weave_agent_sdk.features import structured_call

result = await structured_call(
    prompt="分析用户输入: {...}",
    schema=WeatherQuery,
    max_retries=3,
)
```

配置启用：
```yaml
features:
  structured_call: true
```

**B3. 两阶段 LLM 流水线 (`two_stage_call`)**

bePM 的最精彩设计：拆成"意图理解"（无 Schema 约束 → 自由表达）→ "翻译"（Schema 约束 → 精准 JSON），大幅提升结构化输出准确率。

```python
from weave_agent_sdk.features import two_stage_call

result = await two_stage_call(
    understand_prompt="理解用户想对项目做什么操作",
    translate_schema=DagOperation,  # 第二阶段的目标 Schema
)
```

配置启用：
```yaml
features:
  two_stage_pipeline: true
```

**B4. Prompt 注入防御 (`sanitize_input`)**

截断 + 正则检测注入模式 + 防御策略。来自 bePM 的 `sanitize_user_input`。

```python
from weave_agent_sdk.features import sanitize

safe_text = sanitize(user_input, max_length=2000, strategy="defend")
```

配置启用：
```yaml
features:
  prompt_defense: true
```

#### 内部工具

**C1. 计时器 (`timed_async`)** — context manager，记录 async 操作耗时。来自 myKG StreamLogger 的内联 `perf_counter`。

**C2. 错误兜底 (`fallback`)** — async 调用失败时返回指定的默认值。来自 myKG projector/tagger 的内联 try/except。

**C3. 组件注册表 (`lazy_registry`)** — key→instance 映射，惰性初始化 + TTL 淘汰。取代 myKG 和 bePM 散落各处的模块级全局 dict。

#### 来源映射

| 能力 | 来源 | 在源项目中的形态 |
|------|------|-----------------|
| extract_json | myKG ×4, bePM ×4 | 同一函数在不同文件复制粘贴，错误处理各不同 |
| async_retry | 两个项目均缺失 | LLM 失败直接 crash 或静默返回错误数据 |
| async_timeout | 两个项目均缺失 | 无超时设置，网络挂死无限等待 |
| llm_errors | 两个项目均缺失 | 只抛字符串 `AdapterError("...")` |
| event_bus | myKG, bePM 各手写 | `stream_logger.py` Queue / `ws.py` ConnectionManager |
| schema_validation | myKG, bePM 各手写 | myKG `.get()` 静默 / bePM 手写 dict schema DSL |
| structured_call | bePM ×4 | 同一循环在 4 个函数里各写一遍 |
| two_stage_call | bePM ×1 | `parse_single_task` 200 行，含业务逻辑 |
| sanitize_input | bePM ×1 | `sanitize_user_input` 40 行 |
| timed_async | myKG ×1 | StreamLogger 内联 `time.perf_counter()` |
| fallback | myKG ×2 | projector/tagger 内联 try/except + 硬编码默认值 |
| lazy_registry | myKG ×2, bePM ×1 | 模块级全局 dict，无清理/无 TTL |

#### 不入 Weave 的能力（明确排除）

| 能力 | 来源 | 排除理由 |
|------|------|---------|
| 熔断器 (Circuit Breaker) | bePM `llm_provider.py:37-73` | 偏运维层，sync 实现跑在 async 环境有问题 |
| 跨平台文件锁 | bePM `projects.py:16-59` | SQLite 自带锁，JSON 后端不是主力 |

---

### 4.7 并发安全

Weave 实例是**协程安全**的（非线程安全）。设计要点：

- **单实例多协程**：多个 `weave.run()` 可在同一事件循环中并发调用，各自拥有独立的 run context
- **SQLite 连接**：默认使用单个连接 + `check_same_thread=False`（Python 的 aiosqlite 处理协程级并发）。对于高并发场景，可配置连接池
- **Memory 写入**：同一 namespace 的并发写入由 SQLite 的 WAL 模式串行化（写者不阻塞读者）
- **状态隔离**：每次 `weave.run()` 的中间状态（loop iteration、stream context）在各自协程内，不共享。结束后通过 namespace 持久化到 Memory

```python
# 安全：两个 run 并发执行，各自独立
async with asyncio.TaskGroup() as tg:
    tg.create_task(weave.arun("查学习进度", scope_hints={"session_id": "a"}))
    tg.create_task(weave.arun("查项目风险", scope_hints={"session_id": "b"}))
```

> **限制**：Weave 实例不支持从不同线程并发调用。多线程场景下每个线程应创建独立的 Weave 实例，或使用 `asyncio.run()` 桥接。

---

## 5. 目录结构

```
weave/                              # Git 仓库根目录
├── pyproject.toml                  # pip install weave
├── README.md
├── DESIGN.md                       # 本文档
│
├── weave/                          # Python 包
│   ├── __init__.py                 # 公共 API 导出
│   │   from weave_agent_sdk import Weave
│   │   weave = Weave("config.yaml")
│   │
│   ├── agent.py                    # Weave 类 — 核心编排器
│   ├── config.py                   # WeaveConfig — YAML/ENV/Dict 加载
│   ├── types.py                    # 共享类型: Message, ToolCall, LoopResult
│   │
│   ├── loop/                       # 循环策略
│   │   ├── __init__.py
│   │   ├── base.py                 # BaseLoop 抽象类
│   │   ├── simple.py              # SimpleLoop 实现
│   │   ├── iterative.py          # IterativeLoop 实现
│   │   └── scheduled.py          # ScheduledLoop 实现
│   │
│   ├── memory/                     # 记忆系统
│   │   ├── __init__.py
│   │   ├── base.py                 # StreamMemory / StateMemory / KnowledgeMemory 接口
│   │   ├── manager.py             # MemoryManager — 管理作用域 + namespace
│   │   ├── stream.py              # StreamMemory — 时序消息流
│   │   ├── state.py               # StateMemory — 键值状态
│   │   ├── knowledge.py           # KnowledgeMemory — 知识搜索
│   │   └── backends/
│   │       ├── __init__.py
│   │       ├── sqlite.py          # SQLite 后端（默认）
│   │       ├── file.py            # JSON 文件后端（兼容）
│   │       └── chroma.py          # ChromaDB 向量后端（可选）
│   │
│   ├── llm/                        # LLM 适配
│   │   ├── __init__.py
│   │   ├── base.py                 # BaseLLM 抽象类
│   │   ├── anthropic.py           # Anthropic SDK
│   │   ├── openai.py              # OpenAI 兼容 SDK
│   │   ├── factory.py             # 自动检测与创建
│   │   └── errors.py              # 类型化 LLM 错误 (RateLimitError, etc.)
│   │
│   ├── utils/                      # 内置原子工具
│   │   ├── __init__.py
│   │   ├── json_extract.py        # A1: LLM JSON 提取
│   │   ├── retry.py               # A2: async 重试（指数退避 + jitter）
│   │   ├── timeout.py             # A3: async 超时
│   │   ├── timed.py               # C1: async 计时器
│   │   ├── fallback.py            # C2: 错误兜底
│   │   └── registry.py            # C3: 惰性注册表（TTL 淘汰）
│   │
│   ├── event_bus.py               # A5: 进程内 async pub/sub 事件总线
│   │
│   ├── features/                   # 可选 Feature（配置启用/禁用）
│   │   ├── __init__.py
│   │   ├── schema_validation.py   # B1: Pydantic Schema 校验
│   │   ├── structured_call.py     # B2: 结构化 LLM 调用
│   │   ├── two_stage.py           # B3: 两阶段 LLM 流水线
│   │   └── prompt_defense.py      # B4: Prompt 注入防御
│   │
│   ├── prompts/                    # Prompt 管理
│   │   ├── __init__.py
│   │   ├── loader.py              # 文件加载 + Schema 渲染
│   │   ├── schema.py              # Prompt Composition Schema 解析器
│   │   └── prompt_registry.py     # 命名 Prompt 注册表（自动识别 .md / .schema.yaml）
│   │
│   └── server/                     # 可选 REST 服务器
│       ├── __init__.py
│       ├── app.py                  # FastAPI app 工厂
│       ├── routes.py              # /agents/* 路由
│       └── ws.py                   # WebSocket 流式推送
│
├── examples/                       # 集成示例
│   ├── minimal/                    # 最小示例
│   │   ├── weave.yaml
│   │   ├── prompts/system.md
│   │   └── main.py
│   ├── mykg_enhanced/             # myKG + weave 集成示例
│   └── bepm_enhanced/             # bePM + weave 集成示例
│
└── tests/                          # 测试
    ├── test_agent.py
    ├── test_loop.py
    ├── test_memory.py
    └── test_config.py
```

---

## 6. 使用方式（API 设计）

### 6.1 run() 完整签名

```python
from weave_agent_sdk import Weave

weave = Weave("weave.yaml")

# 完整签名
result = weave.run(
    input: str,                          # 用户输入（必填）
    scope_hints: dict[str, str] = None,  # 作用域 ID 映射，用于解析 {session_id} 等占位符
    context: dict = None,                # 额外的运行时上下文，注入到 prompt 模板
    tool_filter: list[str] = None,       # 限制可用的 tool 列表（None = 全部可用）
)
# → LoopResult(output=str, elapsed_ms=int, iterations=int, memory_updated=dict)
# memory_updated: {"stream": {"session:abc123:stream": 5}, "state": {"session:abc123:state": 2}}
#   记录了本次 run 中哪些 namespace 被修改、写入条数
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `input` | `str` | 用户输入，作为 Loop 的初始 message |
| `scope_hints` | `dict[str,str]` | 可选。key 格式为 `{scope_name}_id`（如 scope 名 "quiz_session" → key 为 "quiz_session_id"）。用于解析 memory 配置中的 `{scope_name_id}` 占位符；未提供的 scope 自动生成 uuid |
| `context` | `dict` | 可选，注入到 prompt 模板 `{{ context.xxx }}`（scope_hints 的值也合并到此命名空间） |
| `tool_filter` | `list[str]` | 可选，限制本次 run 可用的 tool name 白名单 |

### 6.2 最小示例

```python
# main.py
from weave_agent_sdk import Weave

weave = Weave("weave.yaml")

@weave.tool
def search_docs(query: str) -> str:
    """搜索项目文档"""
    return doc_index.search(query)

result = weave.run("帮我找一个处理并发的方案")
print(result.output)
```

```yaml
# weave.yaml
llm:
  provider: anthropic
  model: claude-sonnet-5-20251001

loop:
  type: simple

memory:
  scopes:
    session:                     # priority: 0（最窄）
      stream:
        backend: sqlite
        path: ./data/memory.db
```

### 6.3 增强 myKG（示例）

```python
# mykg_agent.py
from weave_agent_sdk import Weave
from backend.engine.topology import topological_sort_with_depth
from backend.models.graph import KnowledgeBase

kb = KnowledgeBase.load("backend/data/kb.json")
weave = Weave("weave.yaml")

@weave.tool
def get_kb_status() -> dict:
    """获取知识库统计"""
    return {
        "total_nodes": len(kb.nodes),
        "lit_nodes": sum(1 for n in kb.nodes.values() if n.lit),
        "tags": list(kb.tags.keys())
    }

@weave.tool
def find_learnable_nodes() -> list[dict]:
    """找到所有前置条件已满足、可以开始学习的节点"""
    ready = []
    for node in kb.nodes.values():
        if not node.lit:
            parents = kb.get_parents(node.id)
            if all(p.lit for p in parents):
                ready.append({"name": node.name, "description": node.description})
    return ready

@weave.tool
def get_node_detail(name: str) -> dict | None:
    """获取知识节点的详细信息"""
    for node in kb.nodes.values():
        if node.name.lower() == name.lower():
            return {"name": node.name, "description": node.description, "lit": node.lit}
    return None

@weave.tool
def suggest_learning_path(topic: str) -> list[str]:
    """拓扑排序给出建议的学习路径"""
    learned = {n.id for n in kb.nodes.values() if n.lit}
    ordered = topological_sort_with_depth(kb.nodes, kb.edges)
    return [kb.nodes[nid].name for nid in ordered if nid not in learned]

# 启动自主 Agent 循环
result = weave.run("分析我当前的学习进度，推荐接下来学什么")
```

```yaml
# weave.yaml
llm:
  provider: anthropic
  model: claude-sonnet-5-20251001

loop:
  type: iterative
  max_iterations: 5
  stop_conditions:
    - type: no_tool_calls

memory:
  scopes:
    quiz_session:                    # priority: 0（默认，最小 = 最窄）
      stream:
        backend: sqlite
        path: ./data/sessions/{quiz_session_id}.db
        ttl: 3600
      state:
        backend: sqlite
        ttl: 3600
    domain:                       # priority: 10
      state:
        backend: sqlite
        path: ./data/domains/{domain_id}/memory.db
      knowledge:
        backend: sqlite
        fts: true
        path: ./data/domains/{domain_id}/knowledge.db

prompts:
  system: prompts/system.md
  loop_instruction: prompts/loop.md
```

### 6.4 增强 bePM（示例）

```python
# bepm_agent.py
from weave_agent_sdk import Weave
from backend.engine.scheduler import calculate_schedule
from backend.engine.parser import analyze_risks

weave = Weave("weave.yaml")

@weave.tool
def get_project_status(project_id: str) -> dict:
    """获取项目当前状态"""
    project = load_project(project_id)
    return {
        "total_tasks": len(project.nodes),
        "completed": sum(1 for n in project.nodes if n.status == "done"),
        "delayed": sum(1 for n in project.nodes if n.delayed),
        "buffer_remaining_days": project.buffer_days
    }

@weave.tool
def scan_risks(project_id: str) -> list[dict]:
    """扫描项目风险"""
    project = load_project(project_id)
    return analyze_risks(project)

@weave.tool
def get_critical_path(project_id: str) -> list[str]:
    """获取当前关键路径"""
    schedule = calculate_schedule(project)
    return [task_id for task_id in schedule.critical_path]

# 定时风险监控
result = weave.run("检查所有项目，找出有延迟风险的任务并汇报")
```

```yaml
# weave.yaml
llm:
  provider: anthropic
  model: claude-sonnet-5-20251001

loop:
  type: iterative               # 也可以设为 scheduled 定时扫描
  max_iterations: 5
  stop_conditions:
    - type: no_tool_calls

memory:
  scopes:
    workspace:                   # 跨项目工作空间 priority: 20
      knowledge:
        backend: chromadb
        path: ./data/workspace/vectors/
    project:                     # 单个项目 priority: 10
      state:
        backend: sqlite
        path: ./data/projects/{project_id}/memory.db
      knowledge:
        backend: sqlite
        fts: true
        path: ./data/projects/{project_id}/knowledge.db
    session:                     # priority: 0（最窄）
      stream:
        backend: sqlite
        ttl: 3600

prompts:
  system: prompts/system.md
  loop_instruction: prompts/loop.md
```

### 6.5 REST API（可选）

```bash
# 启动服务器
$ weave serve --config weave.yaml --port 48080

# 或程序化
from weave_agent_sdk.server import create_app
app = create_app(weave)
```

API 端点（与 SDK 同级抽象）。`{name}` 对应 `weave.yaml` 中 `agent.name` 字段，默认值 `"default"`。一个 Weave Server 实例可加载多个配置文件，每个对应一个命名 Agent：

| 方法 | 路径 | SDK 等价调用 |
|------|------|-------------|
| `POST` | `/agents/{name}/run` | `weave.run(input)` |
| `GET` | `/agents/{name}/memory` | `weave.memory.list()` |
| `DELETE` | `/agents/{name}/memory` | `weave.memory.clear()` |
| `GET` | `/agents/{name}/status` | `weave.status()` |
| `WS` | `/agents/{name}/stream` | `weave.stream(input)` |

> **weave.status() 返回值**：`{"agent_name": str, "loop_type": str, "is_running": bool, "last_run": {"started_at": float, "elapsed_ms": int, "iterations": int} | None, "memory_stats": {"total_entries": int, "by_scope": dict}}`

**SDK 公共 Memory API**：
```python
# ---- 直接读写 Memory（旁路方式，不经过 Loop）----

# -- 方式 A: 通过 namespace 字符串精确访问 --
weave.memory.stream.append({"role": "user", "content": "..."}, namespace="default:session:stream")
weave.memory.stream.last(20, namespaces=["default:session:stream"])
weave.memory.state.get("current_topic", namespace="default:project:state")
weave.memory.state.set("current_topic", "...", namespace="default:project:state")
weave.memory.knowledge.search("query", namespaces=["default:global:knowledge"])

# -- 方式 B: 通过 scope 名访问（推荐，自动补全 namespace）--
weave.memory.stream.append({"role": "user", "content": "..."}, scope="session")
weave.memory.stream.last(20, scopes=["session"])
weave.memory.state.get("current_topic", scope="project")
weave.memory.state.set("current_topic", "...", scope="project")
weave.memory.knowledge.search("query", scopes=["workspace"])

# -- 管理操作 --
weave.memory.list(scope: str = None)   # → 列出指定 scope（或全部）的 namespace 及其统计
weave.memory.clear(scope: str = None)  # → 清空指定 scope（或全部 scope）的数据
# 不传参数时 list/clear 作用于所有 scope；clear(all=True) 需显式确认
```

**SDK 流式 API**：
```python
# 与 weave.run() 等价，但通过 async generator 逐 event 产出
async for event in weave.stream("我应该学什么?"):
    # event 类型及结构:
    # {"type": "token",       "text": str, "index": int}
    # {"type": "tool_call",   "name": str, "arguments": dict}
    # {"type": "tool_result", "name": str, "result": Any, "error": bool}
    # {"type": "done",        "output": str, "elapsed_ms": int, "iterations": int}
    # {"type": "error",       "message": str, "exception": str}
    match event["type"]:
        case "token":       print(event["text"], end="", flush=True)
        case "tool_call":   print(f"\n🔧 {event['name']}({event['arguments']})")
        case "tool_result": print(f"  → {'❌' if event['error'] else '✅'}")
        case "done":        print(f"\n\n完成 ({event['iterations']} 轮, {event['elapsed_ms']}ms)")
        case "error":       print(f"\n❌ {event['message']}")
```

---

## 7. 数据流

### 7.1 一次完整的 IterativeLoop 执行

```
用户调用 weave.run("我应该学什么?")

┌──────────────────────────────────────────────────────────────┐
│ 1. Loop.on_start()                                          │
│    └─ 记录开始时间，初始化上下文                               │
├──────────────────────────────────────────────────────────────┤
│ 2. Memory 注入 (before_think)                                │
│    ├─ StreamMemory: 加载最近 20 轮对话                         │
│    ├─ StateMemory: 加载 {"current_topic": "推荐系统"}           │
│    └─ KnowledgeMemory: search("学什么") → top 3 相关知识        │
├──────────────────────────────────────────────────────────────┤
│ 3. LLM 调用                                                 │
│    ├─ System: prompts/system.md + memory context             │
│    ├─ Messages: conversation history                        │
│    └─ Tools: 当前注册的所有 tool 的 JSON Schema               │
├──────────────────────────────────────────────────────────────┤
│ 4. LLM 返回 → 决定调用 get_kb_status()                       │
│    └─ after_think: 将 assistant message 写入 StreamMemory       │
├──────────────────────────────────────────────────────────────┤
│ 5. 执行 Tool: get_kb_status()                                │
│    ├─ Tool 返回: {"total_nodes": 50, "lit_nodes": 12, ...}   │
│    └─ 工具结果作为 tool_result message 加入上下文              │
├──────────────────────────────────────────────────────────────┤
│ 6. 再次 LLM 调用（步骤 3 重复）                                │
│    └─ LLM 调用 find_learnable_nodes() → 返回 3 个可学节点     │
├──────────────────────────────────────────────────────────────┤
│ 7. 再次 LLM 调用                                             │
│    └─ LLM 决定不调工具，直接输出建议                           │
│    └─ stop_conditions: no_tool_calls → 停止                   │
├──────────────────────────────────────────────────────────────┤
│ 8. after_think: 将最终响应写入 StreamMemory                    │
│    StateMemory: 更新 {"last_suggestion": ..., "timestamp"}      │
├──────────────────────────────────────────────────────────────┤
│ 9. Loop.on_end()                                             │
│    └─ 返回 LoopResult(output="建议你接下来学...")              │
└──────────────────────────────────────────────────────────────┘
```

### 7.2 ScheduledLoop 的执行

```
                    ┌──────────┐
                    │  IDLE    │
                    └────┬─────┘
                         │ cron 触发 / 事件触发
                    ┌────▼─────┐
                    │ 加载配置  │
                    │ 加载Memory│
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ LLM 思考  │◄──── 调用 Tool
                    └────┬─────┘         │
                         │        ┌──────┘
                    ┌────▼─────┐  │
                    │ 需要调    │──┘
                    │ 用Tool?   │
                    └────┬─────┘
                         │ 否
                    ┌────▼─────┐
                    │ 更新     │
                    │ Memory   │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │ 持久化    │
                    │ 返回结果  │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │  等待     │
                    │ 下一次触发 │
                    └──────────┘
```

**ScheduledLoop 配置热重载**：每次 cron 触发时重新读取 `weave.yaml`，以下字段实时生效无需重启：
- `llm.model`, `llm.max_tokens`, `llm.temperature`
- `loop.schedule`（下次触发按新周期）
- `memory.scopes` 中 scope 的 `ttl`, `max_items`

以下字段在下一次 `weave serve` 重启后才生效（因涉及连接池/调度器重建）：
- `llm.provider`
- `loop.type`（如从 iterative 改为 scheduled 或反之）
- `memory.scopes` 中 scope 的新增/删除/backend 变更

---

## 8. 配置规范

### 8.1 完整配置参考

```yaml
# weave.yaml — 完整配置

# LLM 配置（必填）
llm:
  provider: anthropic                    # anthropic | openai
  model: ${WEAVE_MODEL:-claude-sonnet-5-20251001}
  max_tokens: ${WEAVE_MAX_TOKENS:-4096}
  temperature: ${WEAVE_TEMP:-0.7}
  # api_key 从环境变量 ANTHROPIC_API_KEY 读取

# Loop 配置（必填）
loop:
  type: iterative                       # simple | iterative | scheduled
  
  # IterativeLoop 专属配置
  max_iterations: 10
  stop_conditions:
    - type: no_tool_calls
    - type: tool_call
      name: finish
      
  # ScheduledLoop 专属配置
  # schedule: "0 */6 * * *"

# Memory 配置（可选）
# 不写 memory 段时的默认行为：
#   - 自动创建一个名为 "default" 的 scope
#   - 配 SQLite stream + state，路径 ./data/memory.db
#   - 无 TTL、无 knowledge
# scope 已定义但某 access mode 省略 path 时：
#   - SQLite: 同 scope 内的所有 SQLite access mode 共享同一文件，
#     文件路径取 scope 内第一个指定的 path，若全未指定则默认 ./data/{scope_name}.db
#   - ChromaDB: 默认路径 ./data/{scope_name}_vectors/
memory:
  scopes:
    # 项目自定义作用域。priority 越小越"窄"（值优先被采用）
    # 加载时按 priority 升序排列，同 key 时窄 scope 覆盖宽 scope
    session:                         # priority: 0（默认，最小 = 最窄）
      stream:                        # 对话流
        backend: sqlite
        path: ./data/sessions/{session_id}.db
        ttl: 3600                    # 1 小时后过期
        max_items: 100               # 滑动窗口大小
      state:                         # 临时状态
        backend: sqlite
        ttl: 3600
    project:                         # 项目级（可选）priority: 10
      state:
        backend: sqlite
        path: ./data/projects/{project_id}/memory.db
        ttl: null                    # 永不过期
      knowledge:
        backend: sqlite
        fts: true                    # SQLite FTS5 全文搜索
        ttl: null
    global:                          # 全局级（可选）priority: 20
      knowledge:
        backend: chromadb            # 向量语义搜索
        path: ./data/vectors/
        embedding_model: text-embedding-3-small  # 任意 OpenAI 兼容 embedding 模型名
        # embedding 需要额外的 OPENAI_API_KEY（或通过 base_url 指向其他兼容服务）
        # 如不填 embedding_model，ChromaDB 使用内置的 all-MiniLM-L6-v2（本地运行，无需 API key）

# Prompt 配置（必填）
prompts:
  # 模式 A：单文件（简单场景）
  system: prompts/system.md
  loop_instruction: prompts/loop.md
  memory_use: prompts/memory_use.md       # 可选

  # 模式 B：Schema 组装（复杂场景，可替换上面三行）
  # system: prompts/coach.schema.yaml     # Schema 描述 Prompt 的结构和组装规则
  # Schema 格式见 DESIGN.md §4.4

# Agent 标识（可选，REST API 的 /agents/{name} 使用此值，默认 "default"）
agent:
  name: my_agent

# 可选 Feature 开关（详见 Section 4.6）
features:
  schema_validation: true       # B1: Pydantic Schema 校验
  structured_call: true         # B2: 一键结构化 LLM 调用
  two_stage_pipeline: true      # B3: 两阶段 LLM 流水线
  prompt_defense: true          # B4: Prompt 注入防御

# 服务配置（可选，仅 weave serve 时使用）
server:
  host: ${WEAVE_HOST:-127.0.0.1}
  port: ${WEAVE_PORT:-48080}
  cors_origins:
    - http://localhost:5173

# 日志
logging:
  level: ${WEAVE_LOG_LEVEL:-INFO}
  file: ${WEAVE_LOG_FILE:-./logs/weave.log}
```

### 8.2 环境变量

| 变量 | 用途 | 必需 |
|------|------|------|
| `ANTHROPIC_API_KEY` | Anthropic API Key | 使用 Anthropic 时 |
| `OPENAI_API_KEY` | OpenAI API Key | 使用 OpenAI 时 |
| `WEAVE_MODEL` | 覆盖模型名 | 否 |
| `WEAVE_DATA_DIR` | 数据存储目录 | 否 |
| `WEAVE_PORT` | 服务器端口 | 否 |
| `WEAVE_LOG_LEVEL` | 日志级别 | 否 |

---

## 9. 与非目标对比

| 维度 | myKG / bePM（现状） | Weave（目标） |
|------|-------------------|--------------|
| **Loop** | 纯响应式，无迭代 | 3 种策略：Simple/Iterative/Scheduled |
| **Memory** | JSON 文件 + 内存 dict | 按访问模式：stream/state/knowledge，作用域自定义，namespace 隔离 |
| **集成方式** | REST API，无 pip 包 | `pip install` + `from weave_agent_sdk import Weave` |
| **配置** | 部分 env var，部分硬编码 | 全部 YAML + env var 注入，模板语法 `${VAR:-default}` |
| **Prompt** | 代码中硬编码（myKG）/ 部分外置（bePM） | 100% 外置 .md 文件，模板变量 |
| **LLM 适配** | 手动选择 | 自动检测（env → claude settings → 默认） |
| **持久化** | 单 JSON 文件无事务 | SQLite 事务安全 |
| **跨项目学习** | 不支持 | KnowledgeMemory 语义检索 |

---

## 10. 实现路线图

### Phase 1: 核心 SDK（MVP）

- [ ] `WeaveConfig` — 配置加载（YAML + env var 注入）
- [ ] `BaseLLM` + Anthropic/OpenAI adapter + 自动检测 + LLM 错误类型
- [ ] `BaseLoop` + `SimpleLoop` 实现
- [ ] `BaseMemory` + `StreamMemory` + `StateMemory` (SQLite 后端)
- [ ] Prompt loader（.md 文件 + 变量替换 + Schema 组装）
- [ ] `Weave` agent 类 — 编排 loop + memory + llm
- [ ] Tool registry（装饰器 + 注册函数）
- [ ] **A1-A5 核心基础设施**: extract_json, async_retry, async_timeout, llm_errors, event_bus
- [ ] **C1-C3 内部工具**: timed_async, fallback, lazy_registry
- [ ] `pyproject.toml` — 可 pip install

### Phase 2: 高级 Loop & Memory + 可选 Feature

- [ ] `IterativeLoop` — 多轮迭代 + 停止条件
- [ ] `MemoryManager` — 统一管理作用域和 namespace 的加载/保存/清理
- [ ] Loop 生命周期钩子
- [ ] **B1**: Schema 校验
- [ ] **B2**: 结构化 LLM 调用
- [ ] **B3**: 两阶段 LLM 流水线

### Phase 3: 定时循环 & 长期记忆

- [ ] `ScheduledLoop` — cron 定时 + 事件触发
  - 优雅关闭：收到 shutdown signal 后完成当前迭代再退出，超时 30s 强制终止
  - 失败语义：单次迭代失败不影响后续 cron 触发。失败详情写入 StateMemory（`last_run_error`），可选通过 event_bus 推送告警
- [ ] `KnowledgeMemory` + ChromaDB 后端（可选）
- [ ] Memory 语义搜索集成到 LLM context
- [ ] **B4**: Prompt 注入防御

### Phase 4: REST Server & 集成示例

- [ ] FastAPI server — `/agents/*` 路由 + 事件总线 → SSE/WebSocket 桥接
- [ ] WebSocket 流式推送
- [ ] myKG 集成示例
- [ ] bePM 集成示例

---

## 11. 决策记录

> 2026-07-29, Asher & Claude. 所有问题已确认。

| # | 问题 | 决策 |
|---|------|------|
| Q1 | 语言选型 | **Python 3.11+**，FastAPI（server 部分） |
| Q2 | Loop 范围 | **Simple + Iterative**，Scheduled 后续版本 |
| Q3 | Memory 默认后端 | **SQLite 默认**，保留 file 后端兼容 myKG/bePM |
| Q4 | Knowledge 检索 | **SQLite FTS5 全文搜索**，ChromaDB 向量搜索作为扩展 |
| Q5 | Async vs Sync | **内部 async**，对外 `weave.run()` (sync) + `weave.arun()` (async)，接口统一 |
| Q6 | 发布形式 | **本地 pip install**（`pip install -e .`），后续可发布 PyPI |

详细讨论记录：
- Async/Sync 设计决策：`docs/issues/001-async-vs-sync.md`
