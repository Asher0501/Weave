# Weave v0.4 — 06 迁移与落地（migration）

> 状态：**主体已执行**（v0.3 归档完成、README 重写、DoD A1–A8 达成）。

## 1. 包与命名变化

| 项 | v0.3 | v0.4 | 状态 |
|----|------|------|------|
| 包名/发行名 | `weave_agent_sdk` / `weave-agent-sdk` | `weave` | ✅ 归档+新建 |
| 顶层入口 | `Weave`（config 容器） | **无高层入口**：原子件 + 调用方组合（示例 `demos/atomic/`） | ✅（便利层移出） |
| 接口 | BaseLLM / BaseLoop / 死接口 Memory ABC | `weave.core` 类型化原子契约 | ✅ |
| 重试位置 | loop 层 `_chat_with_retry` | Provider 内嵌（`RetryPolicy`） | ✅ 已下沉 |
| 循环 | simple/iterative/scheduled 三套 | **不随包**：循环是调用方代码（示例 `demos/atomic/loop.py`） | ✅ |
| 事件 | EventBus + stream() 契约 | `EventSink` 钩子（调用方循环 emit；capabilities.TraceSink） | ✅ 最小 |
| CLI / 装配 | `weave init`、weave.yaml 装配 | **无**（已移出 weave；脚手架是调用方示例） | ✅ 移除 |
| 版本源 | `weave_agent_sdk.__version__` | `weave.__version__` | ✅ |

## 2. v0.3 归档（已执行）

旧资产整体移入 **`archive/v0.3/`**：

- 源码与打包：`weave_agent_sdk/`、`weave_agent_sdk.egg-info/`；
- 设计/宪法文档：`DESIGN.md`、`docs/{basic.md, public-api.md, config-reference.yaml, internal-utilities.md, test-spec.md, issues/, history/}`；
- 示例与数据：`prompts/ examples/ demo/ nexus/ MagicMock/ .weave/ data/memory.db`；根 `weave.yaml`（v0.3 样例）与 `CHANGELOG.md`；
- 测试：`tests/` 下 7 个旧测试文件 → `archive/v0.3/tests/`（如需回归：`python -m pytest archive/v0.3/tests -p no:asyncio`，且需把 `archive/v0.3` 加入 PYTHONPATH 以导入归档包）；
- 技能/工具：`.claude/`（weave-sdd SKILL，v0.3 版）。

根目录只保留 v0.4：`weave/ tests/v04/ docs/v0.4/ demos/forum/ data/(新 demo 产物) pyproject.toml README.md`。

**外部依赖说明**：`../14_forum`（agora）已全量运行于 weave v0.4（LLM + Repository 迁移完成，142 项回归绿），运行时通过 `PYTHONPATH=13_weave` 提供 weave 包；agora `pyproject.toml` 依赖行仍写 `weave-agent-sdk`，属 agora 发布侧待改项（见 MIGRATION-REPORT）。

## 3. 测试

- `tests/v04/`：**37 项**原子测试（契约/存储/Provider 可靠性/示例注册表/示例组合 e2e/能力/原子面自检），根目录 `python -m pytest tests/v04 -q -p no:cacheprovider -p no:asyncio`；
- v0.3 旧测试已随包归档（见 §2），不再与 v0.4 混跑。

## 4. 发布清单（DoD 门禁，按修订注 R-2026 口径）

- [x] A1 端到端对话（原子件 + 调用方组合：`demos/atomic` e2e 测试覆盖多轮/工具/错误）
- [x] A2 自定义上下文来源（任意对象实现 `build` 即可被调用方循环使用）
- [x] A3 共享与隔离（业务自选命名空间；SQLite/同后端隔离测试）
- [x] A4 Provider 独立可靠（可靠性单测 + reasoner 过滤 + reasoning 不回传）
- [x] A5 核心原子自检（weave.core 不 import 能力/示例/组合层）
- [x] A6 ~~DX 零配置~~ 已撤销：weave 无 Agent/CLI/装配（配置装配属业务代码）
- [x] A7 命名零约定（core 不解析 namespace；无 shared/agent/messages 默认；历史位置调用方显式注入）
- [x] A8 迁移与叙事（v0.3 归档 archive/v0.3/；README 为原子版；真实集成 demo → `demo-forum/REPORT.md`）

## 5. 真实集成（已完成）

`../14_forum`（agora）× weave v0.4 接入分两步完成：
- LLM 适配迁移 → [`demo-forum/REPORT.md`](../demo-forum/REPORT.md)；
- **Repository 迁移（agora 全量 v0.4，142 项回归全绿，含 QG-5 并发 NFR）** → [`demo-forum/MIGRATION-REPORT.md`](../demo-forum/MIGRATION-REPORT.md)；
可复跑脚本 → `demos/forum/`（smoke / 完整真实 / 离线）。

## 6. 待办（发布前）

- 能力层完整形态（server / scheduled / features，见 04-capability.md）；
- 依赖矩阵核验与可编辑安装验证（本会话 pip editable 超时，改用 PYTHONPATH 验证通过）；
- 归档分支策略与 CHANGELOG v0.4 首版。
