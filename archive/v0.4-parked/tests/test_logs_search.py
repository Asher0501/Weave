"""ConversationLog / Search 参考实现测试（D11 / D12）。

关键是把**契约不变量**钉死，而不是测实现细节：
- 只增不改 + 按 id 幂等（重复写不产生重复条目、批内也去重）；
- tail 语义（最后 n 条按升序、n<=0 不限、无记录返回空列表而非报错）；
- **追加成本不随历史长度增长**（用 SQL 语句形状证明，而不是测耗时）；
- scope 是透明的：不同 scope 互不干扰；
- 两个实现（内存/SQLite）行为一致（替换测试）。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from weave.core.envelopes import Record, Scope
from weave.core.errors import StorageError
from weave.core.interfaces import ConversationLog, Search
from weave.logs.memory import MemoryConversationLog
from weave.logs.sqlite import SqliteConversationLog
from weave.search.keyword import KeywordSearch, NullSearch

SCOPE = Scope(namespace="test", key="history")
_WORK = Path(__file__).parent / ".work"


def _db_path() -> str:
    """工作区内建库（沙箱不允许写系统临时目录）。"""
    _WORK.mkdir(parents=True, exist_ok=True)
    return str(_WORK / f"log_{uuid.uuid4().hex}.db")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(params=["memory", "sqlite"])
def log(request) -> ConversationLog:
    if request.param == "memory":
        return MemoryConversationLog()
    return SqliteConversationLog(_db_path())


def rec(text: str, kind: str = "message") -> Record:
    return Record(kind=kind, payload={"content": text})


# ── 契约不变量 ──────────────────────────────────────────


def test_append_then_tail_roundtrip(log):
    records = [rec(f"m{i}") for i in range(3)]
    run(log.append(SCOPE, records))
    got = run(log.tail(SCOPE, 10))
    assert [r.payload["content"] for r in got] == ["m0", "m1", "m2"]


def test_append_is_idempotent_across_calls_and_within_batch(log):
    a = rec("same")
    run(log.append(SCOPE, [a]))
    run(log.append(SCOPE, [a]))              # 跨调用重复
    run(log.append(SCOPE, [a, a]))            # 批内重复
    assert len(run(log.tail(SCOPE, 0))) == 1


def test_tail_semantics(log):
    run(log.append(SCOPE, [rec(f"m{i}") for i in range(5)]))
    assert [r.payload["content"] for r in run(log.tail(SCOPE, 2))] == ["m3", "m4"]
    assert len(run(log.tail(SCOPE, 0))) == 5          # n<=0 视为不限
    assert len(run(log.tail(SCOPE, -1))) == 5
    assert run(log.tail(Scope("empty"), 5)) == []      # 无记录 = 空列表，不是错误


def test_scope_is_opaque_and_isolating(log):
    run(log.append(Scope("a"), [rec("in-a")]))
    run(log.append(Scope("b"), [rec("in-b")]))
    assert [r.payload["content"] for r in run(log.tail(Scope("a"), 0))] == ["in-a"]
    assert [r.payload["content"] for r in run(log.tail(Scope("b"), 0))] == ["in-b"]


def test_same_id_in_different_scopes_is_kept_separately(log):
    shared = rec("shared")
    run(log.append(Scope("a"), [shared]))
    run(log.append(Scope("b"), [shared]))
    assert len(run(log.tail(Scope("a"), 0))) == 1
    assert len(run(log.tail(Scope("b"), 0))) == 1


def test_metadata_and_kind_survive_roundtrip(log):
    item = Record(kind="observation", payload={"ok": True}, meta={"tool": "read_file"})
    run(log.append(SCOPE, [item]))
    got = run(log.tail(SCOPE, 1))[0]
    assert got.kind == "observation"
    assert got.payload == {"ok": True}
    assert got.meta == {"tool": "read_file"}
    assert got.id == item.id


# ── 性能契约：追加成本不随历史增长（D11 的核心） ──────────


def test_sqlite_append_does_not_rewrite_history():
    """结构性证明：append 只做 INSERT，从不 UPDATE 既有行。

    旧形态（KV append）必须在每次追加时把整段历史写回去，这就是 O(N²) 的来源。
    """
    log = SqliteConversationLog(_db_path())
    statements: list[str] = []
    log._conn.set_trace_callback(statements.append)

    def data_writes(chunk: list[str]) -> list[str]:
        return [
            s for s in chunk
            if s.strip().upper().startswith(("INSERT", "UPDATE"))
        ]

    run(log.append(SCOPE, [rec("first")]))
    first = data_writes(statements)
    assert len(first) == 1, f"首次追加应只有一条 INSERT，实际：{first}"

    mark = len(statements)
    run(log.append(SCOPE, [rec("second")]))
    second = data_writes(statements[mark:])
    assert len(second) == 1, f"第二次追加应只有一条 INSERT，实际：{second}"
    assert all("INSERT" in s.upper() for s in first + second), "不得出现 UPDATE（那意味着重写历史）"

    # 关键对照：历史涨到 200 条后，单次追加的数据写入数**仍然是 1**
    for i in range(200):
        run(log.append(SCOPE, [rec(f"filler-{i}")]))
    mark = len(statements)
    run(log.append(SCOPE, [rec("after-200")]))
    tail_writes = data_writes(statements[mark:])
    assert len(tail_writes) == 1, (
        f"追加成本随历史增长（这是 D11 要消除的 O(N²)）：{len(tail_writes)} 条写入"
    )
    log.close()


def test_memory_log_keeps_all_records():
    log = MemoryConversationLog()
    for i in range(200):
        run(log.append(SCOPE, [rec(f"m{i}")]))
    assert len(run(log.tail(SCOPE, 200))) == 200


# ── Search（L1 关键词） ─────────────────────────────────


def test_keyword_search_finds_by_word_hits():
    log = MemoryConversationLog()
    run(log.append(SCOPE, [
        rec("我们需要讨论 AI Agent 项目的求职竞争力"),
        rec("今天天气不错"),
        rec("Agent 项目的评审口径"),
    ]))
    search = KeywordSearch(log)
    hits = run(search.search(SCOPE, "Agent 项目", 5))
    assert len(hits) == 2
    assert all("Agent" in h.payload["content"] for h in hits)


def test_keyword_search_ranks_by_hit_count_then_recency():
    log = MemoryConversationLog()
    run(log.append(SCOPE, [
        rec("agent"),
        rec("agent project"),
        rec("agent project 求职"),
    ]))
    hits = run(KeywordSearch(log).search(SCOPE, "agent project 求职", 3))
    assert hits[0].payload["content"] == "agent project 求职"
    assert len(hits) <= 3


def test_keyword_search_edge_cases():
    log = MemoryConversationLog()
    search = KeywordSearch(log)
    assert run(search.search(SCOPE, "   ", 3)) == []      # 空查询
    assert run(search.search(SCOPE, "nothing", 3)) == []  # 无命中是空列表
    assert run(NullSearch().search(SCOPE, "x", 3)) == []


def test_keyword_search_is_a_search_atom():
    assert isinstance(KeywordSearch(MemoryConversationLog()), Search)
    assert isinstance(MemoryConversationLog(), ConversationLog)


def test_sqlite_log_raises_storage_error_on_broken_handle():
    log = SqliteConversationLog(_db_path())
    log.close()
    with pytest.raises(StorageError):
        run(log.append(SCOPE, [rec("after close")]))
    with pytest.raises(StorageError):
        run(log.tail(SCOPE, 1))
