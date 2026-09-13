# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-09-06

### Breaking Changes

- **包改名**：`weave` → `weave-agent-sdk`，import 路径 `from weave import ...` → `from weave_agent_sdk import ...`
- **Memory 读写同步化**：`stream` / `state` / `knowledge` 的读写方法由 `async` 改为同步，不再需要 `await` 或 `asyncio.run`
- **移除 Claude Code 绑定**：删除 `load_claude_env()` 与 `weave/utils/env.py`，凭证只从标准环境变量 + 显式 `api_key` 读取，不再隐式读取 `~/.claude/settings.json`
- **构造签名变化**：`Weave.__init__` 的 `config_path` 默认改为 `None`，并新增 `config=` 参数

### Added

- **三种构造入口**：`Weave()` 零配置 / `Weave("weave.yaml")` 读文件 / `Weave(config=WeaveConfig(...))` 程序化构造
- **Tool 显式定义**：`@weave.tool(name=..., description=..., schema=...)` 支持覆盖自动推断
- **`stats(purge=False)`**：`MemoryManager.stats()` 默认只读，`status()` 查询不再有写库副作用

### Changed

- `SQLiteStreamMemory` / `SQLiteStateMemory` / `SQLiteKnowledgeMemory` 改名 `MemoryStream` / `MemoryState` / `MemoryKnowledge`（去实现细节命名）
- 默认 provider 收敛为 `types.DEFAULT_PROVIDER` 单点声明
- 对外异常文案统一英文

### Removed

- `load_claude_env()` 工具函数与 `weave/utils/env.py` 模块

## [0.2.0] - 2026-08-31

- checkpoint/rollback（Memory 状态快照与回滚）
- CLI 脚手架（`weave init`）
- 路径规范化（相对路径统一相对配置文件目录）
