"""InMemoryStateStore — 内存参考实现（测试/短会话）。

正确性约定：JSON 语义 + 值隔离（deepcopy）+ 并发安全（进程内锁）。
它们是实现自带的能力，不再有对应的接口契约（那些已归档到 archive/v0.4-parked/）。
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from weave.core.interfaces import StateStore


class InMemoryStateStore(StateStore):
    """进程内 StateStore。namespace/key 均视为不透明字符串。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ── 基础四方法 ────────────────────────────────────

    async def get(self, namespace: str, key: str) -> Any | None:
        async with self._lock:
            ns = self._data.get(namespace)
            if ns is None or key not in ns:
                return None
            return copy.deepcopy(ns[key])

    async def set(self, namespace: str, key: str, value: Any) -> None:
        async with self._lock:
            self._data.setdefault(namespace, {})[key] = copy.deepcopy(value)

    async def delete(self, namespace: str, key: str) -> None:
        async with self._lock:
            ns = self._data.get(namespace)
            if ns is not None:
                ns.pop(key, None)
                if not ns:
                    self._data.pop(namespace, None)

    async def append(self, namespace: str, key: str, value: Any) -> None:
        async with self._lock:
            ns = self._data.setdefault(namespace, {})
            existing = ns.get(key)
            if existing is None:
                ns[key] = [copy.deepcopy(value)]
                return
            if not isinstance(existing, list):
                raise ValueError(
                    f"append: key {key!r} in namespace {namespace!r} is not a list"
                )
            existing.append(copy.deepcopy(value))

