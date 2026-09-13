# Weave 架构图（v0.4 · 原子组件库）

![Weave 架构图](./weave-architecture.svg)

> 原则：**weave 只交付原子件**；组合（循环/上下文/Agent/工具绑定/装配）是调用方代码。
> 本图与 `weave/` 实际代码一一对应；命名零约定（namespace/key 由调用方定义）。
> 相关：`docs/v0.4/01-principles.md`（先验）、`docs/v0.4/02-architecture.md`（契约细节）。

## 0. 一图看全

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 业务 / 消费方                                                                 │
│   14_forum · agora（接力引擎）       其它产品…                                │
│   ├─ adapter/llm.py      → Provider 契约                                      │
│   ├─ adapter/repository.py → StateStore 契约                                  │
│   └─ agora-web/（界面层）                                                     │
│        · file_tools.py      → 实现 ToolRegistry（业务工具：读项目文件）        │
│        · agentic_llm.py     → 组合 Provider+ToolRegistry 的工具循环            │
└───────────────▲──────────────────────────────────────────────────────────────┘
                │ 只依赖原子契约（单向；下层绝不反向 import）
┌───────────────┴──────────────────────────────────────────────────────────────┐
│ 调用方组合（示例，不随包）     13_weave/demos/atomic/                          │
│   context_builder.py（投影上下文） loop.py（翻译循环）                          │
│   tool_registry.py（@tool/推断/线程池） agent.py（极薄容器）                     │
└───────────────▲──────────────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────────────────┐
│ 参考实现（随包，不进兼容承诺）                                                 │
│   weave.stores        InMemory / SQLite(WAL) / Redis(extra)                   │
│   weave.providers     OpenAICompatible(含 DeepSeek 特性) / Fake               │
│   weave.capabilities  TraceSink(EventSink 实现) / CheckpointManager           │
└───────────────▲──────────────────────────────────────────────────────────────┘
                │ 只实现契约，不依赖上层
┌───────────────┴──────────────────────────────────────────────────────────────┐
│ ★ weave.core —— 唯一稳定契约（零第三方依赖）                                   │
│   interfaces: Provider · StateStore(+Searchable/Trimmable/Listable) ·         │
│               ToolRegistry · EventSink(+Noop)                                 │
│   types:      Message/ToolCall/ToolSchema/ToolResult/LLMResponse/             │
│               StreamChunk/SearchHit      errors: 类型化错误树                  │
│   codec:      msg_to_dict / dict_to_msg                                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

**红线（有测试保障）**：`weave.core` 不得 import `capabilities` / `demos` / 任何组合层；
`stores`/`providers`/`capabilities` 只依赖 `weave.core`；业务只依赖契约。

## 1. 依赖方向（Mermaid）

```mermaid
flowchart TB
    subgraph BIZ["业务（外部仓库）"]
        AG["agora 引擎<br/>adapter/llm · adapter/repository"]
        WEB["agora-web 界面层<br/>file_tools · agentic_llm"]
    end
    subgraph COMP["调用方组合（示例）"]
        EX["demos/atomic<br/>context_builder · loop · tool_registry · agent"]
    end
    subgraph IMPL["参考实现（随包）"]
        ST["weave.stores<br/>InMemory / SQLite / Redis"]
        PV["weave.providers<br/>OpenAICompatible / Fake"]
        CP["weave.capabilities<br/>TraceSink / CheckpointManager"]
    end
    subgraph CORE["★ weave.core（唯一契约）"]
        IF["interfaces"]
        TY["types / errors / codec"]
    end

    AG --> IF
    WEB --> IF
    WEB --> PV
    EX --> IF
    EX --> ST
    EX --> PV
    ST --> IF
    PV --> IF
    CP --> IF
    IF --- TY
```

## 2. 原子契约与类型（Mermaid classDiagram）

```mermaid
classDiagram
    class Provider {
        <<abstract>>
        +complete(messages, tools, max_tokens, temperature) LLMResponse
        +stream(messages, tools, ...) AsyncIterator~StreamChunk~
    }
    class StateStore {
        <<abstract>>
        +get(namespace, key)
        +set(namespace, key, value)
        +delete(namespace, key)
        +append(namespace, key, value)
    }
    class SearchableStore {
        <<protocol>>
        +search(query, namespaces, top_k) list~SearchHit~
    }
    class TrimmableStore {
        <<protocol>>
        +trim(namespace, key, max_items)
    }
    class ListableStore {
        <<protocol>>
        +list_namespaces()
        +list_keys(namespace)
    }
    class ToolRegistry {
        <<abstract>>
        +get_schemas() list~ToolSchema~
        +execute(name, arguments)
    }
    class EventSink {
        <<abstract>>
        +emit(event_type, data)
    }
    class LLMResponse {
        content: str
        reasoning: str?  %% 绝不回传
        tool_calls: list~ToolCall~?
        usage/model/finish_reason
    }
    class Message {
        role/content/name/tool_call_id/tool_calls
    }
    Provider ..> LLMResponse : returns
    Provider ..> Message : consumes
    Provider ..> ToolSchema : tools
    ToolRegistry ..> ToolSchema : get_schemas
    StateStore <|.. SearchableStore : 可选能力
    StateStore <|.. TrimmableStore : 可选能力
    StateStore <|.. ListableStore : 可选能力
```

