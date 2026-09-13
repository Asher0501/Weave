# Issue #001: Async vs Sync 接口设计决策

## 结论

| 维度 | 决策 |
|------|------|
| **内部实现** | Async（`await` 驱动的事件循环） |
| **对外 Sync 入口** | `weave.run()` — `asyncio.run(_run_impl)` |
| **对外 Async 入口** | `weave.arun()` — `await _run_impl` |
| **接口风格** | 统一。参数、返回值完全一致，区别仅 `await` |

## 原理

### `await` 是什么

- Python 异步（asyncio）的核心语法，告诉事件循环："我要等这个 I/O 结果，你先去处理别的协程"
- 同一协程内 `await` 保证串行执行：`b = await fun()` 之后 `b` 一定已计算完毕
- `await` 让出的是事件循环调度权，不涉及多线程

### 什么是协程不是线程

| | 线程 (Thread) | 协程 (Coroutine) |
|---|---|---|
| 调度者 | 操作系统 | 事件循环（你自己） |
| 切换开销 | 有（上下文切换） | 几乎无 |
| 能否并行 | 能（多核） | 不能（单线程） |
| 适用场景 | CPU 密集 | I/O 密集 |

### `await` 何时有效、何时无效

```
await some_coro()
  │
  └─→ 执行 coro 内部
        ├─→ await asyncio.sleep(1)   ← 暂停！交出控制权 ✓
        ├─→ await httpx.post(...)    ← 暂停！交出控制权 ✓
        └─→ for i in range(10M): ... ← 不暂停！一直跑到下一个 await ✗
```

- **有效**：I/O 等待（网络、磁盘、数据库）
- **无效**：纯 CPU 计算（需用 `asyncio.to_thread()` 丢线程池）

### Weave 适合 async 的原因

- LLM 调用 = 网络 I/O（2~10 秒等待）
- Tool 执行 = 查询 DB / 调 API（I/O）
- Memory 读写 = 磁盘 I/O
- 不做本地模型推理，不做 CPU 密集计算

## 接口示例

```python
from weave_agent_sdk import Weave

weave = Weave("weave.yaml")

# Sync 入口（普通脚本）
result = weave.run("我应该学什么?")

# Async 入口（FastAPI 路由 / asyncio 环境）
result = await weave.arun("我应该学什么?")
```

两者参数一致、返回值一致。唯一区别是调用方加不加 `await`。

## 讨论记录

2026-07-29, Asher & Claude.

### Q: sync 和 async 有什么区别？

sync 调用阻塞等待，CPU 闲置。async 调用在等待 I/O 时让出控制权给事件循环调度其他协程，不浪费 CPU。

### Q: `await` 的代码段里不能出现互相依赖的内容？

相反。`await` 保证当前协程内的串行顺序——`b = await fun()` 之后的代码一定等 `b` 算完才执行。`await` 让出的是事件循环调度权，不是让当前协程并行。

### Q: `await` 是让出 CPU 去执行别的线程？

不是线程，是协程。整个过程在一个线程内，事件循环调度同一线程内的多个协程轮流执行。切换几乎无开销。

### Q: `await` 的函数如果需要计算而不是 I/O 呢？

纯 CPU 计算不能被 await 中断，会一直算完才交出控制权，期间阻塞事件循环。这种场景需要用 `asyncio.to_thread()` 丢到线程池。但 Weave 不涉及 CPU 密集任务。
