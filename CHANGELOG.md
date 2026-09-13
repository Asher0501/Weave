# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。
v0.3 及更早的记录见 [`archive/v0.3/CHANGELOG.md`](archive/v0.3/CHANGELOG.md)——
v0.4 是**全新定义**，不背 v0.3 兼容包袱。

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
