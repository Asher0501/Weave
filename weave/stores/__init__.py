"""weave.stores — StateStore 参考实现（KV 存储）。

- `SQLiteStateStore`：单机持久（WAL + synchronous=NORMAL）
- `InMemoryStateStore`：进程内（测试、短会话）

两个实现都只做契约四方法：get / set / delete / append（SQLite 另有 close 释放文件）。
检索、裁剪、枚举等能力已随对应接口一起归档到 `archive/v0.4-parked/`。
"""
from weave.stores.memory import InMemoryStateStore
from weave.stores.sqlite import SQLiteStateStore

__all__ = ["InMemoryStateStore", "SQLiteStateStore"]
