# Weave

> **只做一件事：LLM 交互。**
> **一个输入**（`messages` + 可选 `tools`）→ **一个输出**（归一后的 `LLMResponse`）。
> 厂商无关 · 零第三方依赖 · 可离线测试。

## 它做什么

把"调一次模型"这件最容易写错的事做对：

| 内部做的事 | 具体内容 |
|---|---|
| 厂商协议适配 | OpenAI 兼容（OpenAI / DeepSeek / 通义 / Ollama / vLLM / 各类网关）与 **Anthropic 原生**（`/v1/messages`：system 顶层参数、`tool_result` 内容块、流式事件流） |
| 单动作可靠性 | 单次超时 · 指数退避 + 抖动重放 · **尊重厂商 `Retry-After`** · 整序列总预算 · 取消安全 · 8 类失败分类 |
| 流式聚合 | **SSE 跨分片半行缓冲** · 跨分片 UTF-8 增量解码 · `tool_calls` 按 index 归并（id/name/arguments 片段拼接）· 末尾 usage |
| 输出解码归一 | 原生 `tool_calls` / DSML 文本 / JSON 代码块（带围栏） → 统一结构；严格/宽松两种模式 |
| 用量归一 | `prompt_tokens`/`input_tokens` 等各家写法 → `{input, output, total, reasoning?}`；prompt cache 计数（`cache_read`/`cache_write`）也带出来；对象级累计 |
| 可观测性 | **注入的回调**（默认不上报）；对象自己不写日志、不埋点 |

## 它不做什么（故意的）

多轮循环 · 上下文组装与记忆 · 检索召回 · 工具执行 · 编排 · 多 Agent 协调 —— **全部由调用方决定**。
weave 不替应用做这些选择；旧版那套"原子组件库 + 组装策略 + 默认编排"（`ToolRegistry` /
`capabilities` / `strategy` / `logs` / `search` / `executors` / `catalogs`）已整体归档到
[`archive/v0.4-parked/`](archive/v0.4-parked/README.md)（不是删除，需要时可取回）。

## 快速开始

```python
import asyncio
import weave
from weave.core.types import Message

async def main():
    # 默认协议：OpenAI 兼容
    w = weave.llm(model="deepseek-chat", timeout=30, retry=3)
    resp = await w.call([Message(role="user", content="用一句话说明关键路径是什么")])
    print(resp.content, resp.usage, resp.attempts, resp.finish_reason)

    # Anthropic 原生协议（api.anthropic.com，或兼容端点如 https://api.deepseek.com/anthropic）
    w2 = weave.llm(model="claude-sonnet-4-20250514", protocol="anthropic")

    # 需要自己接厂商 / 测试：注入实现 LLMProvider 的哑原子（只有一个方法）
    # w3 = weave.llm(provider=MyProvider())

    # 流式：内部聚合（推荐）
    resp2 = await w.call_streaming(
        [Message(role="user", content="写三点风险")],
        on_chunk=lambda chunk: print(chunk.text, end="", flush=True),
    )

    await w.aclose()

asyncio.run(main())
```

**凭证**：`api_key=` 显式传入，或走环境变量 —— OpenAI 兼容协议用
`WEAVE_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`；Anthropic 协议用
`WEAVE_API_KEY` / `ANTHROPIC_API_KEY`。`base_url=` 可指向任何兼容端点或网关。

**其它入口**：`w.call_streaming(...)` 流式+聚合 · `w.stream(...)` 只要增量自己组装 · `w.aclose()` 释放资源。
装配细节（`headers` / `transport` / `http_timeout` / `param_filter` / `extra_params`）都能从同一个
`weave.llm(...)` 直接传，不必自己 new provider。

## 包结构

| 包 | 内容 |
|---|---|
| `weave.core` | 契约与词汇：**接口** `LLMProvider`（模型）· `StateStore`（KV）；**信封** `CallRequest` / `LLMResponse` / `StreamChunk` / `TypedFailure`；类型化错误 |
| `weave.llm` | **对象**：`weave.llm(...)` → `call` / `call_streaming` / `stream` / `aclose`；内部 `reliability` / `decode` / `usage` / `streaming` |
| `weave.providers` | 哑原子实现：`OpenAIHTTPProvider` · `AnthropicHTTPProvider` · `FakeProvider`（离线）· 可注入 `Transport`（stdlib 实现）· SSE 解析 |
| `weave.stores` | KV 存储：`SQLiteStateStore`（WAL）· `InMemoryStateStore` |

分工铁律：**原子是哑的**（一次调用，失败即抛，不重试不计时不解码）；可靠性、解码、计量、上报都在对象层。

## 契约与门禁

- **契约面只有两个名字**（`LLMProvider` / `StateStore`），公共导出只有
  `llm` / `coerce_provider` / `LLMClient` / `LLMCallError`；
- `tests/v04/test_contracts.py` 机械执行：接口面不得膨胀、已归档的接口与模块**必须真的不存在**、
  原子层不得依赖上层、`weave.llm` 不得碰存储、原子层不得出现 `Invocation`（解码只在对象里）、`core` 零第三方依赖；
- **替换测试**：换 provider（哑 HTTP ↔ `FakeProvider`）→ 对象层与调用方零改动。

## 测试

```bash
python -m pytest tests/v04 -q      # 全离线：注入假 transport，不联网、不需要 API key
```

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/weave-llm-dimension.md`](docs/weave-llm-dimension.md) | 本维度**规格与逐条验收清单**（完备清单 = 验收标准） |
| [`docs/weave-global-architecture.md`](docs/weave-global-architecture.md) | 全局架构、契约与词汇的位置、**决策状态表**（哪些仍然成立 / 已归档 / 不再实现） |
| `docs/weave-global-architecture.{png,svg,mmd}` | 架构图（渲染图 / 矢量 / mermaid 源码） |
| [`archive/v0.4-parked/README.md`](archive/v0.4-parked/README.md) | 归档了什么、为什么、**怎么取回** |
| `docs/v0.4/` | **历史**（"原子组件库"时期的设计基线与 agora 迁移报告），已被上面两份取代 |

## 消费者

| 项目 | 用法 |
|---|---|
| `../12_bePm` | LLM 层走 weave（`backend/engine/weave_provider.py`，默认 `LLM_SDK_TYPE=weave`），不再需要 anthropic/openai SDK |
| `../14_forum`（agora） | 用 weave 的 KV 存储；其 LLM 适配器仍指向**已归档**的旧 provider，待迁移 |

## License

[MIT](LICENSE) © Weave Contributors
