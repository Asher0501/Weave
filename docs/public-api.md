# Weave — 对外接口清单

> **目的**：明确哪些接口是稳定的公共 API（外部项目依赖），哪些是内部实现（可随时重构）。
> 后续优化 Weave 时，**稳定公开 API 的签名和行为不改变**，保证外部不感知。
>
> 最后更新：2026-07-30

---

## 1. 稳定公开 API（不可变）

这些是外部项目 `from weave_agent_sdk import ...` 后使用的接口，修改签名/行为会破坏下游。

### 1.1 核心入口 — `Weave` 类

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `(config_path: str \| Path \| None = None, *, config: WeaveConfig \| None = None, llm: BaseLLM \| None = None, loop: BaseLoop \| None = None)` | 三种构造：`Weave()` 零配置 / `Weave(path)` 读文件 / `Weave(config=...)` 程序化；`llm=` / `loop=` 为运行期注入 |
| `run()` | `(input: str, scope_hints: dict \| None = None, context: dict \| None = None, tool_filter: list[str] \| None = None) -> LoopResult` | 同步执行 |
| `arun()` | `(input: str, scope_hints: dict \| None = None, context: dict \| None = None, tool_filter: list[str] \| None = None) -> LoopResult` | 异步执行 |
| `stream()` | `(input: str, scope_hints: dict \| None = None, context: dict \| None = None, tool_filter: list[str] \| None = None) -> AsyncIterator[WeaveEvent]` | 流式执行 |
| `tool()` | `(fn: Callable = None, *, name: str \| None = None, description: str \| None = None, schema: dict \| None = None) -> Callable` | 装饰器注册 tool，可显式覆盖 name/description/schema |
| `register_tool()` | `(fn: Callable, *, name: str \| None = None, description: str \| None = None, schema: dict \| None = None) -> None` | 直接注册 tool（等价 `@weave.tool`） |
| `on()` | `(*event_types: str) -> AsyncIterator[WeaveEvent]` | 订阅事件（async generator，`async for` 消费） |
| `emit()` | `async (event_type: str, data: dict \| None = None) -> None` | 发布事件（需 `await`） |
| `memory` | `-> MemoryManager` | Memory 公共访问入口 |
| `last_trace` | `-> dict \| None` | 最近一次 run 的完整 trace 树（`observability.enabled` 时才非 None） |
| `status()` | `() -> dict` | 返回 Agent 状态 |
| `checkpoint()` | `() -> str` | 手动打状态快照，返回 checkpoint_id |
| `checkpoints()` | `() -> list[dict]` | 列出当前 session 的所有快照 |
| `rollback()` | `(checkpoint_id: str \| None = None) -> str` | 回滚到指定快照（默认最近一个） |
| `acheckpoint()` | `async () -> str` | 异步版 checkpoint() |
| `arollback()` | `(checkpoint_id: str \| None = None) -> str` | 异步版 rollback() |

**`weave.run()` 返回值 `LoopResult`**：
```python
LoopResult = namedtuple("LoopResult", [
    "output",         # str — 最终文本输出
    "elapsed_ms",     # int — 总耗时（毫秒）
    "iterations",     # int — LLM 调用次数
    "memory_updated", # dict — access_type → namespace → 写入条数
])
```

### 1.2 Memory 公共 API — `MemoryManager`

| 方法 | 签名 | 说明 |
|------|------|------|
| `stream.append()` | `(entry: dict, namespace: str) -> str` | 追加消息，返回 entry ID |
| `stream.last()` | `(n: int = 20, namespaces: list[str] \| None = None) -> list[dict]` | 最近 N 条消息 |
| `stream.trim()` | `(max_items: int, namespace: str) -> None` | 裁剪到 max_items |
| `state.get()` | `(key: str, namespace: str) -> Any \| None` | 读取键值 |
| `state.set()` | `(key: str, value: Any, namespace: str) -> str` | 写入键值，返回 entry ID |
| `state.delete()` | `(key: str, namespace: str) -> None` | 删除键 |
| `state.get_all()` | `(namespaces: list[str] \| None = None) -> dict[str, Any]` | 获取所有键值对 |
| `knowledge.add()` | `(content: str, namespace: str, metadata: dict \| None = None) -> str` | 追加知识 |
| `knowledge.search()` | `(query: str, namespaces: list[str] \| None = None, top_k: int = 5) -> list[SearchResult]` | 搜索知识 |
| `activate_scopes()` | `(scope_hints: dict[str, str] \| None = None) -> None` | 激活作用域（`{scope_name}_id` → scope_id，未提供回退 scope_name） |
| `get_namespaces()` | `(access_type: str) -> list[str]` | 获取指定类型的 namespace 列表 |
| `get_namespace()` | `(scope_name: str, access_type: str) -> str` | 获取单个 namespace |
| `active_scope_names` | `-> list[str]` | 已激活的 scope 名称列表 |
| `stats()` | `(purge: bool = False) -> dict[str, int]` | namespace 统计（purge=True 先清理过期） |
| `close()` | `() -> None` | 关闭所有后端连接 |
| `cleanup()` | `() -> int` | 清理过期数据 |

