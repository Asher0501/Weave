# agora Repository 迁移报告 —— 完整跑在 weave v0.4 上

> 目的（第一步价值证明）：让 agora 从"只换 LLM"变为"**完整运行于 weave v0.4**"，并量化删掉多少 v0.3 胶水。
> 结论：✅ 迁移完成 —— agora 运行时 **0 处 v0.3 import**，142 项回归全绿（含并发 NFR QG-5），真实 DeepSeek brainstorm（12 轮）跑通并持久化于 v0.4 `entries` 表。

## 迁移前后对照

| 项 | 迁移前（v0.3） | 迁移后（v0.4） |
|---|---|---|
| agora 运行时 import `weave_agent_sdk` | 2 个文件（`adapter/llm.py`、`adapter/repository.py`） | **0**（grep 验证 NONE） |
| Repository 存储 | `MemoryManager(MemoryConfig(sqlite))`——同步 API，每个 async 方法手工包一层 | `SQLiteStateStore(path)`——原生 async，去掉了包胶层 |
| 存储表 | v0.3 `memory_entries`（namespace+access_type+key） | v0.4 `entries`（namespace,key 行隔离，值 JSON） |
| LLM | v0.3 `create_llm`/`BaseLLM` | v0.4 `OpenAICompatibleProvider`（上一阶段已换） |
| 并发模型 | 单 MemoryManager | 每 run 一个 `SQLiteStateStore` 实例（呼应 agora ADR-0005「per-session weave instance」）→ 跨 run 并行 |

## 量化（删除/替代的胶水）

- **删除 2 处 v0.3 import**：`weave_agent_sdk.memory.manager.MemoryManager`、`weave_agent_sdk.types.MemoryConfig`；
- **删除配置样板**：`MemoryConfig(default_backend=..., default_path=...)` 后端分发对象；
- **删除"同步 API 包 async"层**：v0.3 Memory 全同步，Repository 每个方法都得在 async 里包同步调用；v0.4 store 原生 async，直接 await；
- **改 1 个测试文件**：`tests/test_llm.py` 由 mock v0.3 `create_llm` 改为 mock v0.4 Provider（其余 141 项测试零改动即通过）；
- 改动文件：`agora/adapter/repository.py`（重写）、`tests/test_llm.py`（对齐）、`weave/stores/sqlite.py`（并发/性能增强，见下）。

## 过程中暴露并修复的真问题（"用使用证明"的价值）

迁移后第一次跑 agora 全套 → NFR `QG-5`（5 路并发各 30 轮，p95 劣化 ≤ 2× 单会话 +5ms）失败，真回归。修复轨迹：

| 方案 | 单会话 p95 | 并发 p95 | 判定 |
|---|---|---|---|
| 全局 asyncio 锁 + 共享连接 | ~5ms | 24.8ms（bound 15.6） | ✗ 全局锁串行化并发 |
| 每操作独立连接 | 24ms（connect 开销） | 128.7ms | ✗ 单线程基线被拖慢 |
| 每操作连接 + synchronous=NORMAL | 1.5ms | 16.6ms（bound 8.0） | ✗ 线程跳转开销仍超 |
| **每 run 独立实例 + 内联同步 + WAL(NORMAL)** | ~1.5ms | ✅ 通过 bound | ✅ |

最终形态：`SQLiteStateStore` 操作**同步内联**（无线程跳转/无锁，事件循环原子完成），WAL + `synchronous=NORMAL` 降提交延迟；并发隔离由调用方"每 run 一实例"承担（写者由 SQLite 自身串行）。收益：既有 v0.4 语义（async 接口、JSON、namespace 不透明），又满足 agora QG-5 时序约束。

## 验证证据

- `python -m pytest`（14_forum）：**142 passed**（含 test_nfr QG-5）；
- `python -m pytest tests/v04`（13_weave）：weave 侧原子测试通过（修订注 R-2026 后为 37 项；迁移时点为 45 项，语义无回归）；
- 离线 brainstorm（FakeLLM）：`status=stopped termination=fixed_rounds turns=12`；
- 真实 brainstorm（DeepSeek `deepseek-chat` × 12 轮）：exit 0，运行记录 `data/forum_demo_latest.log`；
- 迁移后新库仅含 v0.4 表：`tables=['entries']`，namespace `agora:<run_id>:state/stream`，12 轮转录落盘。

## 残留项（agora 发布侧，非本迁移）

- agora `pyproject.toml` 依赖行仍写 `weave-agent-sdk>=0.3.0`（运行期已不 import）；应改指向 `weave`（本地路径/PYTHONPATH 或发布后依赖），属 agora 维护决策；
- 旧 v0.3 库文件中的历史 run 数据不可读（表结构不同），如需保留需迁移脚本。
