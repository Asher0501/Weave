# weave 的「LLM 交互」维度 · 规格与完备清单

> **定位**：weave 只做这一个维度。它对外**只有一个输入、一个输出**：
> 输入 `messages`（+ 可选 `tools`），输出一次模型响应。
> 上下文从哪来、要不要执行工具、循环几轮——**都是应用的自由**（判据见 §4）。
>
> 图：[设计哲学](weave-design-philosophy.svg) · [架构](weave-global-architecture.svg)

## 1. 对外形状

```python
import weave

# 最常见：只给模型名，provider 由 weave 装配（零第三方依赖）
w = weave.llm(
    model    = "deepseek-chat",
    timeout  = 30,        # 单次尝试超时
    retry    = 3,         # 首次之外的重放次数
    observer = None,      # 可观测性回调（默认不上报）
)

# 需要自己接厂商时（测试 / 私有网关 / 非 OpenAI 协议）：注入自己的哑原子
w = weave.llm(provider=MyProvider(), timeout=30)

# 一个输入 → 一个输出
resp = await w.call(
    messages,                     # list[Message]
    tools=[...],                  # 可选：list[ToolSchema]
    max_tokens=2048, temperature=0.7,   # 可选：本次动作的参数
)
resp.content        # str
resp.tool_calls     # 已归一：[{id, name, arguments(dict)}]，应用自己决定要不要执行
resp.reasoning      # 推理内容（永不回传）
resp.usage          # 已归一：{"input":n, "output":m, "total":k, "reasoning":r?}
resp.finish_reason  # "stop" | "tool_calls" | "length"
resp.model

# 回填（工具往返的两步；纯数据构造，不执行、不循环、不做策略）
resp.as_message()                 # → assistant 消息（content + tool_calls）
Message.tool_result(call, output) # → role="tool" 消息（name / tool_call_id 自动对上）

# 多模态：写语义，形状由适配层生成（一份输入两个协议通用）
from weave.core.types import TextBlock, ImageBlock
messages = [Message(role="user", content=[
    TextBlock("看图"),
    ImageBlock(url="https://x/a.png"),          # 或 ImageBlock(data=b64, media_type="image/png")
])]

# 流式：同一个动作的另一种呈现（可选）
async for chunk in w.stream(messages, tools=[...]):
    ...

await w.aclose()
```

**一个输入**＝ `messages`（+ 可选 `tools`）。**一个输出**＝ 归一后的 `LLMResponse`。
`call()` 正常返回响应；重试耗尽时**抛类型化异常**（异常携带 `failure: TypedFailure`）。

## 2. 内部结构（四块 + 一层契约）

```
weave/llm/                     ← 对象层（本维度）
  client.py      LLMClient     一次动作的生命周期：装配 → 调用 → 归一 → 计量 → 上报
  reliability.py 可靠性         超时 / 重试 / 退避 / 限流 / 取消
  decode.py      解码归一       原生 tool_calls / DSML / JSON → Invocation
  usage.py       计量归一       各厂商 usage 字段 → 统一口径
  streaming.py   流式聚合       增量 chunk → 完整响应（tool_calls 按 index 归并）
weave/core/                     ← 契约与词汇（②③ 共用的词表，不是栈里的一级）
  types.py         Message / Block（TextBlock · ImageBlock）/ ToolCall / ToolSchema /
                   LLMResponse / StreamChunk
                   + payload_content()：str · Block · 原样 dict 三种形态的唯一定义处
  envelopes.py     CallRequest / Invocation / TypedFailure + 失败分类（8 类）
  errors.py        类型化错误；interfaces.py：LLMProvider · StateStore
weave/providers/                ← 厂商适配层（哑原子：一次调用，失败即抛，不重试不计时不解码）
  openai_http.py      OpenAIHTTPProvider（零第三方依赖，HTTP 走注入式 transport）
  anthropic_http.py   AnthropicHTTPProvider（/v1/messages 原生协议）
  sse.py              SSE 半行缓冲 + 事件组装（F1/F2 的落点）
  transport.py        可注入传输协议 + stdlib 实现（含跨分片 UTF-8 增量解码）
  fake.py             离线确定性实现（测试）
```

