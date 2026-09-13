"""示例 ToolRegistry（demos/atomic.tool_registry）测试：schema 推断 / 线程池 / 错误转写。"""
import asyncio
import time

import pytest

from weave.core.errors import ToolError
from demos.atomic.tool_registry import PythonToolRegistry, infer_schema


def test_infer_schema_from_annotations():
    def add(a: int, b: int) -> int:
        """两数相加。"""
        return a + b

    schema = infer_schema(add)
    assert schema.name == "add"
    assert schema.description == "两数相加。"
    params = schema.parameters
    assert params["type"] == "object"
    assert params["properties"]["a"]["type"] == "integer"
    assert params["required"] == ["a", "b"]


def test_infer_schema_optional_default():
    def greet(name: str, lang: str = "zh") -> str:
        return f"{lang}:{name}"

    assert infer_schema(greet).parameters["required"] == ["name"]


def test_registry_decorator_and_execute():
    reg = PythonToolRegistry()

    @reg.tool
    def add(a: int, b: int) -> int:
        return a + b

    @reg.tool(name="hi", description="问候")
    def greet(name: str) -> str:
        return f"hi {name}"

    assert [s.name for s in reg.get_schemas()] == ["add", "hi"]
    assert asyncio.run(reg.execute("add", {"a": 1, "b": 2})) == 3
    assert asyncio.run(reg.execute("hi", {"name": "w"})) == "hi w"


def test_sync_tool_runs_in_thread():
    reg = PythonToolRegistry()

    def slow_sleep(n: float) -> str:
        time.sleep(n)
        return "woke"

    reg.register(slow_sleep, name="sleep")

    async def _t():
        task = asyncio.create_task(reg.execute("sleep", {"n": 0.15}))
        start = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - start < 0.1
        return await task

    assert asyncio.run(_t()) == "woke"


def test_async_tool_executes_directly():
    reg = PythonToolRegistry()

    @reg.tool
    async def fetch(url: str) -> str:
        await asyncio.sleep(0)
        return f"fetched:{url}"

    assert asyncio.run(reg.execute("fetch", {"url": "x"})) == "fetched:x"


def test_tool_error_wraps_failure_and_unknown():
    reg = PythonToolRegistry()

    @reg.tool
    def boom(x: int) -> int:
        raise ValueError("bad input")

    with pytest.raises(ToolError) as ei:
        asyncio.run(reg.execute("boom", {"x": 1}))
    assert ei.value.name == "boom"
    assert isinstance(ei.value.cause, ValueError)
    with pytest.raises(ToolError):
        asyncio.run(reg.execute("nope", {}))


def test_schema_list_and_object_annotations():
    def analyze(items: list[str], cfg: dict) -> None:
        """分析。"""

    schema = infer_schema(analyze)
    assert schema.parameters["properties"]["items"]["type"] == "array"
    assert schema.parameters["properties"]["cfg"]["type"] == "object"
