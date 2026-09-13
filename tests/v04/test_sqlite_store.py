"""SQLiteStateStore 测试：契约四方法 + 持久化。"""
import asyncio
import uuid
from pathlib import Path

from weave.stores.sqlite import SQLiteStateStore

_WORK = Path(__file__).parent / ".work"


def _db_path() -> str:
    _WORK.mkdir(exist_ok=True)
    return str(_WORK / f"test_{uuid.uuid4().hex}.db")


def test_sqlite_crud_and_append():
    store = SQLiteStateStore(_db_path())
    try:

        async def _t():
            await store.set("agent:a1:state", "k", {"v": 1})
            assert await store.get("agent:a1:state", "k") == {"v": 1}
            ns = "agent:a1:stream"
            for i in range(4):
                await store.append(ns, "messages", i)
            assert await store.get(ns, "messages") == [0, 1, 2, 3]
            await store.delete("agent:a1:state", "k")
            assert await store.get("agent:a1:state", "k") is None

        asyncio.run(_t())
    finally:
        store.close()


def test_sqlite_data_survives_reopen():
    path = _db_path()
    first = SQLiteStateStore(path)
    asyncio.run(first.set("ns", "k", {"v": "持久"}))
    first.close()

    second = SQLiteStateStore(path)
    try:
        assert asyncio.run(second.get("ns", "k")) == {"v": "持久"}
    finally:
        second.close()


def test_append_on_non_list_raises():
    store = SQLiteStateStore(_db_path())
    try:
        asyncio.run(store.set("ns", "k", 1))
        try:
            asyncio.run(store.append("ns", "k", 2))
        except ValueError as exc:
            assert "not a list" in str(exc)
        else:                                   # pragma: no cover
            raise AssertionError("向非列表 append 应当报错")
    finally:
        store.close()