分工铁律：**原子是哑的**（一次 HTTP/一次调用，失败即抛，不重试不计时）；
**可靠性、解码、计量、上报全在对象层**。这两边不能互相渗透（否则"换厂商"要改对象，
"换重试策略"要改原子）。

## 3. 完备清单（= 验收标准）

每一条都必须有对应的离线测试。V1 表示本轮必须达成，V2 表示留接口不实现。

### A. 请求构造
- [x] A1 `Message` → 厂商请求体：`system/user/assistant/tool` 四种角色正确映射（V1）
- [x] A2 `assistant.tool_calls` 回传格式正确；`tool` 消息必带 `tool_call_id`（V1）
- [x] A3 厂商差异过滤：如 `deepseek-reasoner` 不接受 `temperature/top_p`（V1）
- [x] A4 `reasoning` 内容**绝不回传**给下一次请求（V1）
- [x] A5 原样内容块（厂商独有块）逐字透传，weave **不解释任何 key**（V1）
- [x] A9 **输入统一**：中立块 `TextBlock` / `ImageBlock` 由适配层翻译成厂商形状——同一份输入在两个
      协议下**语义等价**（形状不同）；构造时校验；与原样 dict **不可混用**；与 `tool_calls` 可共存
      （`Block` 是封闭集合）；`data:…;base64,` 这类厂商编码只出现在适配层（V1）
- [x] A6 原样块与 `tool_calls` 同时给出视为歧义 → 在**发出任何请求之前** `TypeError`，
      且**不得进入可靠性重放**（否则编程错误会被归成可重试的 `server` 并退避重放）（V1）
- [x] A7 `role="tool"` 的原样 content 是 `tool_result` **内层**内容（工具可返回图片）；
      system 出现原样块时顶层 `system` 用块数组（这样 `cache_control` 缓存断点可达）（V1）
- [x] A8 适配层按**配置选定的厂商**拦"别家形状"的块：适配器可选自述 `protocol` /
      `foreign_block_types` / `block_shape_hint`（三个类属性，**不是接口的一部分**，不声明则跳过），
      对象层在**发请求之前**预检并给出本厂商的写法示例。只拦别家**已知**形状，
      **未知块一律放行**（不挡厂商新特性）；`weave.llm(..., shape_check=False)` 可关（V1）

### B. 认证与端点
- [x] B1 `api_key` / `base_url` 可由构造器给，也可从环境变量取（V1）
- [x] B2 支持自定义 headers 与网关（用于代理/内部网关）（V1）
- [x] B3 每次请求动态取 key（支持轮换/多 key），不在对象里缓存语义（V1）

### C. 时间
- [x] C1 单次尝试超时（`timeout`）（V1）
- [x] C2 整序列上限（含所有重放的总时长），防止退避把总时长拖爆（V1）
- [x] C3 超时归类为 `timeout`（`retryable=true`）（V1）

### D. 重试
- [x] D1 只重放**可重试类**（rate_limit / server / network / timeout）（V1）
- [x] D2 指数退避 + 抖动；上限可配（V1）
- [x] D3 尊重厂商的 `Retry-After`（限流时按它等，而不是按自己的退避）（V1）
- [x] D4 次数与耗时计入返回值/事件（`attempts`、`elapsed_ms`）（V1）
- [x] D5 判据明确：**同一请求的重放归本维度**；变更输入后的再一次动作归应用（V1）

