# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。
v0.3 及更早的记录见 [`archive/v0.3/CHANGELOG.md`](archive/v0.3/CHANGELOG.md)——
v0.4 是**全新定义**，不背 v0.3 兼容包袱。

## [未发布]

**主题：让 `Message` 能承载厂商独有的内容块（原样透传）——零新增契约词汇。**

### Added

- **适配层"错家形状"预检**：适配器（provider）可选自述三个类属性
  `protocol` / `foreign_block_types` / `block_shape_hint`——**不是接口的一部分**，不声明就跳过。
  对象层据此在**发请求之前**拦掉"把 OpenAI 的 `image_url` 块发给 Anthropic 适配器"这类错误，
  报错里直接给出当前厂商的正确写法与出路（换形状 / 换 `protocol` / `shape_check=False`）。
  两条纪律：只拦别家**已知**形状，**未知块一律放行**（不挡厂商新特性）；
  `weave.llm(..., shape_check=False)` 可整体关掉。`Message` 依旧不感知任何厂商。
- **两张设计图（SVG）**：`docs/weave-design-philosophy.svg`（先验 · 判决规则 · 哑原子/聪明对象 ·
  core 是词表 · 两种内容形态 · 不许静默）与 `docs/weave-global-architecture.svg`
  （应用 → 对象 → 厂商适配层 + 契约词表），由**零依赖**的 `scripts/render_design_svg.py`
  生成（手写 XML，自带 CJK 宽度排版自校验，溢出即非零退出）。
- **回填构造器**：`LLMResponse.as_message()` 与 `Message.tool_result(call, output)` ——
  工具往返里最容易写错的两步（assistant 回填、`tool_call_id` 对应）从此各只有一处实现。
  `tool_result` 对非 `str` 输出按 `json.dumps(ensure_ascii=False)` 序列化，不可序列化则抛 `TypeError`；
  回填**厂商原样块**（extended thinking 的 thinking 块）仍显式写
  `Message(role="assistant", content=resp.raw_blocks)`——**不做隐式切换**（那会静默改变可移植性）。
  **契约面零新增**：只是既有类型上的方法，不加接口、不加导出、不动门禁。
- **`Message.content` 除 `str` 外接受 `dict` / `list[dict]`**：weave **不解释任何 key**，
  逐字写进该角色的内容槽位。
  - 多模态（`{"type":"image",…}` / `{"type":"image_url",…}`）、`cache_control` 缓存断点、
    厂商未来新增的任意块，都走这一个口子——不改 weave、不新增契约名字（仍是 2 接口 / 4 信封）。
  - `role="tool"` 的原样 content 是 `tool_result` **内层**内容 → 工具可以返回图片。
  - system 含原样块时，Anthropic 顶层 `system` 改用块数组 → 长 system 上可打缓存断点。
- **`LLMResponse.raw_blocks`**：保留厂商**原始内容块数组**，text/thinking/tool_use 之外的块不再被丢弃。
  原样回填成下一轮 `Message(role="assistant", content=resp.raw_blocks)` 即闭环
  （extended thinking 要求 thinking 块随 `tool_use` 一起回传，靠这条才成立）。

### Changed

- OpenAI 响应的 `content` 是数组时，按文本块拼接进 `LLMResponse.content`，其余块进 `raw_blocks`
  （原先数组会被直接塞进 `content: str` 字段，类型与语义都不对）。
- **用法错误提前到发请求之前**：原样块里混入非 dict、或原样 `content` 与 `tool_calls` 同时给出，
  现在在 `LLMClient._request` 阶段就 `TypeError`。此前这类错误会在 provider 里抛出，被
  `classify_exception` 归成**可重试**的 `server`——白白退避重放 3 次，还把编程错误报成厂商故障。
- `messages_to_anthropic` 的返回值放宽为 `(str | list[dict] | None, list[dict])`（system 可为块数组）。

### 文档与图（同步收敛）

