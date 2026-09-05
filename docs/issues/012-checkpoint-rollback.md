# Issue #012: 状态回滚（Checkpoint / Rollback）

## 结论

| 维度 | 决策 |
|------|------|
| **范围** | 第一层 Memory 认知状态回滚（纯非侵入）；第二层 tool 副作用回滚只出约定、不实现 |
| **回滚语义** | fork（快照 append-only，回滚不删快照，可再滚回） |
| **触发时机** | 默认 `after_each_tool`，可配 `after_each_llm` / `manual` |
| **存储** | 复用最窄 scope 的 state namespace + `__checkpoint_` key 前缀（跟随宿主 backend/path） |
| **配置开关** | `checkpoint.enabled` 默认 false（零开销，向后兼容） |

## 背景

Weave 定位"只做 Loop + Memory"。回滚本质是 **Memory 的时间维度快照**，落在职责边界内。

对比同类：
- AgentGit / LangGraph：回滚要显式 state schema 或 reverse tool
- DeltaBox：OS 层 checkpoint，非 SDK 层
- **Weave 的差异**：Memory 是 `state`（键值）+ `stream`（时序流）两种模式，天然可快照——回滚认知状态零宿主成本

## 两层边界

| 层 | 内容 | 非侵入 |
|---|------|--------|
| 第一层（本期） | `state` 键值 + `stream` 时序流 | ✅ 纯非侵入，Weave 掌控 |
| 第二层（只约定） | Tool 外部副作用（删文件/发邮件） | ⚠️ reverse tool 约定，宿主配合 |

## 设计

### Checkpoint 数据结构

存到**当前最窄 scope 的 state namespace**（如 `session:s1:state`），每个快照一个 key（`__checkpoint_cp_xxx` 前缀，与宿主 state 键隔离）：

```python
{
    "id": "cp_<uuid>",
    "created_at": 1234567890.0,          # 快照时刻
    "state": {                            # 完整 state 键值快照
        "session:abc:state": {"topic": "协同过滤"},
    },
    "stream_watermark": 1234567890.0,     # 快照时刻 = stream 水位线
    "state_namespaces": [...],            # 快照时激活的 state namespaces
    "stream_namespaces": [...],           # 快照时激活的 stream namespaces
}
```

### 回滚语义（fork）

- 快照 append-only：`rollback` 不删除快照，只把 Memory 恢复到快照状态
- 回滚后再打点，原快照仍在，可反复回滚

### 关键实现点

1. **state 回滚** = 清空 namespace 再恢复快照值（`state.list` + `state.delete` + `state.set`）
2. **stream 回滚** = `stream_delete_after(watermark)`，删除 `created_at > watermark` 的条目（需后端新增此方法）
3. **watermark 用快照时刻的 `time.time()`**：快照前条目 created_at < watermark，快照后条目 created_at > watermark，精确分隔

## 配置

```yaml
checkpoint:
  enabled: false               # 默认关闭（零开销，向后兼容）
  trigger: after_each_tool     # after_each_tool | after_each_llm | manual
  keep: 10                     # 每 session 保留最近 N 个快照
```

## 公开 API

```python
weave.checkpoint()                     # 手动打点，返回 checkpoint_id
weave.checkpoints()                    # 列出当前 session 的所有快照
weave.rollback(checkpoint_id=None)     # 回滚到指定快照（默认最近一个）
```

## 第二层：reverse tool 约定（不实现）

宿主声明有副作用的 tool 时，可提供逆操作：

```python
@weave.tool
def delete_file(path: str): ...

@weave.tool(reverse="undelete_file")   # 约定：声明逆操作 tool 名
def delete_file(path: str): ...
```

Weave 只记录约定、不在回滚时自动调用 reverse tool——因为外部副作用不是 Loop + Memory 的职责。宿主若需环境回滚，自行在 `rollback()` 前后调用 reverse tool。

## 实现清单

- [ ] `types.py`: `CheckpointConfig` + `WeaveConfig.checkpoint`
- [ ] `config.py`: 解析 `checkpoint:` 段
- [ ] `backends/sqlite.py` + `file.py`: `stream_delete_after(watermark, namespace)`
- [ ] `memory/stream.py`: facade 暴露 `delete_after`
- [ ] `weave/checkpoint.py`: `CheckpointManager`
- [ ] `agent.py`: 创建 CheckpointManager + 暴露 `checkpoint()/checkpoints()/rollback()`
- [ ] `loop/iterative.py`: `after_each_tool` 触发自动打点
- [ ] `tests/`: 快照/回滚/fork/跨 run 持久化/自动打点
- [ ] `docs/public-api.md`: 新增公开 API

## 讨论记录

2026-09-04, Asher & Claude.

- 上游调研：AgentGit/LangGraph 的 checkpoint、AgentRewind 的 aligned checkpoint、DeltaBox 的 OS 层快照
- 边界：第一层 Memory 回滚（非侵入）vs 第二层 tool 副作用（reverse tool 约定）
- 决策：fork 语义、after_each_tool 触发、复用 state 模式存 checkpoint scope
