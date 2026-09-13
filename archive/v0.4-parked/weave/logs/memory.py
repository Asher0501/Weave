"""MemoryConversationLog — 追加日志的内存参考实现（测试/短会话）。

语义与 `SqliteConversationLog` 完全一致（只增不改、按 id 幂等、tail 语义相同），
用于替换测试：换实现不换行为。
"""
from __future__ import annotations

from collections.abc import Sequence

from weave.core.envelopes import Record, Scope
from weave.core.interfaces import ConversationLog


class MemoryConversationLog(ConversationLog):
    """进程内 append-only 日志。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], list[Record]] = {}
        self._ids: dict[tuple[str, str], set[str]] = {}

    async def append(self, scope: Scope, records: Sequence[Record]) -> None:
        bucket_key = (scope.namespace, scope.key)
        rows = self._rows.setdefault(bucket_key, [])
        known = self._ids.setdefault(bucket_key, set())
        for rec in records:                     # 批内与历史都按 id 去重（幂等）
            if rec.id not in known:
                known.add(rec.id)
                rows.append(rec)

    async def tail(self, scope: Scope, n: int) -> list[Record]:
        rows = self._rows.get((scope.namespace, scope.key), [])
        if n and n > 0:
            return list(rows[-int(n):])
        return list(rows)
