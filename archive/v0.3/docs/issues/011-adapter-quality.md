# Issue #011: 适配层实现质量（行为正确性）

## 结论

与 #010（接线）正交的一类问题：内置组件的**具体实现**做得不对或不完整。
修法 = 逐条改那个组件（而非加机制）。

## 问题清单与修法

| # | 问题 | 现状 | 修法 |
|---|------|------|------|
| 1 | **knowledge 检索对 CJK 失效** | `sqlite.py`/`file.py` 用 `query.split()` + 子串 LIKE，中文整句成一个词，搜不到 | 加 CJK n-gram 兜底（或 FTS5 分词） |
| 2 | **推理模型 thinking block 未提取** | `anthropic.py:_parse_anthropic_response` 只取 `text`/`tool_use`，推理模型返回空 | 提取 `thinking`/`reasoning` block |
| 3 | **auth_token 与 api_key 混用** | `_resolve_api_key("anthropic")` 返回 `ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN`，adapter 一律 `api_key=` | 区分两者，来源是 AUTH_TOKEN 时传 `auth_token=` |
| 4 | **跨 provider 塞错 key** | `config.py` 的 api_key 只查 `ANTHROPIC_API_KEY or OPENAI_API_KEY`，deepseek 场景会错读 | 收敛到 provider 感知的 `_resolve_api_key` |
| 5 | **chroma 静默吞错 / 占位实现** | `knowledge_search` `except: pass`；`cleanup_expired` no-op；`namespace_stats` 空 | 吞错改记录；no-op/stats 保持并文档标注 |
| 6 | **死代码** | `_resolve_dict`（无调用者）、`_resolve_model` 回退链（被默认值短路） | 删除或接通回退链 |

## 讨论记录

2026-08-31, Asher & Claude.

- 与 #010 的分界：**机制（能不能接）vs 实现（内置好不好）**。注册表让你"能换一个实现"，
  但不修"内置实现不够好"；本 issue 修后者。
- 优先级排序（按对宿主真实影响）：#2（主力推理模型返回空）> #1（中文检索失效）>
  #4（跨 provider 塞错 key，错了还不报）> #3/#5/#6（一致性/卫生）。