## 3. 一次调用（由调用方组装；weave 不提供 Agent/Loop）

```mermaid
sequenceDiagram
    autonumber
    participant C as 调用方循环（示例 demos/atomic.loop）
    participant CB as 调用方上下文投影
    participant P as Provider（契约）
    participant T as ToolRegistry（契约）
    participant S as StateStore（契约，位置由调用方决定）
    participant E as EventSink（可选）

    C->>E: emit loop_start
    C->>CB: build(agent_id, state, input)
    CB->>S: get(namespace, key)  %% 读历史（若调用方选址）
    CB-->>C: messages=[...]
    C->>P: complete(messages, tools)
    alt 返回 tool_calls
        C->>T: execute(name, args)
        T-->>C: result / ToolError
        C->>S: append(namespace, key, tool_msg)  %% 若调用方选址
        C->>P: complete(messages + tool 消息, tools)   %% 继续（默认不设步数上限，模型自决）
    else 无 tool_calls
        P-->>C: 最终文本
    end
    C->>E: emit loop_end
    C-->>C: 产出（业务自定义结果对象）
```

## 4. Provider 内部：可靠性语义（T2）

```mermaid
flowchart LR
    A[complete_once] --> B{异常类型}
    B -->|429/5xx/网络| R[可重试<br/>指数退避+jitter]
    B -->|401/403/400/422/402| F[不可重试<br/>原样上抛]
    R -->|attempts<max| A
    R -->|耗尽| X[RetryExhaustedError last_error]
    subgraph 超时
      S1[single_timeout 单次调用] --> T1[ProviderTimeoutError]
      S2[overall_timeout 整序列硬上限] --> T2b[ProviderTimeoutError]
    end
    N[stream] --> N1[仅首 chunk 前可重试<br/>产出后错误原样上抛]
```

## 5. 可替换性（同一契约，多实现）

```mermaid
flowchart TB
    subgraph CONTRACT["契约（weave.core）"]
        P0[Provider]
        S0[StateStore + 可选能力]
        T0[ToolRegistry]
        E0[EventSink]
    end
    P0 --> PA[OpenAICompatibleProvider]
    P0 --> PB[FakeProvider]
    P0 --> PX[你的 Provider]
    S0 --> SA[InMemoryStateStore]
    S0 --> SB[SQLiteStateStore]
    S0 --> SC[RedisStateStore extra]
    S0 --> SX[你的 StateStore]
    T0 --> TA[FileReadTools（agora-web）]
    T0 --> TX[你的 ToolRegistry]
    E0 --> EA[TraceSink]
    E0 --> EX[你的 EventSink]
```

## 6. 边界与不变量

| 层 | 内容 | 稳定性 |
|---|---|---|
| 契约 | `weave.core` 接口 + 类型 + 错误 + codec | **进兼容承诺** |
| 参考实现 | stores / providers / capabilities | 不进承诺，可随包演进 |
| 调用方组合 | 循环 / 上下文投影 / Agent 容器 / 工具绑定 | 业务自定，weave 不提供 |

不变量：
1. namespace/key **零约定**（核心视为不透明字符串）；
2. 消息与响应类型化；`reasoning` 承载推理内容且**绝不回传**；
3. 核心零第三方依赖（`pyyaml` 等已随 DX 移除而移除）；
4. 下层不反向依赖上层（测试门禁：`weave.core` 不 import capabilities/demos/组合层）。

## 7. 实证锚点（本仓库真实使用）

- **agora**：只消费 `Provider` 与 `StateStore`（+ `Message`/`ToolSchema`/`ToolError`），
  weave 三次大重构期间 agora **零改动、142 测试全绿**；
- **agora-web**：`FileReadTools` 实现 `ToolRegistry`；`AgenticLLM` 用 weave 原子件组合
  工具循环（过程/结果两段输出、可控读取预算、模型自决停止）；
- **demos/atomic**：循环/上下文/Agent 全部作为"调用方代码"存在，用于说明与测试。
