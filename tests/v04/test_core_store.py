"""契约与 InMemory StateStore 的冒烟测试（业务无关面）。"""
import asyncio

from weave.core import StateStore, classify_http_error
from weave.core.errors import (
    AuthError,
    ProviderRejectedError,
    RateLimitError,
    ServerError,
)
from weave.stores.memory import InMemoryStateStore


def run(coro):
    return asyncio.run(coro)


def test_state_store_crud():
    store = InMemoryStateStore()

    async def _t():
        assert await store.get("agent:a1:state", "k1") is None
        await store.set("agent:a1:state", "k1", {"v": 1})
        assert await store.get("agent:a1:state", "k1") == {"v": 1}
        # 值隔离：外部修改不影响存储
        got = await store.get("agent:a1:state", "k1")
        got["v"] = 999
        assert (await store.get("agent:a1:state", "k1")) == {"v": 1}
        await store.delete("agent:a1:state", "k1")
        assert await store.get("agent:a1:state", "k1") is None

    run(_t())


def test_append_ordered_list():
    store = InMemoryStateStore()

    async def _t():
        ns = "agent:a1:stream"
        for msg in ("hi", "hello", "bye"):
            await store.append(ns, "messages", {"role": "user", "content": msg})
        msgs = await store.get(ns, "messages")
        assert [m["content"] for m in msgs] == ["hi", "hello", "bye"]

    run(_t())


def test_store_only_exposes_the_four_contract_methods():
    """最狠的收缩：检索/裁剪/枚举都不再实现（要这些能力就从归档取回）。"""
    store = InMemoryStateStore()
    assert issubclass(InMemoryStateStore, StateStore)
    for gone in ("search", "trim", "list_namespaces", "list_keys"):
        assert not hasattr(store, gone), f"{gone} 应已随接口一起归档"


def test_namespace_is_opaque_to_store():
    store = InMemoryStateStore()

    async def _t():
        await store.set("完全:自定义:命名", "k", 1)
        assert await store.get("完全:自定义:命名", "k") == 1

    run(_t())


def test_classify_http_error():
    assert isinstance(classify_http_error(429), RateLimitError)
    assert isinstance(classify_http_error(503), ServerError)
    assert isinstance(classify_http_error(401), AuthError)
    assert isinstance(classify_http_error(402), ProviderRejectedError)
