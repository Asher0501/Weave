# archive/v0.4-parked —— 已归档的能力（不是删除）

## 为什么在这里

weave 被收缩成**一个功能**：LLM 交互（`weave.llm(...)`，一个输入、一个输出）。
判据只有一条：**有没有真实消费者**。

清点结果（2026-09 会话）：真正有人在用的只有四个名字——

| 保留 | 谁在用 |
|---|---|
| `LLMProvider` | LLM 交互对象（唯一契约） |
| `Provider`（legacy） | `OpenAICompatibleProvider`，agora 在用 |
| `StateStore` | `SQLiteStateStore` / `InMemoryStateStore`，agora 在用 |
| `EventSink` / `NoopEventSink` | 对象的 `events=` 可观测性通道 |

其余接口与实现**只有它们自己的测试在用**，于是整体移到这里。
将来真要做那些维度，从这里取回即可（取回时要一并取回对应测试）。

## 归档了什么

| 归档内容 | 原来的定位 | 为什么移除 |
|---|---|---|
| `weave/logs/` | `ConversationLog`（追加日志，O(1) 追加） | 会话历史属应用/未来维度，对象不用 |
| `weave/search/` | `Search`（检索，RAG 的落点） | 同上 |
| `weave/executors/` | `Executor`（执行 Python handler） | 执行属应用，weave 不碰 |
| `weave/catalogs/` | `EnvironmentCatalog`（schema 反射） | 同上 |
| `weave/strategy/` | 组装策略（记忆读写/上下文拼装） | 上下文属应用 |
| `weave/capabilities/` | trace / checkpoint | 无消费者 |
| `weave/core/codec.py` | 消息编解码 | 只被 demos/atomic 用 |
| `weave/stores-redis_store.py` | `RedisStateStore` | 无消费者（agora 用 SQLite） |
| `demos/atomic/` | Agent / loop / context / tool_registry 组合示例 | 面向旧形态的 weave 对象 |
| `tests/test_agent.py` 等 5 个 | 上述能力的测试 | 与被归档代码一同搬家 |
| `docs/weave-envelope-protocols.md` | 信封与接口协议 + 邻接矩阵 | 描述的是已归档的接口 |
| `docs/weave-two-views.md` · `weave-view-{a,b}-*.svg` | 两视图（逻辑分解 / 时序编排） | 旧形态（对象 = 四原子 + 策略 + 编排） |

### 第二批（"最狠"收缩）

| 归档内容 | 原来的定位 | 为什么移除 |
|---|---|---|
| `weave/providers/openai_compat.py`（267 行） | `OpenAICompatibleProvider`（openai SDK + 内嵌重试） | 只有 agora 在用；weave 只留一套零依赖实现 |
| `weave/providers/_reliability.py`（152 行） | provider 内部的重试/超时工具 | 可靠性已统一到 `weave.llm.reliability` |
| `weave/providers/adapter.py`（63 行） | `LegacyProviderAdapter` | 被适配的对象已不存在 |
| `tests/test_provider.py` | 上述三块的测试 | 随代码搬家 |
| 接口 `Provider` · `EventSink` · `NoopEventSink` | 旧形态 provider 契约 · 事件 sink | 没有消费者；可观测性只留 `observer=` 回调 |
| 类型 `ToolResult` · `SearchHit` | 旧翻译循环的结果 · 检索命中 | 没有消费者 |
| 错误 `ToolError` · `CatalogError` · `StorageError` · `ProtocolError` · `ContextLengthError` · `RetryExhaustedError` 与对应失败 `kind` | 归档维度/旧重试的错误类型 | weave 内已无生产者 |
| `weave/stores/{memory,sqlite}.py` 的 `search` / `trim` / `list_namespaces` / `list_keys` | 存储的顺手能力（无契约） | 用到时从归档取回 |
| `docs/weave-architecture.*` · `docs/weave-layered-architecture.*` | 中间态设计文档与图 | 描述的接口已不存在 |

**第二批的后果（明确记录）**：`14_forum/agora/adapter/llm.py` 会 `ModuleNotFoundError`
（它 import 了 `weave.providers.openai_compat`）。这是当时明确接受的取舍；
`agora.adapter.repository` / `agora.atoms` / `agora.relay` 仍可正常导入（它们只用
`weave.stores.sqlite` 与 `weave.core.types`）。

### 第三批（文档与图收敛）

门面文档（`README.md` / `docs/weave-*.md`）改为**只描述 weave 现在有的东西**，
被移出的"归档清单 / 迁移映射 / 决策状态"整体存进 `retired-scope.md`（保留，不展示）：

| 归档内容 | 原来在哪 | 为什么移出 |
|---|---|---|
| `retired-scope.md` | `docs/weave-llm-dimension.md` §5/§6 · `docs/weave-global-architecture.md` §3/§4 | 门面文档不列已归档能力与历史决策 |
| `renderers/render_architecture_{png,ascii}.py` | `scripts/` | 画的是含"已归档能力"那一版的图 |
| `renderers/weave-global-architecture.{png,mmd}` | `docs/` | 同上（旧图第④栏列的就是已归档能力） |
| `docs-v0.4/` | `docs/v0.4/` | "原子组件库"时期的设计基线与 agora 迁移报告 |
| `dx-smoke/` | `tests/v04/.work/cli_smoke/` | 旧 DX 时代（`weave.dx.builder`，该模块早已不存在）的示例工程，误提交进了测试的运行时临时目录。`.work/` 这类编译/运行产物现已进 `.gitignore` |

现由零依赖的 `scripts/render_design_svg.py` 产出两张 SVG：
`docs/weave-design-philosophy.svg`（设计哲学）· `docs/weave-global-architecture.svg`（架构）。

## 重要提醒

**归档代码不保证可直接运行**：它们 import 的契约（`Database` / `ConversationLog` /
`Search` / `Executor` / `EnvironmentCatalog` / `ToolRegistry` 等）已从
`weave/core/interfaces.py` 移除，`weave/core/codec.py` 与 `weave/stores/redis_store.py`
也一并在本目录。取回时的正确做法是：

1. 连同该维度的**接口契约**一起取回（需要重新定义，不要再塞进 core 除非它真有多个消费者）；
2. 取回对应的测试，跑通再接入；
3. 如果它要接入 `weave.llm` 对象，先想清楚"是否真的属于 LLM 交互"——
   按当前判据，上下文/执行/编排都**不属于**。

## 参考

- 当前架构：`docs/weave-global-architecture.md`（图：`docs/weave-global-architecture.svg`）
- 设计哲学：`docs/weave-design-philosophy.svg`
- LLM 交互维度规格与验收清单：`docs/weave-llm-dimension.md`
- 从门面文档移出的归档清单 / 迁移映射 / 决策状态：`retired-scope.md`
- 封装空间的评审与触发条件（决定记录，不入 docs）：`encapsulation-review.md`
- 契约面门禁（防止这些能力悄悄长回来）：`tests/v04/test_contracts.py`