### 1.3 公开类型 — `weave.types`

| 类型 | 字段 | 用途 |
|------|------|------|
| `Message` | `role, content, name, tool_call_id, tool_calls` | LLM 对话消息 |
| `ToolCall` | `id, name, arguments` | LLM 请求的 tool 调用 |
| `ToolResult` | `tool_call_id, name, result, error` | Tool 执行结果 |
| `LoopResult` | `output, elapsed_ms, iterations, memory_updated` | run() 返回值 |
| `SearchResult` | `id, content, score, metadata` | knowledge 搜索结果 |
| `WeaveEvent` | `type, data, timestamp` | 事件总线事件 |
| `WeaveConfig` | `agent, llm, loop, memory, prompts, features, server, logging, checkpoint` | 顶层配置 |

### 1.4 公开函数

| 函数 | 签名 | 说明 |
|------|------|------|
| `load_config()` | `(config_path: str \| Path) -> WeaveConfig` | 加载配置文件 |
| `extract_json()` | `(text: str) -> dict \| list` | 从 LLM 文本中提取 JSON |

### 1.5 REST API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/agents/{name}/run` | 执行 Agent |
| `GET` | `/agents/{name}/memory` | 获取 Memory 状态 |
| `DELETE` | `/agents/{name}/memory` | 清空 Memory |
| `GET` | `/agents/{name}/status` | 获取 Agent 状态 |
| `WS` | `/ws/agents/{name}/stream` | WebSocket 流式 |

### 1.6 stream() 事件 schema

```python
{"type": "token",       "text": str, "index": int}
{"type": "tool_call",   "name": str, "arguments": dict}
{"type": "tool_result", "name": str, "result": Any, "error": bool}
{"type": "done",        "output": str, "elapsed_ms": int, "iterations": int}
{"type": "error",       "message": str, "exception": str}
```

### 1.7 并发语义（重要行为契约）

**同一个 `Weave` 实例对 `run()` / `arun()` / `stream()` 的调用是串行的**（每个事件循环一把锁）。这是为了隔离共享实例状态（`_is_running` / `_system_prompt` / `_tools` / 激活 scope / memory 写入计数），避免并发调用互相污染。

需要并发时，**每个请求/协程新建一个 `Weave` 实例**（配置相同即可）。

`emit()` / `on()` 示例：
```python
# 订阅事件（async generator，async for 消费）
async for event in weave.on("token", "done"):
    print(event.type, event.data)

# 发布事件（async，需 await）
await weave.emit("data_change", {"reason": "manual"})
```

### 1.8 超时配置速查（`loop.*`）

五个超时语义不同，勿混用：

| 配置项 | 默认 | 语义 |
|------|------|------|
| `loop.timeout` | 5.0s | `stream()` 收到 `done`/`error` 后等后台任务收尾的宽限时长（**非**整次运行期限） |
| `loop.stream_timeout` | None | `stream()`/WS 空闲超时（自上次事件计时，事件到达即重置；None=不启用） |
| `loop.tool_timeout` | 30.0s | 单个工具执行的硬超时 |
| `loop.llm_timeout` | 120.0s | 在途非流式 LLM 调用被视为「活动」的最长时长（超此视为挂起，允许空闲超时取消） |
| `loop.llm_call_timeout` | 120.0s | 单次非流式 LLM 调用的硬超时（覆盖整个重试序列） |

另见 `loop.llm_retry_attempts`（重试次数，默认 3）、`loop.llm_retry_backoff`（退避倍率，默认 2.0）。

---

## 2. 扩展点（需实现特定接口，签名稳定）

### 2.1 自定义 Loop

