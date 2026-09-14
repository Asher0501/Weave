# Weave

> **只做一件事：LLM 交互。**
> **一个输入**（`messages` + 可选 `tools`）→ **一个输出**（归一后的 `LLMResponse`）。
> 厂商无关 · 零第三方依赖 · 可离线测试。

## 它屏蔽了什么

**weave 屏蔽的是「同一件事在不同厂商的不同说法」和「偶发的传输/协议故障」；不屏蔽事实与业务决定。**

| 层 | 屏蔽掉的具体细节 | 为什么自己写容易错 |
|---|---|---|
| **传输与认证** | URL/path 结构 · `Authorization: Bearer` vs `x-api-key`+`anthropic-version` · `Accept` 头 · key 的查找链 · `urllib` 异常 | 每家一套，异常类型也各家不同 |
| **请求形状** | system 是消息还是顶层参数 · 工具结果的两种编码（`tool_call_id` vs 必须紧邻 `tool_use` 的 `tool_result` 块，相邻还要合并）· `parameters` vs `input_schema` · `stop`→`stop_sequences` · Anthropic 必填 `max_tokens` · 10 个 OpenAI 专有参数与 reasoner 的采样参数 | 全是"踩过才知道"的清单，漏一条就 400 |
| **响应与归一** | 停因两套词表 · 内容块数组拍平成文本 · 参数是对象还是 JSON 字符串 · usage 十几种字段名 → 6 个键（**缺就不给，不补 0**）· 两种错误体嵌套 → 8 类失败 | 想算成本、做预算、做重放，都得先统一口径 |
| **流式**（最重） | `data:` 跨 TCP 分片 · 汉字跨分片的 UTF-8 边界 · `[DONE]`/空行/非 JSON 行 · `tool_calls` 片段按 index 归并 · 稀疏块索引→密集 index · 推理增量两种叫法 · usage 出现在首/尾事件 · **流断了要不要重放** | **本地永远不错，线上偶发**——分片边界根本测不出来 |
| **可靠性** | 哪 4 类能重放、哪 4 类绝不 · 退避 = 指数 + 抖动 · `Retry-After` 解析并封顶 · 总预算 · 单次超时 · 取消语义 · 异常→分类 | 退避写错会把刚恢复的服务再打挂，或让总时长失控 |
| **解码** | 原生 `tool_calls` / DSML / 围栏 JSON 三来源探测 · 参数字符串 → dict · 解不出时 strict/lenient 的两种表现 | 模型不按格式走时没有兜底，就是"偶发解析失败" |

### 具体例子（下面每段输出都是真实跑出来的，`python scripts/show_abstraction_examples.py` 可复现）

**① 同一条 `Message`，两个协议各自的请求体**——你写的只有一份：

```python
messages = [
    Message(role="system", content="简洁"),
    Message(role="assistant",
            tool_calls=[ToolCall(id="t1", name="get_weather", arguments={"city": "北京"})]),
    Message(role="tool", content='{"temp":24}', name="get_weather", tool_call_id="t1"),
]
```

```
# OpenAI 兼容端点实际收到的
[{"role": "system", "content": "简洁"},
 {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
   "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"}}]},
 {"role": "tool", "content": "{\"temp\":24}", "tool_call_id": "t1", "name": "get_weather"}]

# Anthropic 原生端点实际收到的
system   = "简洁"                       ← 抽成了顶层参数
messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
              "name": "get_weather", "input": {"city": "北京"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
              "content": "{\"temp\":24}"}]}]   ← 工具结果变成了 user 里的 tool_result 块
```

**② usage 十几种写法 → 一个口径**

```
Anthropic 原始 : {"input_tokens": 12, "output_tokens": 34,
                  "cache_read_input_tokens": 1024, "cache_creation_input_tokens": 256}
OpenAI    原始 : {"prompt_tokens": 12, "completion_tokens": 34,
                  "prompt_tokens_details": {"cached_tokens": 1024},
                  "completion_tokens_details": {"reasoning_tokens": 9}}

Anthropic 归一 : {"input": 12, "output": 34, "total": 46, "cache_read": 1024, "cache_write": 256}
OpenAI    归一 : {"input": 12, "output": 34, "total": 46, "reasoning": 9, "cache_read": 1024}
```

