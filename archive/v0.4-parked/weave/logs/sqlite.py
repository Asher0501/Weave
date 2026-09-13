"""SqliteConversationLog — 追加日志的 SQLite 参考实现（D11）。

为什么它是**行级表**而不是"往 KV 的一个 key 里塞列表"：
KV 的 append 必然要"读整行 → 改列表 → 写整行"，第 n 次追加重写 n 条记录，
累计 O(N²)。这里一条记录一行，追加是 O(1)、取尾是索引范围扫描。

并发模型与 SQLiteStateStore 保持一致：操作同步内联（方法内部无 await）、
WAL + synchronous=NORMAL + busy_timeout。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from weave.core.envelopes import Record, Scope
from weave.core.errors import StorageError
from weave.core.interfaces import ConversationLog

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    id         TEXT NOT NULL,
    created_at REAL NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    meta       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS conversation_log_dedupe
    ON conversation_log(namespace, key, id);
CREATE INDEX IF NOT EXISTS conversation_log_tail
    ON conversation_log(namespace, key, seq);
"""


class SqliteConversationLog(ConversationLog):
    """append-only 会话日志（SQLite）。"""

    def __init__(self, path: str = "./data/memory.db") -> None:
        self._path = str(path)
        p = Path(self._path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - 环境相关
            raise StorageError(f"ConversationLog 打不开 {self._path}: {exc}") from exc

    def close(self) -> None:
        self._conn.close()

    # ── ConversationLog ──────────────────────────────

    async def append(self, scope: Scope, records: Sequence[Record]) -> None:
        """一条记录一行；`INSERT OR IGNORE` + 唯一索引 = 幂等，且成本与历史长度无关。"""
        if not records:
            return
        rows = [
            (
                scope.namespace,
                scope.key,
                rec.id,
                float(rec.created_at),
                rec.kind,
                json.dumps(rec.payload, ensure_ascii=False, default=str),
                json.dumps(rec.meta, ensure_ascii=False, default=str),
            )
            for rec in records
        ]
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO conversation_log"
                "(namespace,key,id,created_at,kind,payload,meta) VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"ConversationLog.append 失败: {exc}") from exc

    async def tail(self, scope: Scope, n: int) -> list[Record]:
        """最后 n 条（按时间升序）；`n <= 0` 视为不限；无记录返回空列表。"""
        try:
            if n and n > 0:
                rows = self._conn.execute(
                    "SELECT * FROM conversation_log WHERE namespace=? AND key=? "
                    "ORDER BY seq DESC LIMIT ?",
                    (scope.namespace, scope.key, int(n)),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = self._conn.execute(
                    "SELECT * FROM conversation_log WHERE namespace=? AND key=? "
                    "ORDER BY seq ASC",
                    (scope.namespace, scope.key),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"ConversationLog.tail 失败: {exc}") from exc
        return [self._to_record(r) for r in rows]

    # ── 内部 ─────────────────────────────────────────

    @staticmethod
    def _to_record(row: sqlite3.Row) -> Record:
        return Record(
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            id=row["id"],
            meta=json.loads(row["meta"]),
        )
