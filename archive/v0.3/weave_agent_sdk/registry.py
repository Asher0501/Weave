"""统一插件注册表。

Weave 的三个 adapter 层（LLM / Loop / Memory backend）共用此机制：
「配置字符串 → 实现」的分发走注册表，内置实现 = 预注册的默认值，
宿主可 register 自定义实现，且可经 weave.yaml 引用。

这使「分发 + 校验 + 报错」收敛到单一事实来源（见 docs/issues/010）。
"""
from __future__ import annotations

from typing import Any, Callable


class Registry:
    """name -> factory 的注册表。

    factory 既可以是类（Loop / Backend，直接 ``cls(*args)`` 实例化），
    也可以是工厂函数（LLM，懒 import + 定制构造）。
    """

    def __init__(self, kind: str):
        self._kind = kind
        self._factories: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, factory: Callable[..., Any], *, replace: bool = False) -> None:
        """注册一个实现。重名且未显式 ``replace=True`` 时抛 ValueError。"""
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"invalid {self._kind} name: {name!r}")
        if name in self._factories and not replace:
            raise ValueError(f"{self._kind} '{name}' is already registered")
        self._factories[name] = factory

    def get(self, name: str) -> Callable[..., Any]:
        """返回注册的 factory（不调用）。未命中抛 ValueError，列出已知 name。"""
        try:
            return self._factories[name]
        except KeyError:
            raise ValueError(self._unknown_message(name)) from None

    def create(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """``get(name)(*args, **kwargs)``。"""
        return self.get(name)(*args, **kwargs)

    def names(self) -> list[str]:
        """按注册顺序返回已知 name（保证报错文案稳定，见 010 向后兼容）。"""
        return list(self._factories)

    def __contains__(self, name: str) -> bool:
        return name in self._factories

    def _unknown_message(self, name: str) -> str:
        return f"Unknown {self._kind} '{name}'. Known: {', '.join(self.names())}"
