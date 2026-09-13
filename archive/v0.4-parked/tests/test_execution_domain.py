"""执行维度的测试（**未来维度**：Executor / EnvironmentCatalog）。

注意：模型输出的解码已归入 LLM 交互维度（``weave.llm.decode``），不再属于这里。
本文件覆盖：
- Executor 的失败一律类型化（ToolError）：未知工具 / 参数不符 / 内部异常都不崩；
- Catalog 只反射不执行，且必须拒绝重名能力。
"""
from __future__ import annotations

import asyncio

import pytest

from weave.catalogs.reflection import ReflectionCatalog, StaticCatalog, schema_from_function
from weave.core.envelopes import Invocation
from weave.core.errors import CatalogError, ToolError
from weave.core.interfaces import EnvironmentCatalog, Executor
from weave.core.types import ToolSchema
from weave.executors.python_exec import PythonExecutor


def run(coro):
    return asyncio.run(coro)


# ── Executor ────────────────────────────────────────────


def test_python_executor_runs_sync_and_async_handlers():
    async def aio(x: int) -> int:
        return x + 1

    executor = PythonExecutor({"add": lambda a, b: a + b, "aio": aio})
    assert run(executor.execute(Invocation(name="add", arguments={"a": 1, "b": 2}))) == 3
    assert run(executor.execute(Invocation(name="aio", arguments={"x": 1}))) == 2


def test_python_executor_registration_forms():
    executor = PythonExecutor()
    executor.register(lambda: "x", name="renamed")

    @executor.tool
    def plain() -> str:
        return "plain"

    @executor.tool(name="aliased")
    def other() -> str:
        return "aliased"

    assert run(executor.execute(Invocation(name="renamed", arguments={}))) == "x"
    assert run(executor.execute(Invocation(name="plain", arguments={}))) == "plain"
    assert run(executor.execute(Invocation(name="aliased", arguments={}))) == "aliased"
    assert set(executor.handlers) == {"renamed", "plain", "aliased"}


def test_python_executor_failures_are_typed():
    def boom() -> None:
        raise RuntimeError("kaboom")

    def typed() -> None:
        raise ToolError("typed", {}, "already typed")

    executor = PythonExecutor({"boom": boom, "typed": typed, "needs_arg": lambda a: a})

    with pytest.raises(ToolError) as unknown:
        run(executor.execute(Invocation(name="missing", arguments={})))
    assert "未知工具" in str(unknown.value)

    with pytest.raises(ToolError) as mismatch:
        run(executor.execute(Invocation(name="needs_arg", arguments={"wrong": 1})))
    assert "参数不匹配" in str(mismatch.value)

    with pytest.raises(ToolError) as raised:
        run(executor.execute(Invocation(name="boom", arguments={})))
    assert raised.value.cause is not None and "kaboom" in str(raised.value)

    with pytest.raises(ToolError) as passthrough:
        run(executor.execute(Invocation(name="typed", arguments={})))
    assert "already typed" in str(passthrough.value)


def test_python_executor_satisfies_contract():
    assert isinstance(PythonExecutor(), Executor)


# ── EnvironmentCatalog ──────────────────────────────────


def test_schema_from_function_covers_types_required_and_defaults():
    def search(query: str, limit: int = 5, flag: bool = False, tags: list[str] | None = None):
        """搜索资料。

        详细说明不该出现在描述里。
        """

    schema = schema_from_function(search)
    assert schema.name == "search"
    assert schema.description == "搜索资料。"
    props = schema.parameters["properties"]
    assert props["query"] == {"type": "string"}
    assert props["limit"] == {"type": "integer", "default": 5}
    assert props["flag"] == {"type": "boolean", "default": False}
    assert props["tags"]["type"] == "array"
    assert schema.parameters["required"] == ["query"]


def test_schema_from_function_handles_var_args_and_unknown_types():
    def weird(a, *args, **kwargs):
        pass

    schema = schema_from_function(weird)
    assert schema.parameters["properties"] == {"a": {}}
    assert schema.parameters["required"] == ["a"]


def test_reflection_catalog_describes_without_executing():
    calls: list[str] = []

    def touch(name: str) -> str:
        """名字。"""
        calls.append(name)
        return name

    executor = PythonExecutor({"touch": touch})
    catalog = ReflectionCatalog(executor)
    schemas = run(catalog.describe())
    assert [s.name for s in schemas] == ["touch"]
    assert calls == [], "describe() 绝不能执行 handler"
    assert isinstance(catalog, EnvironmentCatalog)


def test_reflection_catalog_rejects_bad_source():
    with pytest.raises(CatalogError):
        run(ReflectionCatalog("not a mapping").describe())


def test_reflection_catalog_accepts_mapping_and_handlers_object():
    """重名在映射里不可能出现（键唯一），所以两条路径都应正常返回。"""

    def one(a: int) -> int:
        return a

    class Holder:
        handlers = {"dup": one}

    from_mapping = run(ReflectionCatalog({"dup": one}).describe())
    from_object = run(ReflectionCatalog(Holder()).describe())
    assert [s.name for s in from_mapping] == ["dup"]
    assert [s.name for s in from_object] == ["dup"]


def test_static_catalog_and_duplicate_detection():
    catalog = StaticCatalog([ToolSchema(name="a"), ToolSchema(name="a")])
    with pytest.raises(CatalogError):
        run(catalog.describe())
    ok = StaticCatalog([ToolSchema(name="a"), ToolSchema(name="b")])
    assert [s.name for s in run(ok.describe())] == ["a", "b"]
