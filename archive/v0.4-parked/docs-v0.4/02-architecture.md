# Weave v0.4 — 02 核心原子契约（architecture）

> 状态：**与 weave/ 代码同步**。前提：`docs/v0.4/01-principles.md`（含修订注 R-2026）。
> 范围：weave 只交付原子件；组合（循环/上下文/Agent/工具绑定/DX）见 `demos/atomic/`（调用方示例，非 weave 契约）。

## 1. 包布局（原子交付面）

```
weave/
├── __init__.py        # 仅 __version__
├── core/              # ★唯一稳定契约层（原子）
│   ├── types.py       #   Message/ToolCall/ToolSchema/ToolResult/LLMResponse/
│   │                  #   StreamChunk/SearchHit
│   ├── errors.py      #   类型化错误树 + classify_http_error
│   ├── interfaces.py  #   Provider/StateStore/SearchableStore/TrimmableStore/
│   │                  #   ListableStore/ToolRegistry/EventSink
│   └── codec.py       #   msg_to_dict/dict_to_msg（消息序列化工具）
├── stores/            # StateStore 参考实现：memory.py / sqlite.py / redis_store.py
├── providers/         # Provider 参考实现：openai_compat.py / fake.py / _reliability.py
└── capabilities/      # 可选工具：trace.py（EventSink 实现）/ checkpoint.py

demos/atomic/          # 调用方组合示例（非 weave 包）：context_builder / loop /
                       # tool_registry / agent —— weave 不提供这些
```

## 2. 原子接口（签名即契约）

| 接口 | 方法 | 要点 |
|------|------|------|
| `Provider` | `async complete(messages, tools=None, *, max_tokens=4096, temperature=0.7, **extra) -> LLMResponse`；`async stream(...) -> AsyncIterator[StreamChunk]` | 内嵌可靠性（T2）；类型化消息/响应；schema 中立输入、厂商格式在 Provider 侧转换（T1.1） |
| `StateStore` | `async get/set/delete(namespace,key)`；`async append(namespace,key,value)` | namespace 为**不透明字符串、零命名约定**（T7）；值 JSON 可序列化；append 维护有序列表 |
| `ToolRegistry` | `get_schemas() -> list[ToolSchema]`；`async execute(name, arguments) -> Any` | 原子接口组（T1）；失败抛 `ToolError` |
| `EventSink` | `async emit(event_type, data=None)` | 最小钩子（T4）；默认 `NoopEventSink`；由调用方循环/能力实现上报 |

可选能力（Protocol，`@runtime_checkable`，核心不依赖）：

| 能力 | 方法 | 用途 |
|------|------|------|
| `SearchableStore` | `search(query, *, namespaces=None, top_k=5, **kw) -> list[SearchHit]` | 检索（后端自愿实现） |
| `TrimmableStore` | `trim(namespace, key, max_items)` | 预算控制 |
| `ListableStore` | `list_namespaces()` / `list_keys(namespace)` | checkpoint/迁移/审计 |

## 3. 数据与可靠性要点

- 消息/响应全类型化；工具 schema 用厂商中立 `ToolSchema`；
- `LLMResponse.reasoning` 承载推理内容——消息模型无该字段，回传天然不含（T2）；
- 可靠性语义（T2）：429/5xx/网络可重试，401/403/400/422/402 业务错直抛；`RetryPolicy`
  （max_attempts/backoff/jitter/single_timeout/overall_timeout）；流式仅首 chunk 前可重试；
  耗尽抛 `RetryExhaustedError`。见 `weave/providers/_reliability.py`。

## 4. 明确不在此层的东西

循环、上下文投影、Agent 容器、工具绑定、配置装配、CLI——统统**不在 weave**；
它们是用本层原子件写出来的调用方业务代码（示例与端到端测试见 `demos/atomic/`、`tests/v04/`）。
