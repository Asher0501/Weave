# Weave v0.4 — 设计基线

> 状态：**实现完成**（37 项原子测试通过；真实 LLM demo 已跑通）。先验见 `01-principles.md`。
> 本目录是 v0.4「**全新定义**」的唯一入口。v0.3 旧资产已归档至 `archive/v0.3/`，互不混放。

> ⚠️ **修订注 R-2026（API 原子化）**：weave 交付范围**只含原子件**（`weave.core`/`stores`/`providers`/`capabilities`）。Agent、默认循环/上下文装配、工具绑定、DX（config/builder/sync/CLI init）**已移出 weave 包**，转为调用方示例 `demos/atomic/`；weave 不再提供 CLI、weave.yaml 装配与"零配置 Agent()"。下文中凡指向这些的条目按此注理解。

## 背景（一句话）

v0.4 不再背 v0.3 兼容包袱：Weave 重构为**原子组件库**（包 `weave`）——只交付 `weave.core` 类型化原子契约（Provider / StateStore+可选能力 / ToolRegistry / EventSink）+ `stores`/`providers`/`capabilities` 参考实现；**循环、上下文装配、Agent、工具绑定、CLI/装配器均为调用方代码**（示例 `demos/atomic/`），业务与命名零约定（修订注 R-2026）。

## 目录

| 文件 | 内容 | 状态 |
|------|------|------|
| `01-principles.md` | 先验体系（I/A/D/T/N/分层/否决/术语/挂起；含修订注 R-2026） | **已冻结（先验层）** |
| `02-architecture.md` | 原子契约形态（types/errors/interfaces/可靠性/错误树） | 已产出（随 `weave/core` 同步） |
| `03-recipes.md` | 参考实现（stores/providers/capabilities）+ demos/atomic 组合示例入口 | 已产出 |
| `04-capability.md` | 能力层与钩子（EventSink/TraceSink/CheckpointManager） | 最小外壳已实现；server/scheduled/features 待办 |
| `05-dx.md` | 组合与示例（原 DX 层已移出 weave；调用方最小编排/组合点清单） | 已产出（按 R-2026 重写） |
| `06-migration.md` | 迁移与落地（归档清单/DoD 门禁/发布；DoD 按 R-2026 口径） | 已产出；归档已执行 |
| `demo-forum/REPORT.md` | 14_forum(agora) × weave v0.4 真实 LLM demo（背景/记录/总结汇总表） | ✅ 完成 |
| `demo-forum/MIGRATION-REPORT.md` | agora Repository 迁移：完整跑在 v0.4 + 胶水量化 + QG-5 并发修复 | ✅ 完成 |

## v0.3 归档执行情况（2026 会话）

v0.3 旧资产已整体移入 **`archive/v0.3/`**，根目录只保留 v0.4：

| 归档项（→ archive/v0.3/） | 保留于根（v0.4） |
|---|---|
| `weave_agent_sdk/` + `weave_agent_sdk.egg-info/` | `weave/`（新包） |
| `DESIGN.md`、`CHANGELOG.md`、旧 `weave.yaml` | `README.md`（v0.4 版） |
| `docs/`：basic.md、public-api.md、config-reference.yaml、internal-utilities.md、test-spec.md、issues/、history/ | `docs/v0.4/`（设计基线） |
| `tests/`：test_weave 等 7 个旧测试 | `tests/v04/`（37 项原子测试） |
| `prompts/`、`examples/`、`demo/`、`nexus/`、`MagicMock/`、`.weave/`、`.claude/`、`data/memory.db` | `demos/atomic/`（调用方组合示例）+ `demos/forum/`（agora 测试脚本） |

根目录现状：`weave/ tests/v04/ docs/v0.4/ demos/atomic/ demos/forum/ archive/v0.3/ data/(新 demo 产物) pyproject.toml README.md …`

> 注：`../14_forum`（agora）已全量运行于 weave v0.4（PYTHONPATH=13_weave）；`demos/forum/*` 脚本即用该方式。

## 演进路线

1. 先验层冻结 ✅ → 2. 原子契约 ✅ → 3. 参考实现 ✅ → 4. 能力层（最小 ✅）→ 5. 组合示例（R-2026：原 DX 便利移出 weave → `demos/atomic`）✅ → 6. 迁移落地 ✅（归档执行、README 原子版）→ **真实集成 demo ✅**（`demo-forum/REPORT.md`）

## 挂起/待办（开放项）

- 能力层完整形态：server（REST/WS 事件桥接）、scheduled 常驻循环、observability 落盘、features 骨架（见 `04-capability.md` §4）；
- knowledge 注入机制选择（SearchableStore 可选能力 vs 检索工具）在配方文档标注为开放；
- 发布收尾：依赖矩阵核验、归档分支策略、CHANGELOG v0.4 首版。