### E. 错误分类
- [x] E1 HTTP 状态 → 类型化（429/5xx/401/400/402…）（V1）
- [x] E2 网络异常 → `network`；超时 → `timeout`；DNS/连接重置同族（V1）
- [x] E3 厂商错误体（JSON 里的 message/code）解析进 `TypedFailure.message/detail`（V1）
- [x] E4 业务性拒绝（如内容策略）→ `rejected`，**不重试**（V1）
- [x] E5 `origin` 标明来源（`provider:<名字>`），便于定位（V1）

### F. 流式
- [x] F1 SSE 解析：`data:` 行、`[DONE]`、keep-alive 空行（V1）
- [x] F2 **跨网络分片的半行**必须正确缓冲（不能把一行截成两半丢字段）（V1）
- [x] F3 `content` 增量聚合（V1）
- [x] F4 `reasoning_content` 增量聚合，且不回传（V1）
- [x] F5 `tool_calls` 增量拼接：按 index 归并 `id` / `name` / `arguments` 片段（V1）
- [x] F6 流中途失败：保留已产出的部分，标记失败，不静默吞掉（V1）
- [x] F7 取消（`asyncio.CancelledError`）正确上抛并关闭连接（V1）
- [x] F8 流式末尾的 usage（部分厂商只在最后一个 chunk 给）（V1）