```python
class BaseLoop(ABC):
    @abstractmethod
    async def run(self, agent: Any, user_input: str) -> LoopResult: ...
    async def on_start(self, agent: Any, input: str) -> None: ...
    async def before_think(self, agent: Any, current_input: str) -> dict[str, Any]: ...
    async def after_think(self, agent: Any, response: Any) -> None: ...
    async def on_end(self, agent: Any, result: LoopResult) -> None: ...
```

### 2.2 自定义 LLM 适配器

```python
class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: list[Message], tools: list[dict] | None = None,
                   max_tokens: int = 4096, temperature: float = 0.7) -> LLMResponse: ...
    @abstractmethod
    async def chat_stream(self, messages: list[Message], tools: list[dict] | None = None,
                          max_tokens: int = 4096, temperature: float = 0.7) -> AsyncIterator[str]: ...
```

### 2.3 自定义 Memory 后端

```python
class StreamMemory(ABC):     # append / last / trim
class StateMemory(ABC):      # get / set / delete / get_all
class KnowledgeMemory(ABC):  # add / search
```

### 2.4 注册与注入（真实扩展点）

内置实现（anthropic/openai/deepseek、simple/iterative/scheduled、sqlite/file/chroma）
是**预注册的默认值**；宿主经 `register_*` 注册自定义实现后，即可在 `weave.yaml` 中用
其名称引用（`llm.provider` / `loop.type` / scope `backend`）。

```python
from weave_agent_sdk import register_llm, register_loop, register_memory_backend

register_llm("my_gateway", my_gateway_factory)      # llm.provider: my_gateway
register_loop("human_review", HumanReviewLoop)       # loop.type: human_review
register_memory_backend("redis", RedisBackend)       # backend: redis
```

`Weave.__init__` 的 `llm=` / `loop=` 为运行期注入（离线/测试），优先级高于配置。

> **注意**：`register_*` 是全局注册，需在首次 `Weave(...)` 构造前调用（通常在 import 期），
> 否则该实例在构造时已按内置名字解析完 `loop.type` / `provider`。

---

## 3. 内部实现（可随时重构，不受兼容约束）

以下方法/属性以 `_` 前缀标记，外部不应依赖：

| 模块 | 内部接口 | 说明 |
|------|---------|------|
| `Weave` | `._config`, `._llm`, `._loop`, `._memory`, `._tools`, `._tool_map`, `._tool_meta`, `._event_bus`, `._prompts`, `._system_prompt`, `._is_running`, `._last_run` | 内部状态 |
| `Weave` | `._run_impl()`, `._create_loop()`, `._load_system_prompt()`, `._register_tool_internal()` | 内部方法 |
| `MemoryManager` | `._backends`, `._active_scopes`, `._config` | 内部状态 |
| `MemoryManager` | `._get_backend_for_namespace()` | 内部方法 |
| `EventBus` | `._subscribers`, `._max_queue_size` | 内部状态 |
| `Config` | `_resolve_env()`, `_parse_memory_scopes()` | 内部函数 |
| `LLM` | `_auto_detect_provider()`, `_resolve_api_key()`, `_default_model()` | 内部函数 |
| `IterativeLoop` | `_build_tool_schemas()`, `_execute_tool()`, `_is_finish_tool()`, `_format_tool_result()` | 内部 helper |

---

## 4. 可选 Feature（配置开关，签名稳定）

| 模块 | 函数 | 说明 |
|------|------|------|
| `weave.features.schema_validation` | `validate_schema(data, schema) -> (instance, error)` | Pydantic 校验 |
| `weave.features.structured_call` | `structured_call(llm, prompt, schema, ...) -> Any` | 结构化 LLM 调用 |
| `weave.features.two_stage` | `two_stage_call(understand_prompt, translate_schema, ...) -> Any` | 两阶段 LLM |
| `weave.utils.json_extract` | `extract_json(text) -> dict\|list` | JSON 提取 |

---

## 5. 不可变承诺

优化/重构 Weave 内部时，以下保证不变：

1. **`weave = Weave("config.yaml")` + `weave.run("...")` 的 5 行启动方式不变**
2. **`LoopResult` 的 4 个字段（output, elapsed_ms, iterations, memory_updated）不变**
3. **`stream()` 的 5 种事件类型和 schema 不变**
4. **`MemoryManager` 的 `stream/state/knowledge` 三级访问模式不变**
5. **`weave.yaml` 的配置格式（llm/loop/memory/prompts/server）向后兼容**
6. **REST API 端点路径和方法不变**
7. **所有 `_` 前缀的方法/属性可随时删除或重命名**
