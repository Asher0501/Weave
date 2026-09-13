# Issue #002: 事件总线 (In-Process Pub/Sub)

## 结论

| 维度 | 决策 |
|------|------|
| **入 Weave** | ✅ 是，作为基础设施 |
| **类型** | 必选基础功能（非 Feature 开关） |
| **范围** | 进程内 async pub/sub，Weave Server 负责桥接到 SSE/WebSocket |

## 是什么

一个进程内的事件通知机制。Weave 在 Loop 执行过程中 emit 事件，订阅方通过 `weave.on(event_name)` 注册并消费。

```
Weave 内部 → emit("事件名", data) → asyncio.Queue → 订阅方 await 取走
```

不是中断——不打断生产者。不是提醒——消费者自己挂着等。就是**标准化的生产队列 + 订阅接口**。

## 实现原理

```python
# 内部结构：事件名 → 订阅者队列列表
_subscribers: dict[str, list[asyncio.Queue]] = {}

# 生产者（Weave 内部调用）
async def emit(event_name: str, data: dict):
    for queue in self._subscribers.get(event_name, []):
        await queue.put(data)   # 阻塞式，防止背压丢消息

# 消费者（外部调用）
async def on(event_name: str):
    queue = asyncio.Queue()
    self._subscribers[event_name].append(queue)
    try:
        while True:
            event = await queue.get()
            yield event
    finally:
        self._subscribers[event_name].remove(queue)   # 自动清理
```

## 背压策略

- **阻塞生产者**：`await queue.put()`（默认无界队列，不会满；可配置 `maxsize` 启用有界模式）
- 不静默丢消息（bePM 的 `put_nowait` 做法）

## 使用示例

```python
weave = Weave("config.yaml")

# 订阅
async with weave.on("tool_called") as stream:
    async for event in stream:
        print(f"Tool called: {event['name']}")

# Weave 内置事件类型
# llm_token      — LLM 每输出一个 token
# tool_call      — 准备调用 tool
# tool_result    — tool 调用完成
# iteration      — Loop 每一次迭代开始/结束
# run_complete   — 整个 run 完成
# error          — 任何错误
```

## 参考来源

- myKG `stream_logger.py:82-104` — asyncio.Queue + async generator，但无清理、满即丢
- bePM `ws.py:13-75` — WebSocket rooms，但单例全局、无背压

## 讨论记录

2026-07-29, Asher & Claude.

### Q: 相当于中断信号？
不是。中断"打断当前执行"，事件总线是"门口贴通知"，消费者自己决定什么时候看。

### Q: 怎么保证别的项目都能听到？
同一 Python 进程内通过 asyncio.Queue 的内存传递。跨进程（前端浏览器）由 Weave Server 桥接到 SSE/WebSocket。

### Q: 是生产者生产队列并提供提醒？
没有提醒机制。消费者 `await queue.get()` 自己蹲着等，队列有东西就醒来拿走。

### Q: 背压是什么？
生产者快于消费者导致队列满。Weave 采用阻塞生产者策略，不丢消息。
