"""环境能力目录的参考实现（执行引擎域，D10）。

- `ReflectionCatalog`：从已注册的 handler **反射**出 `ToolSchema`（默认实现，零负担）；
- `StaticCatalog`：手写 schema（外部环境 / 远端目录由你自己描述时用）。

契约见 weave.core.interfaces.EnvironmentCatalog：只读、绝不执行动作、结果稳定可缓存。
"""
from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from typing import Any, get_args, get_origin, get_type_hints

from weave.core.errors import CatalogError
from weave.core.interfaces import EnvironmentCatalog
from weave.core.types import ToolSchema

_PY_TO_JSON: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _json_type(annotation: Any) -> dict[str, Any]:
    """Python 注解 → JSON Schema 片段（认不出的类型退化为 string）。"""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is None:
        return {"type": _PY_TO_JSON.get(annotation, "string")}
    # Optional[X] / Union[X, None] → X（注意 `X | None` 的 origin 是 types.UnionType）
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        return _json_type(non_none[0]) if len(non_none) == 1 else {
            "anyOf": [_json_type(a) or {"type": "string"} for a in non_none]
        }
    if origin in (list, set, tuple):
        items = _json_type(args[0]) if args else {}
        return {"type": "array", "items": items or {"type": "string"}}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}


def _first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    lines: list[str] = []
    for raw in doc.strip().splitlines():
        line = raw.strip()
        if not line:
            break
        lines.append(line)
    return " ".join(lines)


def schema_from_function(fn: Callable[..., Any], *, name: str | None = None) -> ToolSchema:
    """函数 → ToolSchema（纯反射，不执行函数）。"""
    tool_name = name or getattr(fn, "__name__", "")
    if not tool_name:
        raise CatalogError("无法为匿名函数生成 schema：请显式给 name")
    try:
        hints = get_type_hints(fn)
    except Exception:                                # noqa: BLE001 - 注解解析失败不致命
        hints = getattr(fn, "__annotations__", {}) or {}
    signature = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in signature.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        schema = _json_type(hints.get(param_name, param.annotation))
        if param.default is not inspect.Parameter.empty:
            schema = {**schema, "default": param.default}
        else:
            required.append(param_name)
        properties[param_name] = schema
    parameters_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters_schema["required"] = required
    return ToolSchema(
        name=tool_name,
        description=_first_paragraph(fn.__doc__),
        parameters=parameters_schema,
    )


class ReflectionCatalog(EnvironmentCatalog):
    """默认实现：从已注册的 handler 反射出清单。

    `source` 可以是 `Mapping[str, Callable]`，也可以是任何带 `.handlers` 的对象
    （例如 `PythonExecutor`）。
    """

    def __init__(self, source: Mapping[str, Callable[..., Any]] | Any) -> None:
        self._source = source

    def _handlers(self) -> Mapping[str, Callable[..., Any]]:
        handlers = getattr(self._source, "handlers", self._source)
        if not isinstance(handlers, Mapping):
            raise CatalogError(f"无法从 {type(self._source).__name__} 取得 handler 表")
        return handlers

    async def describe(self) -> list[ToolSchema]:
        schemas = [schema_from_function(fn, name=name) for name, fn in self._handlers().items()]
        names = [s.name for s in schemas]
        if len(set(names)) != len(names):
            raise CatalogError(f"环境里存在重名能力：{sorted(names)}")
        return schemas


class StaticCatalog(EnvironmentCatalog):
    """手写清单（远端/外部环境，或不想暴露函数签名时）。"""

    def __init__(self, schemas: Iterable[ToolSchema]) -> None:
        self._schemas = [s for s in schemas]

    async def describe(self) -> list[ToolSchema]:
        names = [s.name for s in self._schemas]
        if len(set(names)) != len(names):
            raise CatalogError(f"环境里存在重名能力：{sorted(names)}")
        return list(self._schemas)
