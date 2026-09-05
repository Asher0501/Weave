"""SQLite 存储后端。

所有 Memory 类型的默认后端。
单文件 SQLite 数据库，WAL 模式，零额外依赖。

Schema:
    CREATE TABLE memory_entries (
        id TEXT PRIMARY KEY,
        namespace TEXT NOT NULL,
        access_type TEXT NOT NULL,  -- 有意冗余：方便索引和按类型过滤
        key TEXT,
        content TEXT NOT NULL,
        metadata TEXT DEFAULT '{}',
        created_at REAL NOT NULL,
        expires_at REAL             -- NULL = 永不过期
    );
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from weave_agent_sdk.types import MemoryEntry, SearchResult
from weave_agent_sdk.utils.search import search_tokens

# ── Schema ────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    access_type TEXT NOT NULL,
    key TEXT,
    content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    expires_at REAL
);
"""

INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_ns_at ON memory_entries(namespace, access_type);",
    "CREATE INDEX IF NOT EXISTS idx_ns_key ON memory_entries(namespace, access_type, key);",
    "CREATE INDEX IF NOT EXISTS idx_ns_expires ON memory_entries(namespace, expires_at);",
]


class SQLiteBackend:
    """SQLite 存储后端，被 StreamMemory / StateMemory / KnowledgeMemory 共享。"""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute(CREATE_TABLE_SQL)
            for idx_sql in INDEXES_SQL:
                self._conn.execute(idx_sql)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Stream ────────────────────────────────────────

    def stream_append(self, entry: dict[str, Any], namespace: str, ttl: float | None = None) -> str:
        entry_id = str(uuid.uuid4())
        now = time.time()
        content = json.dumps(entry, ensure_ascii=False)
        # TTL：scope 配置了 ttl 时计算 expires_at=created_at+ttl，否则永不过期
        # （NULL）。此前 ttl 被静默丢弃、写入从不设 expires_at，记忆永不过期、
        # DB 无界增长（review round-4 issue 1）。
        expires_at = (now + ttl) if ttl else None

        self.conn.execute(
            """INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at)
               VALUES (?, ?, 'stream', NULL, ?, ?, ?)""",
            (entry_id, namespace, content, now, expires_at),
        )
        self.conn.commit()
        # 写路径被动清理过期条目（cleanup_expired 的 docstring 语义"写入时调用"），
        # 保证过期数据随写入被回收，DB 不无界增长（review round-4 issue 1）。
        self.cleanup_expired()
        return entry_id

    def stream_last(self, n: int, namespaces: list[str] | None) -> list[dict[str, Any]]:
        """查询最近的 n 条 stream 记录。

        返回的内容 dict 中会包含 _created_at 内部字段，用于跨 backend 合并时排序。
        调用方（MemoryStream.last）在跨 backend 合并后会清理该字段。
        """
        if not namespaces:
            namespaces = self._all_stream_namespaces()

        if not namespaces:
            return []

        placeholders = ",".join("?" * len(namespaces))
        now = time.time()

        cursor = self.conn.execute(
            f"""SELECT content, created_at FROM memory_entries
                WHERE namespace IN ({placeholders})
                  AND access_type = 'stream'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                LIMIT ?""",
            (*namespaces, now, n),
        )
        rows = cursor.fetchall()
        # 反转以恢复时间顺序（最旧在前），并携带 created_at 供跨 backend 合并
        result = []
        for r in reversed(rows):
            entry = json.loads(r[0])
            entry["_created_at"] = r[1]  # 内部字段：用于跨 backend 按时间排序
            result.append(entry)
        return result

    def stream_trim(self, max_items: int, namespace: str) -> None:
        """保留最近 max_items 条（max_items 优先于 TTL）。"""
        cursor = self.conn.execute(
            """SELECT id FROM memory_entries
                WHERE namespace = ? AND access_type = 'stream'
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?""",
            (namespace, max_items),
        )
        ids_to_delete = [r[0] for r in cursor.fetchall()]
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            self.conn.execute(
                f"DELETE FROM memory_entries WHERE id IN ({placeholders})",
                ids_to_delete,
            )
            self.conn.commit()

    def stream_delete_after(self, watermark: float, namespace: str) -> int:
        """删除 created_at > watermark 的 stream 条目（用于状态回滚）。

        Args:
            watermark: 时间水位线（快照时刻的 time.time()）。快照前条目的
                created_at 均小于 watermark，快照后条目的 created_at 均大于
                watermark，据此精确分隔"快照后新增"的条目。
            namespace: 目标 namespace

        Returns:
            删除的条数
        """
        cursor = self.conn.execute(
            """DELETE FROM memory_entries
                WHERE namespace = ? AND access_type = 'stream'
                  AND created_at > ?""",
            (namespace, watermark),
        )
        self.conn.commit()
        return cursor.rowcount

    # ── State ─────────────────────────────────────────

    def state_get(self, key: str, namespace: str) -> Any | None:
        now = time.time()
        cursor = self.conn.execute(
            """SELECT content FROM memory_entries
                WHERE namespace = ? AND access_type = 'state' AND key = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                LIMIT 1""",
            (namespace, key, now),
        )
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None

    def state_set(self, key: str, value: Any, namespace: str, ttl: float | None = None) -> str:
        now = time.time()
        content = json.dumps(value, ensure_ascii=False)
        # TTL：scope 配置了 ttl 时计算 expires_at=created_at+ttl，否则永不过期
        # （NULL，review round-4 issue 1）。
        expires_at = (now + ttl) if ttl else None

        # UPSERT: 删除旧值再插入
        self.conn.execute(
            """DELETE FROM memory_entries
                WHERE namespace = ? AND access_type = 'state' AND key = ?""",
            (namespace, key),
        )

        entry_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at)
               VALUES (?, ?, 'state', ?, ?, ?, ?)""",
            (entry_id, namespace, key, content, now, expires_at),
        )
        self.conn.commit()
        # 写路径被动清理过期条目（review round-4 issue 1）
        self.cleanup_expired()
        return entry_id

    def state_delete(self, key: str, namespace: str) -> None:
        self.conn.execute(
            """DELETE FROM memory_entries
                WHERE namespace = ? AND access_type = 'state' AND key = ?""",
            (namespace, key),
        )
        self.conn.commit()

    def state_get_all(self, namespaces: list[str] | None) -> dict[str, Any]:
        if not namespaces:
            namespaces = self._all_state_namespaces()

        if not namespaces:
            return {}

        now = time.time()
        placeholders = ",".join("?" * len(namespaces))
        cursor = self.conn.execute(
            f"""SELECT key, content FROM memory_entries
                WHERE namespace IN ({placeholders})
                  AND access_type = 'state'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at ASC""",
            (*namespaces, now),
        )
        result: dict[str, Any] = {}
        for key, content in cursor.fetchall():
            result[key] = json.loads(content)
        return result

    # ── Knowledge ─────────────────────────────────────

    def knowledge_add(self, content: str, namespace: str, metadata: dict[str, Any] | None = None, ttl: float | None = None) -> str:
        entry_id = str(uuid.uuid4())
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        # TTL：scope 配置了 ttl 时计算 expires_at=created_at+ttl，否则永不过期
        # （NULL，review round-4 issue 1）。
        expires_at = (now + ttl) if ttl else None

        self.conn.execute(
            """INSERT INTO memory_entries (id, namespace, access_type, key, content, metadata, created_at, expires_at)
               VALUES (?, ?, 'knowledge', NULL, ?, ?, ?, ?)""",
            (entry_id, namespace, content, meta_json, now, expires_at),
        )
        self.conn.commit()
        # 写路径被动清理过期条目（review round-4 issue 1）
        self.cleanup_expired()
        return entry_id

    def knowledge_search(self, query: str, namespaces: list[str] | None, top_k: int) -> list[SearchResult]:
        """SQLite FTS5 全文搜索。"""
        if not namespaces:
            namespaces = self._all_knowledge_namespaces()

        if not namespaces:
            return []

        now = time.time()
        placeholders = ",".join("?" * len(namespaces))

        # 使用 LIKE 做简单关键词搜索（FTS5 表需单独创建，此处为基础实现）。
        # search_tokens 对 CJK 追加 bigram，突破中文无空格导致的整句匹配失效（docs/issues/011）。
        search_terms = search_tokens(query)
        if not search_terms:
            # 空查询 / 纯空白查询：直接返回空结果，避免生成非法 SQL
            # （`AND ()` 语法错误会被 before_think 的 try/except 吞掉仅记
            #   warning，导致 knowledge 检索静默失效；scheduled 事件驱动
            #   模式下 payload 无 input（空串）每次触发都会命中此路径）。
            return []

        like_clauses = " OR ".join(["content LIKE ?"] * len(search_terms))
        like_params = [f"%{t}%" for t in search_terms]

        cursor = self.conn.execute(
            f"""SELECT id, content, metadata FROM memory_entries
                WHERE namespace IN ({placeholders})
                  AND access_type = 'knowledge'
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND ({like_clauses})
                LIMIT ?""",
            (*namespaces, now, *like_params, top_k),
        )

        results: list[SearchResult] = []
        for row in cursor.fetchall():
            results.append(SearchResult(
                id=row[0],
                content=row[1],
                score=_simple_score(query, row[1]),
                metadata=json.loads(row[2]) if row[2] else {},
            ))
        return results

    # ── Cleanup ───────────────────────────────────────

    def cleanup_expired(self, limit: int = 100) -> int:
        """被动清理过期条目（写入时调用）。

        本机 Python 内置 SQLite 未启用 SQLITE_ENABLE_UPDATE_DELETE_LIMIT，
        `DELETE ... LIMIT` 语法不受支持（sqlite3.OperationalError）。因此先
        查过期条目的 id 再按 id 删除，既保留 limit 上限（写路径清理有界），
        又兼容所有 SQLite 编译选项（review round-7 fix）。
        """
        now = time.time()
        cursor = self.conn.execute(
            """SELECT id FROM memory_entries
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                LIMIT ?""",
            (now, limit),
        )
        ids_to_delete = [r[0] for r in cursor.fetchall()]
        if not ids_to_delete:
            return 0
        placeholders = ",".join("?" * len(ids_to_delete))
        cursor = self.conn.execute(
            f"DELETE FROM memory_entries WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        self.conn.commit()
        return cursor.rowcount

    # ── Namespace helpers ─────────────────────────────

    def _all_stream_namespaces(self) -> list[str]:
        cursor = self.conn.execute(
            "SELECT DISTINCT namespace FROM memory_entries WHERE access_type = 'stream'"
        )
        return [r[0] for r in cursor.fetchall()]

    def _all_state_namespaces(self) -> list[str]:
        cursor = self.conn.execute(
            "SELECT DISTINCT namespace FROM memory_entries WHERE access_type = 'state'"
        )
        return [r[0] for r in cursor.fetchall()]

    def _all_knowledge_namespaces(self) -> list[str]:
        cursor = self.conn.execute(
            "SELECT DISTINCT namespace FROM memory_entries WHERE access_type = 'knowledge'"
        )
        return [r[0] for r in cursor.fetchall()]

    def list_namespaces(self) -> list[str]:
        cursor = self.conn.execute(
            "SELECT DISTINCT namespace FROM memory_entries"
        )
        return [r[0] for r in cursor.fetchall()]

    def namespace_stats(self) -> dict[str, int]:
        """返回各 namespace 的条目数。

        仅统计未过期条目（expires_at IS NULL 或 > now）——此前不过滤过期
        条件，已过期但未被写路径清理的条目会虚高计入 status().memory_stats
        / GET /agents/{name}/memory，与 TTL"记忆可过期"语义在管理面不一致
        （review round-6 issue 5）。
        """
        now = time.time()
        cursor = self.conn.execute(
            """SELECT namespace, COUNT(*) FROM memory_entries
                WHERE (expires_at IS NULL OR expires_at > ?)
                GROUP BY namespace""",
            (now,),
        )
        return {r[0]: r[1] for r in cursor.fetchall()}


def _simple_score(query: str, content: str) -> float:
    """简单的关键词匹配评分。"""
    terms = search_tokens(query)
    content_lower = content.lower()
    hits = sum(1 for t in terms if t.lower() in content_lower)
    return hits / len(terms) if terms else 0.0
