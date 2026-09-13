"""示例：业务自有的 ToolRegistry 实现（调用方代码，不在 weave 包内）。

weave 只给原子接口（get_schemas/execute）；把 Python 函数绑定成工具、注解推导
schema、同步函数走线程池等，都是调用方可选的实现方式。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin

from weave.core.errors import ToolError
from weave.core.interfaces import ToolRegistry
from weave.core.types import ToolSchema

_JSON_TYPES: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _type_to_json(anno: Any) -> str:
    if anno is inspect.Parameter.empty or anno is Any or anno is None:
        return "string"
    origin = get_origin(anno)
    if origin is not None and get_args(anno):
        if origin in (list, tuple, set):
            return "array"
        if origin is dict:
            return "object"
    if anno in (list, tuple, set):
        return "array"
    if anno is dict:
        return "object"
    return _JSON_TYPES.get(anno, "string")


def infer_schema(fn: Callable[..., Any], *, name: str | None = None, description: str | None = None) -> ToolSchema:
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        properties[pname] = {"type": _type_to_json(param.annotation)}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    doc = description
    if doc is None and fn.__doc__:
        doc = fn.__doc__.strip().splitlines()[0] if fn.__doc__.strip() else ""
    return ToolSchema(
        name=name or fn.__name__,
        description=doc or "",
        parameters={"type": "object", "properties": properties, "required": required},
    )


@dataclass(slots=True)
class BoundTool:
    fn: Callable[..., Any]
    schema: ToolSchema

    @property
    def name(self) -> str:
        return self.schema.name

    async def run(self, arguments: dict[str, Any]) -> Any:
        try:
            if inspect.iscoroutinefunction(self.fn):
                return await self.fn(**arguments)
            return await asyncio.to_thread(self.fn, **arguments)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(self.name, arguments, str(e), cause=e) from e


class PythonToolRegistry(ToolRegistry):
    """示例实现：@reg.tool 装饰器 + 注解推导 + 线程池执行。"""

    def __init__(self) -> None:
        self._tools: list[BoundTool] = []
        self._by_name: dict[str, BoundTool] = {}

    def register(self, fn: Callable[..., Any], *, name: str | None = None, description: str | None = None) -> Callable[..., Any]:
        tool = BoundTool(fn=fn, schema=infer_schema(fn, name=name, description=description))
        if tool.name in self._by_name:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools.append(tool)
        self._by_name[tool.name] = tool
        return fn

    def tool(self, fn: Callable[..., Any] | None = None, *, name: str | None = None, description: str | None = None) -> Any:
        def _deco(f: Callable[..., Any]) -> Callable[..., Any]:
            self.register(f, name=name, description=description)
            return f

        if fn is None:
            return _deco
        return _deco(fn)

    def get_schemas(self) -> list[ToolSchema]:
        return [t.schema for t in self._tools]

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._by_name.get(name)
        if tool is None:
            raise ToolError(name, arguments, f"unknown tool '{name}'")
        return await tool.run(arguments)
