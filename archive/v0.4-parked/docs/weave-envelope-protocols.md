# Weave 接口间的共享类型（信封）与标准协议

> **状态（重要）**：本文描述的是**未来维度**（上下文管理 / 执行）的接口协议草案，
> 属于 parked 素材，**不是 weave 当前契约的一部分**。
> weave 当前只做一个维度：**LLM 交互**（对象 `weave.llm(...)`，一个输入一个输出），
> 见 `docs/weave-llm-dimension.md` 与 `docs/weave-global-architecture.md`。
> 按最新方向：这些接口**先不动**，等各自维度真正要落地时再接线。

> 术语平实化：**信封（envelope）** = 接口/单元之间递数据用的公共数据结构。它是允许存在的共享点；
> 信封之外，各方各自独立（这就是"正交"的边界：共享被压缩到少数冻结信封上）。

## 1. 参与方（节点）

原子接口（契约层）：
- `P` = LLM Provider　`D` = Database　`Pa` = Parser　`Ex` = Executor（执行域）
- `EC` = EnvironmentCatalog（**执行域内的环境描述接口**；D-T1：独立接口 + 默认反射实现 `ExecutorReflectionCatalog`）
- `CL` = ConversationLog（**上下文管理域内的追加日志接口**；D-T2：独立可选接口 + 默认实现 `SqliteConversationLog`）
- `SR` = Search 检索（**上下文管理域内的检索接口**；D-T3：只承诺「问题 → 相关记录」，编码/重排零件不入契约）

业务单元 / 编排（策略与流程层）：
- `S` = 会话管理　`A` = 上下文组装策略（含记忆读写）　`E` = 解析执行（Pa+Ex 的业务侧）　`O` = 编排（`weave.run()` 默认配方）

## 2. 有向图（仅画存在的边）

```
O ──Recall/Remember(Scope)──► A          A ──CRUD op(ns,key,·)──► D
O ◄────────Context──────────── A          D ──────────Record[]──► A
O ──CallRequest──────────────► S          S ─────CallRequest────► P
O ◄────────CallResult───────── S          P ──LLMResponse/chunk─► S
O ──DecodeRequest────────────► E          E ─────Invocation────► Ex →（外部环境）
O ◄────────Observation[]────── E          EC ────ToolSchema[]───► S → P（跨域，经 S）
                                          A ──append/tail(Scope)─► CL
                                          CL ─────────Record[]──► A
                                          A ─search(Scope,query,k)► SR
                                          SR ─────────Record[]──► A
                                          
禁止（越层/职责重叠）：S→D、S→CL、S→SR、A→P、S→A、P→D、Pa→D、Ex→D、EC→D、Ex→P、O→CL、O→SR
（schema 只经 EC→S→P；历史只经 A↔CL；检索只经 A↔SR；编排永不直连任何存储/检索）
```

## 3. 邻接矩阵（行=源，列=目标；空=无传递）

| 源＼目标 | P | D | Pa | Ex | EC | CL | SR | S | A | E | O |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **P** Provider | — | ✗ | — | — | — | — | — | `LLMResponse` / `StreamChunk` / 原生错误 | — | — | — |
| **D** Database | — | — | — | — | — | — | — | — | `Record[]` | — | — |
| **Pa** Parser | — | ✗ | — | `Invocation` | — | — | — | — | — | — | — |
| **Ex** Executor | ✗ | ✗ | — | — | — | — | — | — | — | — | — |
| **EC** EnvironmentCatalog | — | ✗ | — | — | — | — | — | `ToolSchema[]` | — | — | — |
| **CL** ConversationLog | — | — | — | — | — | — | — | — | `Record[]` | — | — |
| **SR** Search 检索 | — | — | — | — | — | — | — | — | `Record[]` | — | — |
| **S** 会话管理 | `CallRequest` | ✗ | — | — | — | ✗ | ✗ | — | ✗ | — | `CallResult` |
| **A** 组装策略 | ✗ | `CRUD op(ns,key,·)` | — | — | — | `append/tail(Scope)` | `search(Scope,query,k)` | ✗ | — | — | `Context` |
| **E** 解析执行 | — | ✗ | — | `execute(Invocation)` | — | — | — | — | — | — | `Observation[]` |
| **O** 编排 | — | — | — | — | — | ✗ | ✗ | `CallRequest` | `Recall/RememberRequest(Scope)` | `DecodeRequest` | — |

备注（图上不画，属实现内部）：`SR` 的实现内部可能用到编码/重排这类模型零件——它们由**装配层注入**，不是接口之间的传递，因此不构成边；同理 `CL` 可以被 `SR` 的实现作为索引来源注入使用。

图例：`✗` = 明文禁止（编码我们已确认的边界：会话管理不碰库、组装策略不调模型、任何原子都不碰库以外的东西）。

## 4. 每个存在的元素 · 标准协议

### 4.1 冻结基类型（改动 = 基变换，双方必须协同）

