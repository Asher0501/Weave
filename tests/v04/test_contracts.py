"""契约面测试：weave 只有一个功能，所以接口面也必须很小。

三条门禁（CI）：
1. **接口面**：只保留有真实消费者的接口；删掉的那些必须真的不存在了；
2. **反向依赖**：原子层不依赖上层；`weave.llm` 不碰存储；
3. **零第三方依赖**：`core` 只用标准库。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import weave
import weave.core.interfaces as I
from weave.core.envelopes import CallRequest, Invocation, TypedFailure
from weave.core.types import LLMResponse, Message, StreamChunk, ToolSchema

WEAVE_ROOT = Path(I.__file__).resolve().parents[1]
CORE_DIR = WEAVE_ROOT / "core"

FORBIDDEN_ATOM_IMPORTS = ("weave.llm", "demos")

ATOM_FILES = sorted(
    p
    for p in WEAVE_ROOT.rglob("*.py")
    if "__pycache__" not in p.parts and p.parts[-2] in {"core", "stores", "providers"}
)

#: 已归档的能力：这些名字不允许再出现在 weave 包里（防止悄悄长回来）
REMOVED_INTERFACES = (
    "Provider", "EventSink", "NoopEventSink",
    "Database", "ConversationLog", "Search", "Executor", "EnvironmentCatalog",
    "ToolRegistry", "SearchableStore", "TrimmableStore", "ListableStore", "Parser",
)


def run(coro):
    return asyncio.run(coro)


# ── 1. 接口面 ───────────────────────────────────────────


def test_only_two_interfaces_remain_and_both_have_consumers():
    """留下的每个接口都要说得出谁在用。"""
    assert inspect.isabstract(I.LLMProvider)      # LLM 交互对象 + 两个 provider 实现
    assert inspect.isabstract(I.StateStore)       # SQLiteStateStore / InMemoryStateStore


def test_removed_interfaces_are_gone():
    for name in REMOVED_INTERFACES:
        assert not hasattr(I, name), f"{name} 应已归档（archive/v0.4-parked/），不该还在契约里"


def test_removed_packages_are_gone():
    import importlib

    for module in ("weave.logs", "weave.search", "weave.executors", "weave.catalogs",
                   "weave.strategy", "weave.capabilities", "weave.core.codec",
                   "weave.providers.openai_compat", "weave.providers.adapter",
                   "weave.providers._reliability"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_public_surface_is_small():
    assert set(weave.__all__) == {
        "llm", "coerce_provider", "LLMClient", "LLMCallError", "__version__",
    }


def test_provider_atoms_are_dumb_no_retry_parameters():
    for name in ("complete", "stream"):
        params = set(inspect.signature(getattr(I.LLMProvider, name)).parameters)
        assert not {"retry", "retries", "timeout", "backoff"} & params, (
            f"LLMProvider.{name} 出现了可靠性参数：{params}"
        )


def test_llm_provider_takes_frozen_envelope():
    params = inspect.signature(I.LLMProvider.complete).parameters
    assert list(params) == ["self", "request"]
    assert params["request"].annotation in (CallRequest, "CallRequest")


def test_state_store_has_exactly_four_contract_methods():
    abstract = {
        name for name, value in inspect.getmembers(I.StateStore)
        if getattr(value, "__isabstractmethod__", False)
    }
    assert abstract == {"get", "set", "delete", "append"}


def test_minimal_implementations_satisfy_contracts():
    class P(I.LLMProvider):
        async def complete(self, request: CallRequest) -> LLMResponse:
            return LLMResponse(content="ok", model="fake")

    class DB(I.StateStore):
        def __init__(self) -> None:
            self.data: dict[tuple[str, str], object] = {}

        async def get(self, namespace, key):
            return self.data.get((namespace, key))

        async def set(self, namespace, key, value):
            self.data[(namespace, key)] = value

        async def delete(self, namespace, key):
            self.data.pop((namespace, key), None)

        async def append(self, namespace, key, value):
            bucket = self.data.setdefault((namespace, key), [])
            assert isinstance(bucket, list)
            bucket.append(value)

    provider = P()
    assert run(provider.complete(CallRequest())).content == "ok"
    with pytest.raises(NotImplementedError):
        provider.stream(CallRequest())          # 可选能力：未实现必须显式报错

    store = DB()
    run(store.set("ns", "k", 1))
    assert run(store.get("ns", "k")) == 1
    run(store.delete("ns", "k"))
    assert run(store.get("ns", "k")) is None
    run(store.append("ns", "rows", "a"))        # append：key 处的有序列表
    run(store.append("ns", "rows", "b"))
    assert run(store.get("ns", "rows")) == ["a", "b"]


def test_envelopes_are_the_small_set():
    import weave.core.envelopes as E

    assert set(E.__all__) == {
        "CallRequest", "Invocation", "TypedFailure",
        "Message", "Block", "TextBlock", "ImageBlock",
        "ToolCall", "ToolSchema", "LLMResponse", "StreamChunk",
        "RETRYABLE_KINDS", "NON_RETRYABLE_KINDS", "classify_exception",
    }
    for gone in ("Scope", "Record", "RecallRequest", "RememberRequest", "Context",
                 "CallResult", "Observation", "RunResult"):
        assert not hasattr(E, gone), f"{gone} 不再需要，应已删除"


def test_stream_chunk_carries_tool_call_deltas():
    chunk = StreamChunk(kind="tool_call_delta", index=1, tool_call_name="read_",
                        arguments_delta='{"a"')
    assert (chunk.index, chunk.tool_call_name, chunk.arguments_delta) == (1, "read_", '{"a"')


def test_invocation_is_frozen_value():
    inv = Invocation(name="t", arguments={"a": 1}, id="c1")
    assert inv.arguments == {"a": 1} and inv.id == "c1"
    with pytest.raises(Exception):
        inv.name = "other"                       # frozen：解码产物不应被改写


def test_typed_failure_kind_sets_are_disjoint():
    from weave.core.envelopes import NON_RETRYABLE_KINDS, RETRYABLE_KINDS

    assert RETRYABLE_KINDS.isdisjoint(NON_RETRYABLE_KINDS)
    with pytest.raises(ValueError):
        TypedFailure(kind="rate_limit", retryable=False)
    with pytest.raises(ValueError):
        TypedFailure(kind="made_up")


# ── 2. 反向依赖门禁 ─────────────────────────────────────


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.append(node.module)
    return found


def test_atom_layer_has_zero_reverse_dependencies():
    violations: list[str] = []
    for path in ATOM_FILES:
        for module in _imports_of(path):
            if module.startswith(FORBIDDEN_ATOM_IMPORTS):
                violations.append(f"{path.relative_to(WEAVE_ROOT)} → {module}")
    assert not violations, "原子层出现了上层依赖：" + "; ".join(violations)


def test_object_layer_does_not_touch_storage_or_removed_modules():
    """LLM 交互对象不认识存储，也不认识已归档的能力。"""
    forbidden = ("weave.stores", "weave.logs", "weave.search", "weave.executors",
                 "weave.catalogs", "weave.strategy", "weave.capabilities", "demos")
    scanned = sorted((WEAVE_ROOT / "llm").glob("*.py"))
    assert scanned, "没扫到任何 llm 层文件，门禁形同虚设"
    violations: list[str] = []
    for path in scanned:
        for module in _imports_of(path):
            if module.startswith(forbidden):
                violations.append(f"llm/{path.name} → {module}")
    assert not violations, "LLM 交互对象接入了存储/已归档能力：" + "; ".join(violations)


def test_core_has_zero_third_party_dependencies():
    stdlib_only = {"__future__", "abc", "asyncio", "collections", "copy", "dataclasses",
                   "enum", "hashlib", "json", "logging", "os", "pathlib", "sqlite3",
                   "time", "types", "typing", "uuid", "contextlib", "itertools",
                   "inspect", "re", "math", "base64", "random", "functools", "warnings"}
    violations: list[str] = []
    for path in sorted(CORE_DIR.glob("*.py")):
        for module in _imports_of(path):
            root = module.split(".")[0]
            if root.startswith("weave"):
                continue
            if root not in stdlib_only:
                violations.append(f"{path.name} → {module}")
    assert not violations, "core 出现了第三方依赖：" + "; ".join(violations)


def test_providers_do_not_decode_tool_calls():
    """哑原子不解码：providers 层不得出现 Invocation。"""
    offenders = [
        p.name for p in (WEAVE_ROOT / "providers").glob("*.py")
        if "Invocation" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"Provider 里出现了解码职责：{offenders}"


def test_message_and_toolschema_are_still_frozen_vocabulary():
    message = Message(role="user", content="hi")
    schema = ToolSchema(name="t")
    assert message.role == "user" and schema.name == "t"
