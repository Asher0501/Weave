"""KeywordSearch — 最小检索实现（D12 的 L1 形态）。

只承诺「问题 → 相关记录」：不引入任何模型调用，纯词面匹配。
它的存在意义是**证明检索这一格可用**，而不是提供最好的检索：
真需要语义召回时，换一个 `Search` 实现即可（策略层与编排层零改动）。

成本说明：每次检索读取该 scope 的全部记录（`tail(scope, 0)`）。
这是 L1（关键词）的合理实现；L2/L3 的实现应自带索引。
"""
from __future__ import annotations

import re

from weave.core.envelopes import Record, Scope
from weave.core.interfaces import ConversationLog, Search

_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text or "")}


def _record_text(rec: Record) -> str:
    payload = rec.payload
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(str(v) for v in payload.values())
    return str(payload)


class KeywordSearch(Search):
    """词面检索：命中词数 → 得分，得分相同则最近的优先。"""

    def __init__(self, log: ConversationLog) -> None:
        self._log = log

    async def search(self, scope: Scope, query: str, k: int) -> list[Record]:
        wanted = _tokens(query)
        if not wanted:
            return []
        records = await self._log.tail(scope, 0)      # 0 = 不限
        scored: list[tuple[int, int, Record]] = []
        for position, rec in enumerate(records):
            hits = len(wanted & _tokens(_record_text(rec)))
            if hits:
                scored.append((hits, position, rec))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        limit = k if k and k > 0 else len(scored)
        return [rec for _, _, rec in scored[:limit]]


class NullSearch(Search):
    """空检索：什么都没实现时的占位（返回空，不报错）。"""

    async def search(self, scope: Scope, query: str, k: int) -> list[Record]:
        return []
