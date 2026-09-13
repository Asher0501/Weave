# retired-scope —— 从门面文档移出的内容（保留，不展示）

`README.md` / `docs/weave-llm-dimension.md` / `docs/weave-global-architecture.md` 现在
**只描述 weave 现在有的东西**。本文保存那些被移出但仍需备查的内容：归档清单、迁移映射、
决策状态。它们不是"能力"，是**历史账目**。

## 1. 已归档的能力（archive/v0.4-parked/）

| 归档内容 | 原来的定位 | 为什么不在对象里 |
|---|---|---|
| `weave/logs/` | `ConversationLog`（追加日志） | 会话历史属应用 |
| `weave/search/` | `Search`（检索，RAG 落点） | 上下文从哪来属应用 |
| `weave/executors/` | `Executor`（执行 handler） | 执行属应用 |
| `weave/catalogs/` | `EnvironmentCatalog`（schema 反射） | 同上 |
| `weave/strategy/` | 组装策略（记忆读写 / 上下文拼装） | 上下文属应用 |
| `weave/capabilities/` | trace / checkpoint | 无消费者 |
| `weave/core/codec.py` | 消息编解码 | 只被 demos/atomic 用 |
| `weave/stores/redis_store.py` | `RedisStateStore` | 无消费者 |
| `demos/atomic/` | Agent / loop / context / tool_registry 示例 | 面向旧形态 |
| legacy provider（`openai_compat` + `_reliability` + `adapter`，419 行） | openai SDK + 内嵌重试 | 只留零依赖实现 |
| 接口 `Provider` · `EventSink` · `NoopEventSink` | 旧 provider 契约 · 事件 sink | 可观测性只留 `observer=` 回调 |
| 类型 `ToolResult` · `SearchHit`；错误 `ToolError` / `CatalogError` / `StorageError` / `ProtocolError` / `ContextLengthError` / `RetryExhaustedError` | 归档维度 / 旧重试的错误 | weave 内已无生产者 |
| `stores/{memory,sqlite}.py` 的 `search` / `trim` / `list_namespaces` / `list_keys` | 存储的顺手能力（无契约） | 无消费者 |

保留的是**真有消费者**的两样：`LLMProvider`（本维度契约）· `StateStore`（KV 存储）。

## 2. 代码去留映射（v0.4 收缩时）

| 原位置 | 去向 | 原因 |
|---|---|---|
| `weave/parsers/*`（native/JSON/DSML） | 逻辑迁入 `weave/llm/decode.py` | 解码属于"读懂模型输出" |
| `weave/strategy/session.py`（重试/超时/分类） | 逻辑迁入 `weave/llm/reliability.py` | 单动作可靠性属于本维度 |
| `weave/strategy/assembly.py` | 归档 | 上下文拼装属应用 |
| `logs` · `search` · `executors` · `catalogs` · `capabilities` · `core/codec.py` · `stores/redis_store.py` · `demos/atomic` | 归档（含各自测试） | 无真实消费者 |
| `core/interfaces.py` | 收缩为 2 个（`LLMProvider` · `StateStore`） | 契约面 = 有消费者的接口 |
| `core/envelopes.py` | 收缩为一套信封 | 未来维度用的 `Scope`/`Record`/`Context`… 已删除 |
| `providers/openai_compat.py` · `_reliability.py` · `adapter.py` | 归档 | 只有 agora 在用 |
| `providers/openai_http.py`（新增） | 哑 provider（零依赖 + 注入式 transport） | A2/A3/B1–B3/E3 由它承担 |
| `providers/sse.py` · `transport.py`（新增） | SSE 半行缓冲 · 可注入 HTTP 传输 | F1/F2 的落点 |

## 3. 决策状态（v0.4 时点）

| # | 决策 | 状态 |
|---|---|---|
| D1 | 重试/超时/分类属"单动作内部事务" | ✅ 落在 `weave.llm.reliability` |
| D5 | 配置形态：构造器 + 实例属性 + 无全局可变状态 | ✅ |
| D13 | 解码只有一处 | ✅ 落在 `weave.llm.decode`（原子不解码） |
| D14 | 对象不拼装消息、不碰存储 | ✅（更彻底：对象不认识存储） |
| D15 | 判据：同一请求的重放 = 本维度；变更输入的再一次动作 = 应用 | ✅ |
| D18 | 可观测性 = 注入回调；对象不写日志不埋点 | ✅ |
| D2 / D10 | schema 归"引擎"、以 `describe()` 暴露 | 📦 随 `catalogs`/`executors` 归档 |
| D3 / D6 / D7 / D8 | 记忆默认行为 · `prompts`/`context` 语义 · 命名约定在策略层 | 📦 随 `strategy` 归档 |
| D11 / D12 | `ConversationLog` / `Search` 作为契约接口 | 📦 归档（接口与实现一起） |
| D4 / D9 / D16 / D17 | `run()` 默认编排 · `RunResult` · `prompts`/`initial` · 薄委托 | ❌ 不再存在：循环与输入都归应用 |

## 4. 第二批收缩的后果（历史记录）

`14_forum/agora/adapter/llm.py` 会 `ModuleNotFoundError`（它 import 了
`weave.providers.openai_compat`）。这是当时明确接受的取舍；
`agora.adapter.repository` / `agora.atoms` / `agora.relay` 仍可正常导入
（它们只用 `weave.stores.sqlite` 与 `weave.core.types`）。
