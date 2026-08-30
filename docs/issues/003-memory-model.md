# Issue #003: Memory 模型设计

## 结论

| 维度 | 决策 |
|------|------|
| **时间维度** | 不作为 Memory 类型，改为 TTL 配置（每条记忆独立设置过期时间） |
| **作用域** | 由接入项目自定义（Weave 不内置 system/project/session） |
| **访问模式** | stream / state / knowledge 三种接口 |
| **隔离机制** | namespace 前缀，单表行级隔离 |

## 访问模式

### stream — 时序消息流

```python
memory.stream.append({"role": "user", "content": "我不懂 SVD"})
memory.stream.last(20)  # → 按时间倒序取最近 N 条
```

- 追加写入，顺序读取
- 超出窗口自动裁剪
- 对应 myKG `chat_history` / bePM `messages.json`

### state — 键值状态

```python
memory.state.set("current_topic", "协同过滤")
memory.state.get("current_topic")  # → "协同过滤"
memory.state.set("current_topic", "矩阵分解")  # 覆盖旧值
```

- 按 key 精确读写，新值覆盖旧值
- 对应 myKG `self._sessions[sid]` dict / bePM `project.json` 进度字段

### knowledge — 知识搜索

```python
memory.knowledge.search("冷启动问题")
# → [{"content": "...", "score": 0.92}, ...]
memory.knowledge.add({"content": "新发现..."})  # 追加，不覆盖
```

- 语义/全文搜索
- 对应 myKG `kb.json` / bePM 不存在（跨项目学习缺失）

### 三种访问模式的底层区别

| | stream | state | knowledge |
|---|---|---|---|
| SQL | `ORDER BY ... LIMIT` | `WHERE key=?` | `... MATCH '...'` |
| 写入 | 追加 | 覆盖同名 key | 追加 |
| 读取 | 时序窗口 | 精确 key | 模糊搜索 |
| 容量 | 滑动窗口裁剪 | 无限制 | 无限制 |

## 时间维度

不作为 Memory 类型，改为 TTL 配置：

```yaml
memory:
  scopes:
    chat_session:
      stream:
        backend: sqlite
        ttl: 3600          # 1 小时后数据过期
      state:
        ttl: 3600
    project:
      state:
        ttl: null           # 永不过期
      knowledge:
        ttl: null
```

## 作用域由项目自定义

Weave 不内置 system/project/session 层级。接入项目在 `weave.yaml` 中定义自己的作用域：

```yaml
# myKG 的作用域定义
memory:
  scopes:
    kb_global:            # 全局知识库
      state: { backend: sqlite }
      knowledge: { backend: chromadb }
    domain:               # 一个学习领域
      state: { backend: sqlite }
      knowledge: { backend: sqlite, fts: true }
    quiz_session:         # 一次 quiz
      stream: { backend: sqlite, ttl: 3600 }
      state: { ttl: 3600 }
```

```yaml
# bePM 的作用域定义
memory:
  scopes:
    workspace:            # 跨项目工作空间
      knowledge: { backend: chromadb }
    project:              # 单个项目
      state: { backend: sqlite }
      knowledge: { backend: sqlite, fts: true }
    edit_session:         # 一次编辑操作
      stream: { backend: sqlite, ttl: 300 }
```

## Namespace 隔离实现

所有数据存在同一张表，通过 namespace 列行级隔离：

```sql
CREATE TABLE memory_entries (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    access_type TEXT NOT NULL,       -- stream / state / knowledge
    key TEXT,
    content TEXT NOT NULL,
    metadata JSON,
    created_at REAL NOT NULL,
    expires_at REAL                  -- NULL = 永不过期
);

CREATE INDEX idx_ns_at ON memory_entries(namespace, access_type);
CREATE INDEX idx_ns_key ON memory_entries(namespace, access_type, key);
```

Namespace 格式：`{scope_name}:{scope_id}:{access_type}`

```
例：
  quiz_session:abc123:stream      → session abc123 的对话流
  domain:recsys:state             → "推荐系统" 领域的状态
  kb_global:default:knowledge     → 全局知识库
```

## 一次 run() 的 Memory 加载流程

```python
# Weave 内部
namespaces = [
    f"{scope}:{sid}:{access_type}"
    for scope, sid in self._active_scopes  # 由 weave.run(scope_hints=...) 决定
    for access_type in ["stream", "state", "knowledge"]
]

# 1. 并行加载所有激活的 namespace
contexts = await self.memory.load_context(namespaces)

# 2. 拼入 LLM prompt
# stream contexts  → 对话历史
# state contexts   → 当前状态
# knowledge contexts + user_query → 语义搜索结果
```

## 讨论记录

2026-07-29, Asher & Claude.

### Q: SQLite 保存的是记忆上下文？记忆分层如何实现？
不是只保存对话历史。通过 namespace 前缀实现 system/project/session 三级行级隔离。

### Q: 作用域和时间维度的区别？
正交的。作用域是 namespace 隔离，时间维度通过 access_type（stream/state/knowledge）+ TTL 配置实现。

### Q: system 级 Working 和 Long-term 有区别吗？
区别不在生命周期，在访问模式——Working 是 key-value 精确读，Long-term 是语义搜索。因此改名为 state 和 knowledge。

### Q: stream/state/knowledge 的区别？
- stream: 时序列表，追加写入，按时间顺序取最近 N 条
- state: 键值对，精确 key 读写，新值覆盖旧值
- knowledge: 搜索，追加不覆盖，语义/全文检索
