"""Weave —— 只做一件事：LLM 交互。

对外**一个输入（messages + 可选 tools）、一个输出（归一后的 LLMResponse）**：

```python
import weave

w = weave.llm(model="deepseek-chat", timeout=30, retry=3)   # provider 由 weave 装配
# 需要自己接厂商时再注入：weave.llm(provider=MyProvider())
resp = await w.call(messages, tools=[...])
resp.content, resp.tool_calls, resp.usage
```

它内部把四件事做对：**可靠性**（超时/重放/退避/限流/错误分类）、
**解码归一**（原生 tool_calls / DSML / JSON）、**计量归一**、**可观测性回调**。
完备清单见 `docs/weave-llm-dimension.md`。

包内一共四层（见 docs/weave-global-architecture.md）：

- `weave.core`      契约与词汇：`LLMProvider`（唯一模型接口）· `StateStore`（KV）·
                    信封（CallRequest / LLMResponse / StreamChunk / TypedFailure）
- `weave.llm`       对象：`weave.llm(...)`，内部 reliability / decode / usage / streaming
- `weave.providers` 哑原子实现：`OpenAIHTTPProvider`（零第三方依赖）· `FakeProvider`
- `weave.stores`    KV 存储（`SQLiteStateStore` / `InMemoryStateStore`）

**上下文从哪来、要不要执行工具、循环几轮——都是应用的自由**（weave 不参与）。
其它能力（会话日志 / 检索 / 执行器 / 环境目录 / 组装策略 / 编排 / checkpoint /
旧 provider 与适配器）已整体归档到 `archive/v0.4-parked/`，需要时再从那里取回。
"""

from weave.llm import LLMClient, LLMCallError, coerce_provider, llm

__version__ = "0.5.0"

__all__ = ["llm", "coerce_provider", "LLMClient", "LLMCallError", "__version__"]