- **门面文档只描述 weave 现在有的东西**：`README.md` / `docs/weave-*.md` 里移除
  "已归档能力清单 / 迁移映射 / 决策状态表 / 未来维度预告"，以及坏掉的消费者指向（agora）
  与历史设计基线入口（`docs/v0.4/`）。被移除的内容**归档保留**，不删除：
  `archive/v0.4-parked/retired-scope.md`。
- 旧的图与渲染脚本一并归档到 `archive/v0.4-parked/renderers/`
  （`render_architecture_{png,ascii}.py` · `weave-global-architecture.{png,mmd}`）——
  它们画的是含"已归档能力"那一版的图；`docs/v0.4/` → `archive/v0.4-parked/docs-v0.4/`；
  `demos/`（agora 演示脚本）→ `archive/v0.4-parked/demos/`。归档文档内的路径引用已同步修正。
- `docs/weave-llm-dimension.md`：原 §4「非目标」表 → 收敛为三条式「边界」（判据一句话 +
  在这/不在这）；§5「与其它能力的边界」与 §6「代码去留映射」移出；验收数字更新为 174 passed。

### Verified

- 离线测试 **180 passed**（143 原有 + 24 块透传 + 7 适配层预检 + 6 回填构造器）：透传规则 · 歧义拒绝 ·
  两个 provider 双向 · thinking 块回填闭环 · 错家形状预检（含"未知块放行"与"可关"）·
  预检先于重放 · 对象层不改写内容 · 回填后的 id 在两个协议里都对得上 · 契约面未增长。

### 代价（明确写下来）

- 原样块是**厂商形状**：只对当前厂商有效、**换 provider 不可移植**；`str` 形态才跨厂商。
  「替换测试」（换 provider 调用方零改动）因此只在 `str` 路径上成立。

## [0.4.0] - 2026-09-13

**主题：收缩为单一功能 —— 「LLM 交互」（一个输入、一个输出）。**

### Breaking Changes

- **交付范围收敛为单一维度**：`weave.llm(...)` 完成一次模型交互。
  多轮循环、上下文组装与记忆、检索召回、工具执行、编排、多 Agent 协调 **一律不再由 weave 提供**，
  由调用方自己写（agora 的 relay、bePm 的 engine 都是这么做的）。
- **契约面收缩**：接口 **7 → 2**（`LLMProvider` · `StateStore`）；
  信封 **13 → 4**（`CallRequest` / `LLMResponse` / `StreamChunk` / `TypedFailure`）。
- **删除并归档**（`archive/v0.4-parked/`，是归档不是删除）：
  - 接口：`Database`(别名) · `ConversationLog` · `Search` · `Executor` · `EnvironmentCatalog` ·
    `ToolRegistry` · `Provider`(legacy) · `EventSink` / `NoopEventSink` · 三个可选存储协议
  - 包：`weave.logs` · `weave.search` · `weave.executors` · `weave.catalogs` · `weave.strategy` ·
    `weave.capabilities` · `weave.core.codec` · `weave.stores.redis_store`
  - legacy provider：`openai_compat` + `_reliability` + `adapter`（419 行；基于 openai SDK 且内嵌重试）
  - 类型 `ToolResult` · `SearchHit`；错误 `ToolError` / `CatalogError` / `StorageError` /
    `ProtocolError` / `ContextLengthError` / `RetryExhaustedError` 及对应失败 `kind`
  - 存储实现上的 `search` / `trim` / `list_namespaces` / `list_keys`（无契约消费者）
- **可观测性只保留一条通道**：注入回调 `observer=`（原 `events: EventSink` 删除）。
- `StateStore` 成为 KV 的唯一名字（原 `Database` 别名移除）。

### Added

- **`weave.llm(...)` 对象层**，内部四块：
  - `reliability`：单次超时 · 指数退避+抖动重放 · **尊重 `Retry-After`** · 整序列总预算 · 取消安全 · 8 类失败分类
  - `decode`：原生 `tool_calls` / DSML / JSON 代码块 → 统一结构；严格/宽松两种模式
  - `usage`：厂商字段归一（含 `reasoning` 与 **prompt cache** 计数）+ 对象级累计
  - `streaming`：**SSE 跨分片半行缓冲** · 跨分片 UTF-8 增量解码 · `tool_calls` 按 index 归并 · 末尾 usage
