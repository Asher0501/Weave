# Weave v0.4 — 03 参考实现（recipes）

> 状态：**随包实现（参考实现，不进兼容承诺）**。weave 只附 stores/providers/capabilities；
> 组合层（循环/上下文/Agent/工具绑定）是调用方代码，示例在 `demos/atomic/`。

## 随包参考实现

| 实现 | 模块 | 说明 |
|------|------|------|
| `InMemoryStateStore` | `weave.stores.memory` | 默认/测试；实现 Searchable/Trimmable/Listable（朴素检索） |
| `SQLiteStateStore` | `weave.stores.sqlite` | 单机持久（WAL + synchronous=NORMAL）；行级隔离；可选能力齐全 |
| `RedisStateStore` | `weave.stores.redis_store` | 多进程共享（extra `weave[redis]`） |
| `OpenAICompatibleProvider` | `weave.providers.openai_compat` | openai/deepseek/兼容网关；reasoning 剥离、reasoner 参数过滤、内嵌可靠性 |
| `FakeProvider` | `weave.providers.fake` | 离线确定性（测试/示例） |
| `TraceSink` / `CheckpointManager` | `weave.capabilities` | EventSink 实现 / 基于 ListableStore 的快照-回滚 |

## 调用方组合示例（demos/atomic/，非 weave 交付）

| 示例 | 内容 |
|------|------|
| `context_builder.py` | 业务自建上下文投影（system/历史由调用方定） |
| `loop.py` | 业务自建翻译循环 `run_turn`（调度+工具+可选手选持久化） |
| `tool_registry.py` | 业务自建 ToolRegistry 实现（`@tool`/注解推导/线程池） |
| `agent.py` | 业务自建极薄容器 `DemoAgent`（组合以上示例） |

用法示例（先 `from weave import ...` 只取原子件）：

```python
from weave.core.types import Message
from weave.providers.fake import FakeProvider, FakeTurn
from weave.stores.memory import InMemoryStateStore
from demos.atomic.agent import DemoAgent

agent = DemoAgent(
    FakeProvider(turns=[FakeTurn(content="你好")]),
    InMemoryStateStore(),
    agent_id="demo",
)

def locate(agent_id: str) -> tuple[str, str]:
    return (f"org:{agent_id}:session", "log")   # 业务自选命名

import asyncio
print(asyncio.run(agent.call("1+2 是多少")).output)
```

## 命名零约定（T7）

weave 不定义任何业务命名；历史读写位置由调用方经 `locate_history` 显式注入（上面
`DemoAgent(history_locator=locate)`）；不提供 = 无状态单回合；`writes` 以
`(namespace, key)` 为键。
