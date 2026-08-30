# Issue #006: Knowledge 检索对 CJK 失效（及 memory 读接口可用性）

## 问题

SQLite 的 knowledge「语义搜索」实为 `query.split()` + `content LIKE %term%` 的子串匹配。中文无空格，整句被当作单个 term：`"客户投诉支付成功但订单未生成"` 永远搜不到 `"客户投诉：支付成功但订单未生成"`（仅差一个冒号）。

## 现状

- `weave/memory/backends/sqlite.py:243-284`：`knowledge_search` 用 `query.split()` 得 terms，逐个 `content LIKE ?`（`%term%`）OR 连接；`_simple_score` 同理按空格切词。
- `weave/memory/knowledge.py`：只有 `add` / `search`，无枚举接口。

## 影响

- 无空格语言（中文/日文）下 knowledge 检索基本退化——`before_think` 的 `knowledge.search(current_input, ...)` 命中率极低，共享知识无法注入。demo 原本用 knowledge 存共享客观事实，实测检索为空，被迫改用 `state`（`state.get_all` 全量注入）才实现「共享可见」。
- 管理面（如做隔离报告）读不回已播种的 knowledge，只能宿主自己缓存一份。

## 建议

1. SQLite 后端启用 FTS5 + 合适分词（中文用 trigram 或 simple 分词），或对 CJK 做 n-gram 兜底；至少文档明确「LIKE 检索对无空格语言的能力边界」。
2. 文档补充指引：需要「全量注入」的共享上下文用 `state`，需要「按相关性检索」的用 `knowledge`（chroma 后端才是真语义搜索）。
3. 增加 `knowledge.list(namespace)`（或 `get_all`）便于调试/管理面枚举。

## 附记（次要）

- `weave/memory/manager.py:222` 的 `get_namespace` 依赖先 `activate_scopes`（改写 `_active_scopes` 共享状态）才可读，纯读一个 namespace 也有副作用。可加纯函数 `namespace_for(scope_name, scope_id, access_type)`。

## 讨论记录

2026-08-30, Asher & Claude.
