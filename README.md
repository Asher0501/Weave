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
| 厂商独有内容块 | `content` 除 `str` 外可直接给 `dict` / `list[dict]`，**原样逐字透传、不解释 key**（多模态、`cache_control`、厂商新块都不必改 weave）；响应侧保留原始块 `raw_blocks`，thinking 块可无损回填下一轮 |
| 可观测性 | **注入的回调**（默认不上报）；对象自己不写日志、不埋点 |

## 它不做什么（故意的）

多轮循环 · 上下文组装与记忆 · 检索召回 · 工具执行 · 编排 · 多 Agent 协调 —— **全部由调用方决定**。
判据一句话：**需要跨多次交互才成立的，都不在本维度**。weave 不替应用做这些选择。

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

**厂商独有的内容块**：`Message.content` 除了 `str`，还可以直接给 `dict` / `list[dict]` ——
weave **不解释任何 key**，逐字写进该角色的内容槽位。厂商特有能力都走这一个口子，不必改 weave：

```python
from weave.core.types import Message

# 1) 图片（内容块形状按当前厂商给）
resp = await w.call([Message(role="user", content=[
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
])])

# 2) 长 system 上打缓存断点（Anthropic：system 变成块数组）
w2 = weave.llm(model="claude-sonnet-4-20250514", protocol="anthropic")
messages = [
    Message(role="system", content=[
        {"type": "text", "text": long_prompt, "cache_control": {"type": "ephemeral"}},
    ]),
    Message(role="user", content="问题"),
]

# 3) 响应侧保留原始块 → 原样回填，thinking + tool_use 才能无损续接
resp = await w2.call(messages, tools=tools)
messages.append(Message(role="assistant", content=resp.raw_blocks))
```

代价必须知道：原样块是**厂商形状**，只对当前厂商有效、**换 provider 不可移植**（`str` 形态才跨厂商）。
另外原样 `content` 与 `tool_calls` 同时给出是歧义，会在发出请求**之前**直接 `TypeError`，不会被当成厂商失败重放。

**适配层会替你看着厂商**：配置里选定的协议决定哪些块形状合法，写错家会在发请求前报错，并给出当前厂商的写法：

```python
w = weave.llm(model="deepseek-chat")           # → OpenAI 兼容适配器
await w.call([Message(role="user", content=[{"type": "image", "source": {...}}])])
# TypeError: Message.content 里出现了别家形状的块 'image'，但当前适配器是 openai
#            （配置来自 weave.llm(protocol=...)）。openai 的写法示例：
#            {"type":"image_url","image_url":{"url": …}}。换成本厂商的块形状，
#            或把 protocol 改成对应协议；若该网关确实接受混合形状，可传 shape_check=False。
```

只拦**别家已知形状**，未知块一律放行（不挡厂商以后新增的类型）；网关确实吃混合形状时传 `shape_check=False` 关掉。
一个 `weave.llm(...)` 对象对应一家厂商——要同时用两家就建两个对象。

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
| [`docs/weave-design-philosophy.svg`](docs/weave-design-philosophy.svg) | **设计哲学图**：先验 · 判决规则 · 哑原子/聪明对象 · core 是词表 · 两种内容形态 · 不许静默 |
| [`docs/weave-global-architecture.svg`](docs/weave-global-architecture.svg) | **架构图**：应用 → 对象 → 厂商适配层，右侧是 ②③ 共用的词表 |
| [`docs/weave-global-architecture.md`](docs/weave-global-architecture.md) | 架构说明 · 对外 API · 不变量与门禁 |
| [`docs/weave-llm-dimension.md`](docs/weave-llm-dimension.md) | 本维度**规格与逐条验收清单**（完备清单 = 验收标准）+ 真实端点实测记录 |

图由 `python scripts/render_design_svg.py` 生成（零依赖手写 SVG，自带排版自校验）。

## 消费者

| 项目 | 用法 |
|---|---|
| `../12_bePm` | LLM 层走 weave（`backend/engine/weave_provider.py`，默认 `LLM_SDK_TYPE=weave`），不再需要 anthropic/openai SDK |

## License

[MIT](LICENSE) © Weave Contributors
