"""PythonExecutor — 把 Python 函数当环境能力执行（执行引擎域）。

契约见 weave.core.interfaces.Executor：只执行；不知道"何时执行、执行几次"。
失败一律转成 `ToolError`（上层再转 Observation 回填，让模型自救）。
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from weave.core.envelopes import Invocation
from weave.core.errors import ToolError
from weave.core.interfaces import Executor

Handler = Callable[..., Any]


class PythonExecutor(Executor):
    """注册式执行器。

    用法：
        ex = PythonExecutor()
        ex.register(read_file, name="read_file")     # 显式注册
        @ex.tool                                       # 或用装饰器
        def add(a: int, b: int) -> int: ...
    """

    def __init__(self, handlers: Mapping[str, Handler] | None = None) -> None:
        self._handlers: dict[str, Handler] = dict(handlers or {})

    # ── 注册 ─────────────────────────────────────────

    def register(self, fn: Handler, *, name: str | None = None) -> Handler:
        self._handlers[name or fn.__name__] = fn
        return fn

    def tool(self, fn: Handler | None = None, *, name: str | None = None):
        """装饰器形态：`@ex.tool` 或 `@ex.tool(name="x")`。"""

        def wrap(inner: Handler) -> Handler:
            return self.register(inner, name=name)

        return wrap(fn) if fn is not None else wrap

    @property
    def handlers(self) -> dict[str, Handler]:
        """给 `ReflectionCatalog` 反射用的只读快照。"""
        return dict(self._handlers)

    def close(self) -> None:
        """预留：持有资源（进程/连接）的实现在这里释放。"""

    # ── Executor ─────────────────────────────────────

    async def execute(self, invocation: Invocation) -> Any:
        fn = self._handlers.get(invocation.name)
        if fn is None:
            raise ToolError(
                invocation.name,
                invocation.arguments,
                f"未知工具 {invocation.name!r}（可用：{sorted(self._handlers)}）",
            )
        try:
            result = fn(**invocation.arguments)
        except TypeError as exc:                    # 参数不匹配 = 模型用错了工具
            raise ToolError(
                invocation.name, invocation.arguments, f"参数不匹配: {exc}", cause=exc
            ) from exc
        except ToolError:
            raise                                   # 已经类型化，原样透出
        except Exception as exc:                    # noqa: BLE001 - 统一转类型化失败
            raise ToolError(
                invocation.name, invocation.arguments, str(exc), cause=exc
            ) from exc
        if inspect.isawaitable(result):
            try:
                return await result
            except ToolError:
                raise
            except Exception as exc:                # noqa: BLE001 - 统一转类型化失败
                raise ToolError(
                    invocation.name, invocation.arguments, str(exc), cause=exc
                ) from exc
        return result
