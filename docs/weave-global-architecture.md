# Weave 全局架构

> **一句话**：weave 只做**一个维度**——LLM 交互。对外**一个输入、一个输出**；
> 上下文从哪来、工具怎么执行、循环几轮，**都是应用的自由**。

![Weave 架构](./weave-global-architecture.svg)

设计哲学单独一张图：[`weave-design-philosophy.svg`](./weave-design-philosophy.svg)
（先验 · 判决规则 · 哑原子/聪明对象 · core 是词表 · 三种输入形态 · 不许静默）。
两图都由 `python scripts/render_design_svg.py` 生成，**零依赖**（手写 SVG）且带排版自校验。

## 1. 四段 + 两张"收齐表"

**每段只写它自己的事；最后两张表把「上面用到的词」与「要守的规矩」收齐。**

```
① 应用 / 业务（外部）
     输入  messages + tools ｜ 三种取值：纯文本 / 中立块 / 原样厂商块（谁翻译：适配层 / 适配层 / 调用方）
     输出  LLMResponse（正常）｜ TypedFailure（失败，8 类）—— 这两个名字都出自词表
     自己决定  循环 · 上下文 · 执行 · 编排（需要跨多次交互的，都不在 weave）
   ── messages（+ 可选 tools）──►
② weave.llm —— 对象层（一次动作的生命周期）
     call · call_streaming · stream · aclose
     预检 shape_check · reliability · decode · usage · streaming · 回填构造器
     失败在这里被归类：TypedFailure 的 8 个 kind —— 可重试 4 类 / 不可重试 4 类
   ── CallRequest（哑原子：一次调用，失败即抛）──►
③ weave.providers —— 适配层（哑原子 · 可替换）
     OpenAIHTTPProvider · AnthropicHTTPProvider · FakeProvider · 可注入 Transport
     厂商差异在这里被吃掉：认证 · 端点 · 参数 · 形状（三种输入形态也在这里落地）
     纪律  不重试 · 不计时 · 不分类 · 不解码 —— 它只做翻译
   ── HTTP / SSE ──►
④ 厂商（外部）
     OpenAI 兼容端点 · Anthropic 原生 · 各类网关 —— 上面那些差异的来源

┌ 收齐 1 · 词表 weave.core —— 上面出现过的类型全部在这里定义（②③ 共用，不是栈里的一级）
│  接口  LLMProvider · StateStore
│  词汇  Message · Block（①的输入）· ToolCall · ToolSchema（工具往返）·
│        LLMResponse（②的输出）· StreamChunk（流式）
│  信封  CallRequest（② → ③）· Invocation（② 内部）· TypedFailure（② 的失败出口）
│  失败  可重试 rate_limit · server · network · timeout ｜ 不可重试 auth · bad_request · rejected · parse_error
└────────────────────────────────────────────────────────────────────────────
┌ 收齐 2 · 守则 · 不变量与门禁 —— 机械执行上面每一段（违反即测试红）
│  契约面（就是上面那份词表）接口 2 个不得膨胀 · 已归档的名字必须真的不存在
│  原子层（③ 与 stores）不得 import 上层 · ③ 不得出现 Invocation · core 零第三方依赖
│  ② 不得 import 存储 · 替换测试：换适配器 → 对象层与调用方零改动 · 改 core = 基变换（②③ 同步改）
└────────────────────────────────────────────────────────────────────────────
```

**为什么这样摆**：输入形态本来就是 `Message.content` 的取值，所以写在 ① 的"输入"那一行；
失败 8 类本来就产生在对象层，所以写在 ② 的"失败"那一行。词表与守则是**收齐**，不是新增的第三、四块——
词表里每个名字都标了它出现在哪一段，守则里每条都注明了它守的是谁。
所以它们不再是"四张并列清单"，而是一段链 + 两张汇总。

## 2. 对外 API

```python
import weave

w = weave.llm(model="deepseek-chat", timeout=30, retry=3, observer=None)
# 默认装配零第三方依赖的 OpenAIHTTPProvider；
# 需要测试 / 私有厂商 / 另一家协议时：
#   weave.llm(model="claude-sonnet-4-20250514", protocol="anthropic")
#   weave.llm(provider=MyProvider())
resp = await w.call(messages, tools=[...], max_tokens=2048)
resp.content, resp.tool_calls, resp.usage, resp.finish_reason
resp.attempts, resp.elapsed_ms          # 这个动作试了几次、花了多久
resp.raw, resp.raw_blocks               # 厂商原始片段 / 原始内容块（不丢块）
resp.as_message()                       # 回填 assistant（content + tool_calls）
Message.tool_result(call, output)       # 回填工具结果（name / tool_call_id 自动对上）

async for chunk in w.stream(messages): ...              # 只要增量（自行组装）
resp = await w.call_streaming(messages, on_chunk=...)   # 流式 + 内部聚合
await w.aclose()
```

**输入三种内容形态**（`Message.content`）：

| 形态 | 写法 | 谁翻译成厂商形状 | 可移植性 |
|---|---|---|---|
| 纯文本 | `content="你好"` | weave（适配层包成文本块） | **跨厂商** |
| **中立块** | `content=[TextBlock("看图"), ImageBlock(url=…)]` | weave（适配层生成厂商形状） | **跨厂商**（多模态推荐） |
| 原样厂商块 | `content=[{"type": "image", "source": {…}}]` | 调用方 | 仅当前厂商；换 provider 需改写 |

原样形态是接厂商独有能力的**唯一入口**（多模态、`cache_control`、thinking 回传、厂商新块），
weave 不解释任何 key。适配层按**配置选定的厂商**拦"别家已知形状"的块（未知块一律放行，
`shape_check=False` 可关）——厂商知识只在 ③，`Message` 里没有厂商名。

完整规格与逐条验收清单：**`docs/weave-llm-dimension.md`**。

## 3. 不变量与门禁

**不变量**

1. 对象只认识 `LLMProvider`（哑原子）；循环 / 上下文 / 执行 / 编排都不在对象里；
2. 适配层是哑的：不重试 · 不计时 · 不分类 · 不解码 · 不知道策略；
3. 失败一律 `TypedFailure`（`kind` 与 `retryable` 必须一致，可重试 4 类 / 不可重试 4 类）；
4. 可观测性只有一条通道：注入的回调 `observer=`；对象自己不写日志、不埋点；
5. 无全局可变状态：配置只在构造器与实例属性上；
6. `Message` 与 `weave.core` **不认识任何厂商**。

**门禁（`tests/v04/test_contracts.py`，CI 机械执行）**

- 接口面只保留**真有消费者**的两个名字（`LLMProvider` · `StateStore`），
  已归档的接口与包**必须真的不存在**（名单在测试文件里）；
- `weave.__all__` 只允许五个名字，防止公共面悄悄膨胀；
- 原子层（core / stores / providers）不得 import 上层（`weave.llm` / `demos`）；
- `weave/llm` 不得 import 存储或已归档模块；
- `core` 零第三方依赖；`providers` 不得出现 `Invocation`（原子不解码）；
- **替换测试**：换 provider（`OpenAIHTTPProvider` ↔ `AnthropicHTTPProvider` ↔ `FakeProvider`）
  → 对象层与应用零改动（`str` 形态下成立）。

## 4. 怎么跑

```bash
python -m pytest tests/v04 -q                 # 全离线：假 transport，不联网、不需要 API key
python scripts/render_design_svg.py           # 重画两张图（含排版自校验）
```
