# Weave v0.4 — 04 能力层与钩子（capability）

> 状态：**最小外壳已实现**；server/scheduled/features 事件集与完整形态见 §4 待办。
> 前提：T4（可观测=钩子注入）、I2（核心不依赖能力）。本层可整体替换/移除而不影响核心。

## 1. EventSink 事件集（暂定，随本文件定稿）

事件由**调用方自己的循环**在关键点 emit（示例 `demos/atomic/loop.py::run_turn` 已按下列事件上报）：

| 事件 | data（示意） | 时机 |
|------|-------------|------|
| `loop_start` | {agent_id, input} | 回合开始 |
| `iteration_start` | {iteration} | 每次模型调用前 |
| `llm` | {model, finish_reason, tool_calls} | 每次模型返回后 |
| `tool` | {name, ok, error} | 每个工具执行后 |
| `loop_end` | {output, iterations} | 回合结束 |

注入方式：示例容器 `demos/atomic/agent.py::DemoAgent.call(q, sink=...)` 把 sink 传给你的循环。
默认 `NoopEventSink`（weave.core.interfaces），零开销。

## 2. 参考实现（weave.capabilities）

| 模块 | 类 | 能力 |
|------|----|------|
| `trace.py` | `TraceSink` | 内存事件收集（TraceEvent / by_type / summary） |
| `checkpoint.py` | `CheckpointManager` | 命名空间快照/回滚（依赖 `ListableStore`） |

## 3. 与存储可选能力的协作

- checkpoint 需要 store 实现 `ListableStore`（InMemory/SQLite/Redis 已实现）；
- observability 未来可把事件序列化进独立命名空间（能力层自选，不进核心）。

## 4. 待办（能力层完整形态）

- [ ] Server（REST/WS）：在"调用方循环 + `EventSink`"之上做事件桥接（v0.3 的 EventBus/WS 模式可复用为调用方实现）；
- [ ] scheduled 常驻循环：调用方"定时触发 + 自己的循环"的包装示例；
- [ ] observability 落盘（JSONL）与 trace 树聚合。
- 注：structured_call / two_stage / prompt_defense 等业务半成品按「业务归用户」原则不进入 weave；如需要，作为调用方代码。