| 元素 | 语义 | 最小字段 | 不变量 | 错误通道 | 归属（谁定义/谁可改） |
|---|---|---|---|---|---|
| **`Message`** | 对话消息 | `role`, `content`, `name?`, `tool_call_id?`, `tool_calls?` | `role ∈ {system,user,assistant,tool}`；`tool` 必带 `tool_call_id`；`assistant.tool_calls` 与后续 `tool` 消息成对 | 不承载错误（错误走 TypedFailure） | 核心（跨域共享：LLM 域 ↔ 上下文域） |
| **`ToolSchema`** | 环境能力描述 | `name`, `description`, `parameters`(JSON Schema) | `name` 唯一；`parameters` 是合法 JSON Schema object | — | **执行域产出**（`EnvironmentCatalog.describe`），LLM 域消费 |
| **`Invocation`** | 一次要执行的动作 | `name`, `arguments`, `id?`, `raw?` | `arguments` 可 JSON 序列化；`name` 必须在 `ToolSchema` 名单内（或显式标记未知）；**只能由 `Pa` 产出**（唯一解码者，D13） | — | **执行域内**（Parser 产出 / Executor 消费） |
| **`TypedFailure`** | 类型化失败 | `kind`, `retryable`, `message`, `origin`, `detail?` | `kind ∈ {rate_limit, server, network, auth, bad_request, rejected, timeout, tool_error, parse_error, catalog_error, storage_error}`；`retryable` 与 `kind` 一致 | 本身就是错误通道 | 核心（所有层共用同一种失败信封） |

### 4.2 单元间信封

| 元素 | 语义 | 最小字段 | 不变量 | 归属 |
|---|---|---|---|---|
| **`Scope`** | 记忆寻址（命名约定在此，核心零约定） | `namespace`（可由模板派生）、`key`、`params?` | 由策略解释；核心视其为不透明字符串 | 组装策略 |
| **`Record`** | 一条记忆记录 | `kind ∈ {message, observation, summary}`、`payload`、`created_at`、`meta?` | 可 JSON 序列化；有序由 `created_at` + 追加序保证；每条有稳定去重键（写回幂等的前提） | 组装策略（`D`/`CL` 只搬运，不解释） |
| **`RecallRequest`** | 召回请求 | `scope`, `n`, `mode ∈ {tail, search, hybrid}`, `query?` | `mode=search` 时必须给 `query` | 编排 → 组装策略 |
| **`RememberRequest`** | 写回请求 | `scope`, `records: [Record]`, `ttl?` | 写回幂等（同一 `records` 重复写不产生重复条目；由 `id`/去重键保证） | 编排 → 组装策略 |
| **`Context`** | 拼接产物（模型看到的东西） | `messages: [Message]`, `meta{token_budget?, sources?}` | 第一条可为 system；`tool` 消息配对完整；顺序即模型所见顺序 | 组装策略 → 编排 |
| **`CallRequest`** | 单次动作请求 | `messages: [Message]`, `schemas: [ToolSchema]`（引用）, `opts{max_tokens?, temperature?, stream?}` | `schemas` 只引用环境目录，不得就地改写 | 编排 → 会话管理 → Provider |
| **`LLMResponse`** | 单次动作结果 | `content`, `tool_calls?`（厂商 native，**未解码**）, `reasoning?`, `usage`, `finish_reason`, `model` | `reasoning` 永不回传；`tool_calls` 保持厂商原样（解码是 Parser 的事） | Provider → 会话管理 |
| **`StreamChunk`** | 流式片段 | `kind ∈ {token, reasoning, tool_call_delta, finish}`, `text?`, `delta?` | 顺序即到达顺序；聚合由会话管理负责 | Provider → 会话管理 |
| **`CallResult`** | 单次动作的成败 | `response?: LLMResponse` 或 `failure?: TypedFailure`；`attempts`, `elapsed_ms` | 二者恰有其一 | 会话管理 → 编排 |
| **`Observation`** | 一次执行的结果 | `invocation`(或 `id`), `ok`, `value?`, `failure?`, `elapsed_ms` | `ok=false` 时必有 `failure`；**失败也要回填**（让模型自救） | 解析执行 → 编排 |

### 4.3 接口协议（非数据类型，但同样需要冻结）