两家都没给 `total`，weave 算出来；缓存命中的两种写法（`cache_read_input_tokens` 与
`prompt_tokens_details.cached_tokens`）归一到同一个键。

**③ 错误体两种嵌套 → 同一类失败**

```
OpenAI    429  {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}
          → kind='rate_limit'  retryable=True  retry_after=3.0

Anthropic 429  {"type": "error", "error": {"type": "rate_limit_error", "message": "tokens exceeded"}}
          → kind='rate_limit'  retryable=True  retry_after=None
```

应用只需 `switch(kind)` 一次；`Retry-After` 已经解析并封顶好。

**④ 分片边界：SSE 行被 TCP 切开 / 汉字断在分片中间**

```
'键' 的 3 个字节被切成 1+2：
  朴素逐块 decode : ['�', '��']  → 拼起来 = '���'    ← 乱码
  增量解码        : ['', '键']   → 拼起来 = '键'      ← weave 的做法

一个 data: 行被切成两个分片：
  仍拿到完整事件: {"choices":[{"delta":{"content":"关键路径"}}]}
```

**四种手段**：**翻译**（形状→中立词汇）· **归一**（名字与取值统一）· **兜底**（不合规也读懂）·
**吸收**（偶发故障不进调用方视野）。外加一条反向原则：**保留原始事实**——`raw` / `raw_blocks` /
`attempts` / `elapsed_ms` / `failure` 全交出来。**屏蔽 ≠ 撒谎**：它不假装"一次就成功"，也不假装"我都读懂了"。

**故意不屏蔽的**：厂商原样块的 key（接厂商新能力的唯一入口）· `tool_call_id` 的对应（谁执行、结果是什么是你的决定）·
`arguments` 是否符合你声明的 schema · `finish_reason='length'` 的含义 · 选哪个模型 ·
范围外 API（Batch / Files / Assistants）· "这次一定成功"的假象。

**判据**：换一家厂商，这件事的**说法**会变吗？会变 → 该被 weave 屏蔽。
这件事**该不该做**？→ 属于业务，weave 一律不屏蔽。正因为它对第二类一概不碰，才敢对第一类屏蔽得这么彻底。

**它不省的**：循环 · 停止判定 · 上下文与记忆 · 检索 · 工具执行 · 编排 —— 一行都不省，这是故意的（见下节）。

**什么时候不该用它**：只有**一个**调用点 · **一家**厂商 · 不用流式 · 不做工具往返 · 不需要离线验证这些行为
—— 那就直接用官方 SDK，更省。weave 回本的位置是：**流式 + 工具往返 + 第二家厂商 + 这些行为要能验收**。

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

**工具往返**：weave 只把"模型想调什么"读成结构化请求，**执行与循环都是你的**；回填那两步有构造器，
免得手抄 id：

```python
resp = await w.call(messages, tools=tools)

while resp.finish_reason == "tool_calls":
    messages.append(resp.as_message())                                # 回填 assistant（content + tool_calls）
    for call in resp.tool_calls:
        messages.append(Message.tool_result(call, my_execute(call)))  # name / tool_call_id 自动对上
    resp = await w.call(messages, tools=tools)                        # 轮数上限与停止判定由你决定
```

`tool_result(call, output)`：`output` 是 `str` 就原样，其它值按 `json.dumps(ensure_ascii=False)` 序列化，
不可序列化则抛 `TypeError`。要回填**厂商原样块**（如 extended thinking 的 thinking 块）请显式写
`Message(role="assistant", content=resp.raw_blocks)`——这两个构造器**不做隐式切换**。

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
「它屏蔽了什么」里的四个例子由 `python scripts/show_abstraction_examples.py` 复现（离线，纯函数 + 已有实现）。

## 消费者

| 项目 | 用法 |
|---|---|
| `../12_bePm` | LLM 层走 weave（`backend/engine/weave_provider.py`，默认 `LLM_SDK_TYPE=weave`），不再需要 anthropic/openai SDK |

## License

[MIT](LICENSE) © Weave Contributors
