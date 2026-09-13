"""SQLiteStateStore — SQLite 参考实现（单机持久，WAL + synchronous=NORMAL）。

值按 JSON 文本存储于单表（namespace, key）行级隔离。

性能/并发模型（对齐 v0.3 基线 + QG-5）：
- 操作**同步内联**执行（无 asyncio.to_thread、无锁）：每个方法内部无 await，
  事件循环上原子完成，微秒~亚毫秒级；并发中继在 await 点交错，不互相阻塞；
- WAL + ``synchronous=NORMAL``：提交不逐次 fsync，写延迟与并发尾部低（异常断电
  最多丢最近提交——会话记录类数据可接受）；
- 跨进程并发由 SQLite 自身（busy_timeout + WAL 写者串行）保证；多进程高并发
  场景请换成真正的服务端存储（Redis 参考实现已归档到 archive/v0.4-parked/）。

注意：单个调用最长可能阻塞到 busy_timeout（默认 5s），仅当与其他进程写同一
库文件且写锁被占时发生；单进程多实例（如 agora 每 run 一实例）无此问题。
"""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

from weave.core.interfaces import StateStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""


class SQLiteStateStore(StateStore):
    """KV 存储（SQLite）：get / set / delete / append + close。"""

    def __init__(self, path: str = "./data/memory.db") -> None:
        self._path = str(path)
        p = Path(self._path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── 内部（同步内联） ─────────────────────────────

    def _run(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _decode(text: str) -> Any:
        return json.loads(text)

    def close(self) -> None:
        self._conn.close()

    # ── StateStore ────────────────────────────────────

    async def get(self, namespace: str, key: str) -> Any | None:
        rows = self._run(
            "SELECT value FROM entries WHERE namespace=? AND key=?", (namespace, key)
        )
        if not rows:
            return None
        return copy.deepcopy(self._decode(rows[0]["value"]))

    async def set(self, namespace: str, key: str, value: Any) -> None:
        encoded = self._encode(value)
        self._execute(
            "INSERT INTO entries(namespace,key,value) VALUES(?,?,?) "
            "ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",
            (namespace, key, encoded),
        )

    async def delete(self, namespace: str, key: str) -> None:
        self._execute(
            "DELETE FROM entries WHERE namespace=? AND key=?",
            (namespace, key),
        )

    async def append(self, namespace: str, key: str, value: Any) -> None:
        rows = self._run(
            "SELECT value FROM entries WHERE namespace=? AND key=?", (namespace, key)
        )
        if not rows:
            self._execute(
                "INSERT INTO entries(namespace,key,value) VALUES(?,?,?)",
                (namespace, key, self._encode([value])),
            )
            return
        existing = self._decode(rows[0]["value"])
        if not isinstance(existing, list):
            raise ValueError(
                f"append: key {key!r} in namespace {namespace!r} is not a list"
            )
        existing.append(value)
        self._execute(
            "UPDATE entries SET value=? WHERE namespace=? AND key=?",
            (self._encode(existing), namespace, key),
        )

__all__ = ["SQLiteStateStore"]