- **`AnthropicHTTPProvider`**：Anthropic **原生协议**（`/v1/messages`：system 顶层参数、
  `tool_result` 内容块、`tool_use.input` 已是对象、SSE 事件流 `content_block_delta` / `input_json_delta`）。
- **`OpenAIHTTPProvider`**：OpenAI 兼容协议，**零第三方依赖**（stdlib `urllib` + 自写 SSE）。
- **可注入 `Transport`**：协议适配可**离线**验收（不联网、不需要 API key）。
- **`FakeProvider`**：离线确定性哑原子（测试用）。
- **门禁**（`tests/v04/test_contracts.py`）：契约面与公共导出不得膨胀、已归档的接口/模块必须真的不存在、
  原子层不得依赖上层、`weave.llm` 不得碰存储、原子层不得出现 `Invocation`、全包零第三方依赖。
- **CI**（`.github/workflows/ci.yml`）：Python 3.11/3.12/3.13 矩阵 + 独立门禁 job（零依赖源码扫描）。
- `scripts/smoke_real_llm.py`：真实端点冒烟（凭证只从环境变量或 `~/.claude/settings.json` 取，
  **不打印 key**；把原始响应与归一结果并排输出）。
- `scripts/render_architecture_{ascii,png}.py`：架构图生成（带排版自校验）。

### Changed

- **解码归属**：独立 `Parser` 接口 → **对象内部**（`weave/llm/decode.py`）——"读懂模型输出"属本维度。
- **单动作可靠性归属**：provider 内嵌 / 策略层会话管理 → **对象层** `weave/llm/reliability.py`；原子保持哑。
- `ToolCall.arguments` 放宽为 `Any`：原子保持厂商原样（JSON 字符串），由对象层解码。
- 用量归一新增 `cache_read` / `cache_write`（真实端点实测发现；**非 0 才出现**，不计入 `input`）。
- 文档据实重写：`README.md`（门面）、`docs/weave-llm-dimension.md`（规格 + 逐条验收清单 + §8 实测记录）、
  `docs/weave-global-architecture.md`（含决策状态表）；架构图三态（PNG/SVG/mermaid）重生成。
- 打包元数据：description/keywords/extras/urls 与实际一致；`dependencies = []`；
  新增 `[tool.pytest.ini_options]`（`testpaths = ["tests/v04"]`）。

### Removed

- v0.3 旧资产整体归档到 `archive/v0.3/`（原 agent SDK、`weave.yaml`、prompts、旧 tests/docs/examples）。
- 中间态文档归档：`docs/weave-architecture.*`、`docs/weave-layered-architecture.*`、两视图旧图；
  `docs/v0.4/`（原子库时期的设计基线）标注为历史。
- 仓库卫生：清理 `data/` 运行残留、`.pytest_cache/`、临时脚本目录。

### Verified

- **离线测试 143 passed**（不联网、不需要 API key）。
- **真实端点冒烟全通**（`https://api.deepseek.com/anthropic` + `deepseek-v4-pro`，四条路径）：
  非流式文本 · thinking-only（预算耗尽）· **流式聚合**（2 网络分片 → 35 chunk）· **真实 `tool_use` 解码**。
- `../12_bePm` 已切到 `LLM_SDK_TYPE=weave`（默认，不再需要 anthropic/openai SDK），其离线测试 11 passed。
- 本机 editable 安装已修正：`weave-0.1.0` → `weave-0.4.0.dev0`；卸载了陈旧的 `weave_agent_sdk-0.3.0`。

### Known issues

- 若在别的机器上遇到旧元数据（`pip show weave` 显示 0.1.0），执行：
  `pip uninstall -y weave weave_agent_sdk && pip install -e .`
- `../14_forum`（agora）的 `agora/adapter/llm.py` 仍指向**已归档**的 legacy provider，
  需要迁移到 `OpenAIHTTPProvider` / `AnthropicHTTPProvider`，或从归档取回旧实现。
- **未发布 PyPI**：目前"开箱即用"等价于"拿到源码 → `pip install -e .` 或加 `PYTHONPATH`"。