### G. 输出解码归一（用户已确认留在本维度）
- [x] G1 原生 `tool_calls` → `Invocation`；`arguments` 从 JSON 字符串转 dict（V1）
- [x] G2 DSML 文本兜底（含全角/半角竖线）（V1）
- [x] G3 ```` ```json ```` 代码块兜底（V1）
- [x] G4 三种都解不出 → 返回空列表，**不报错**（残留文本仍在 `content` 里）（V1）
- [x] G5 结构损坏到无法继续（如 arguments 不是合法 JSON）→ `parse_error`（V1）
- [x] G6 解码不改变输入（纯函数）；不执行任何动作（V1）
- [x] G7 响应保留厂商**原始内容块**（`LLMResponse.raw_blocks`），text/thinking/tool_use 之外的块
      **不丢**；原样回填成下一轮 `Message.content` 即闭环（extended thinking 要求 thinking 块
      随 `tool_use` 回传，靠这条才成立）（V1）

### H. 计量
- [x] H1 各厂商字段名归一：`prompt_tokens/completion_tokens/input_tokens/total_tokens`（V1）
- [x] H2 推理 token（`reasoning_tokens` / `completion_tokens_details`）单列（V1）
- [x] H3 对象级累计（`w.usage`），供应用读取（V1）
- [x] H4 流式下 usage 缺失时不报错，仅缺字段（V1）
- [ ] H5 货币成本估算**不做**（需要价格表 → 交给注入的 observer / 应用）（V2）

### I. 可观测性
- [x] I1 回调是**注入的**，默认 no-op；对象**不写日志、不埋点**（V1）
- [x] I2 事件集：`llm.request` / `llm.response` / `llm.retry` / `llm.failure` / `llm.stream_end`（V1）
- [x] I3 事件带 `attempts` / `elapsed_ms` / `usage` / `model`（V1）
- [x] I4 **回调抛异常不得影响主流程**（V1）

### J. 失败通道
- [x] J1 所有失败都是 `TypedFailure`（`kind` + `retryable` 一致）（V1）
- [x] J2 重试耗尽 → 抛类型化异常，异常携带 `.failure`（V1）
- [x] J3 原子抛出的异常在对象层被分类，不让原始异常穿到应用（V1）

### K. 生命周期与并发
- [x] K1 同一对象可并发调用（无共享可变状态竞争）（V1）
- [x] K2 `aclose()` 释放底层连接；重复调用安全（V1）
- [x] K3 无全局可变状态；配置只在构造器 + 实例属性（V1）

### L. 可测试性
- [x] L1 注入式 transport：不联网即可测重试/超时/流式/错误分类（V1）
- [x] L2 提供 `FakeProvider`（哑原子）用于对象层测试（V1）
- [x] L3 换厂商实现（兼容/Anthropic/Fake）→ **对象层与应用零改动**（V1）

## 4. 边界

判据一句话：**需要跨多次交互才成立的，都不在本维度。**

- **在这**：一次交互内部的协议适配 · 可靠性（超时/重放/退避/限流/取消/分类）· 解码归一 ·
  计量归一 · 流式聚合 · 可观测性回调。
- **不在这**：多轮循环与停止判定 · 上下文组装与记忆 · 检索召回 · 工具执行 · 编排 ·
  模型选择与路由 · 成本估算。这些是实现应用时的自由，weave 不提供、也不替调用方决定。

## 5. 验收方式与证据

```bash
python -m pytest tests/v04 -q        # 全离线（注入假 transport，不联网、不需要 API key）
```

| 证据 | 结果 |
|---|---|
| 本维度离线测试 | **174 passed**（对象层 · 协议适配/SSE/传输 · 内容块透传与适配层预检 · 契约面与门禁） |
| 真实端点冒烟 | **已跑通**（见 §8） |
| 替换测试 | 换 provider（`OpenAIHTTPProvider` ↔ `AnthropicHTTPProvider` ↔ `FakeProvider`）→ 对象层与应用零改动 |
| 反向依赖门禁 | 源码扫描：原子层/`llm` 层不得出现编排或策略符号；`weave` 全包零第三方依赖 |
| D13 边界门禁 | `weave/providers/*.py` 不得出现 `Invocation`（原子不解码） |
| 端到端冒烟（离线） | `weave.llm(FakeProvider)` → `content / tool_calls / usage / attempts` 全部归一 |

未勾选项只有 1 条：**H5 货币成本估算**（V2）—— 它需要价格表，归属注入的 observer，
不属于"一次交互"。

## 6. 真实端点实测记录

脚本：`scripts/smoke_real_llm.py`（只从环境变量或 `~/.claude/settings.json` 取凭证，
**不打印 key**；把「原始响应」与「归一结果」并排输出，便于对照）。

实测端点：`https://api.deepseek.com/anthropic`（DeepSeek 的 Anthropic 兼容端点），
模型 `deepseek-v4-pro`，`protocol="anthropic"`。

| # | 路径 | 结果 |
|---|---|---|
| 1 | 非流式 + 给足 token | ✅ `content` 正确、`finish_reason=stop`、usage 归一 |
| 2 | 非流式 + `max_tokens=32` | ✅ 正确归一为 `content=''`、`finish_reason='length'`（预算被 thinking 吃光，见下） |
| 3 | 流式 `call_streaming()` | ✅ 2 个网络分片 → 35 个归一 chunk（`reasoning`×32 / `usage`×2 / `finish`×1），聚合结果与 usage 正确 |
| 4 | 带工具声明 + 真实 `tool_use` | ✅ 原始 `content:[{thinking},{tool_use,id,name,input:{city:北京}}]` → `finish_reason='tool_calls'`、`tool_calls=1`，`input` 已是对象 |

### 两个只有真机才会暴露的点

1. **prompt cache 计数**：真实 usage 里有 `cache_creation_input_tokens` / `cache_read_input_tokens`
   （OpenAI 兼容侧对应 `prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens`）。
   原先被丢弃 → 现已归一为 `cache_read` / `cache_write`（**只在实际非 0 时出现**，
   且不计入 `input`，因为厂商的 `input_tokens` 不含缓存命中部分）。

2. **thinking 会吃掉预算**：该模型默认输出 thinking（无需显式开启）。`max_tokens` 给小了
   （如 32），整个预算会被思考耗尽 —— 表现为 `content` 为空、`finish_reason='length'`、
   `reasoning` 有内容。**这不是 bug，是真实的预算陷阱**：调用方要么给足预算，
   要么显式读 `reasoning` 并检查 `finish_reason`。weave 已把三者（`content` / `reasoning` /
   `finish_reason`）都交出来，不做隐式改写。