| 接口 | 方法 | 不变量 | 失败 | 归属 |
|---|---|---|---|---|
| **`EnvironmentCatalog`（EC）** | `async describe() -> list[ToolSchema]` | 只读、**不得执行任何动作**；`name` 全局唯一；结果可缓存（同一环境内稳定）；不得依赖调用者上下文 | `TypedFailure(kind=catalog_error, retryable=false)` | **执行域**（D-T1：独立接口） |
| `ExecutorReflectionCatalog`（默认实现） | 同上 | 从已注册的 executor/handler 反射生成清单 | 同上 | 执行域参考实现 |
| **`ConversationLog`（CL）** | `async append(scope: Scope, records: list[Record]) -> None`<br>`async tail(scope: Scope, n: int) -> list[Record]` | 追加语义：**只增不改**；`append` 按去重键幂等（同批重复写不产生重复条目）；`tail` 返回"最后 n 条、按时间升序"，`n<=0` 视为不限；单次追加的成本不得随历史长度增长（**禁止整段重写**）；`scope` 对实现透明（实现不得解释命名约定） | `TypedFailure(kind=storage_error)`；`scope` 不存在时 `tail` 返回空列表（**不是错误**） | **上下文管理域**（D-T2：独立可选接口） |
| `SqliteConversationLog`（默认实现） | 同上 | 一条记录一行 + 索引；与 `SQLiteDatabase` 可共库不同表 | 同上 | 上下文管理域参考实现 |
| **`Search`（SR 检索）** | `async search(scope: Scope, query: str, k: int) -> list[Record]` | 只承诺**「问题 → 相关记录」**；`k` 为上限（可实现返回更少）；结果按相关性由实现排序；`scope` 透明（不解释命名约定）；**不得执行动作、不得改写记录**；编码/重排等**零件不是契约**（实现内部可注入，见 D-T3） | `TypedFailure(kind=storage_error)`；无命中返回空列表（不是错误） | **上下文管理域**（D-T3：只留"检索"这一格，零件不入契约） |
| `SearchableStore`（v0.4 形态） | 同上（`search(ns, query)`） | 已有的关键词/LIKE 实现即最小落地形态 | 同上 | 上下文管理域参考实现 |
| `Embedder` / `Reranker`（**零件，不进契约**） | `embed(texts) -> vectors` / `rerank(query, docs, top_n)` | 属 **LLM 能力域的域内能力**，由装配层**注入**给检索实现；策略层不可见、不写进替换测试门面 | 复用 `TypedFailure`（rate_limit / auth / timeout …） | LLM 能力域（**待真实症状出现再决定是否提升为接口**） |

### 4.4 协议级规则（对所有元素生效）

1. **单一归属**：每个元素只有一个"定义者"；其他方只消费/引用，不就地改写。
2. **冻结与基变换**：4.1 的元素一旦要改，视为基变换 → 必须同步改所有消费者 + 替换测试全绿。
3. **失败不抛穿分层**：原子层以内的错误在跨层时一律转成 `TypedFailure`（会话管理负责分类，执行侧负责 `tool_error`/`parse_error`）。
4. **引用而非拷贝**：`ToolSchema` 由执行域产出一份，`S`/`O` 只传引用。
5. **禁止边**：邻接矩阵中标 `✗` 的方向不得出现（CI 门禁：反向依赖 + 越层调用扫描）。
6. **唯一解码者**：厂商输出里的工具调用语法（native tool_calls / DSML / JSON 回退）**只在 `Pa` 解码**；Provider 不得回退解析（D13）。

## 5. 归属待定 · 逐条讨论（一次一条）

- [x] **T1**：`EnvironmentCatalog.describe()` —— **决策：执行域内的独立接口**（D-T1），并提供默认实现 `ExecutorReflectionCatalog`（从已注册 executor 反射生成清单）。理由：变化轴独立（"有哪些能力" ≠ "怎么执行"）、可分别替换、未来接远端目录（MCP/HTTP）零改造；默认反射实现让常规用法零负担。
- [x] **T2**：`ConversationLog`（append/tail）—— **决策：在"上下文管理"域加独立可选接口**（D-T2），默认实现 `SqliteConversationLog`；`Database` 保持通用 KV 角色不变。理由：追加式增长与 KV 整体覆盖语义不匹配（当前 `SQLiteStateStore.append` 是"读整行 → 改列表 → 写整行"，第 n 次追加重写 n 条，累计 O(N²)）；提升为契约后性能与并发语义可替换、策略层代码不动。可选性为真：只实现 `Database` 的无状态模式仍照常工作（替换测试保留此用例）。
- [x] **T3**：`Embedding/Rerank` —— **决策：不把它们作为原子接口**（D-T3）。层层拆开后：① 索引/存储、② 检索、③ 编码、④ 重排、⑤ 拼装本是**一条跨域流水线**，不是单一能力；其中 **②检索归上下文管理域**（契约里只留这一格：`search(scope, query, k) → [Record]`，v0.4 已有 `SearchableStore` 作为最小落地形态），③④ 是**这一格内部的零件**，属 LLM 能力域，由装配层注入给检索实现，**不进契约、不进替换测试门面**，策略层看不见。理由：主流向量库普遍自带编码（零件不是对外契约）；同时保住"模型调用不分散、只有一套凭证与可靠性"。**提升为接口的触发条件（出现任一再说）**：关键词/LIKE 完全搜不到（词汇不匹配）**且**素材量超过能手工挑的量；或召回候选里前 k 条总是不对（那时才需要重排）。
- 归属待定：**已全部收口（T1/T2/T3）**。后续新增元素按同一格式追加，逐条讨论。

（讨论结论将回填到本文件与全局架构图。）
