"""StateMemory 实现 — 按 namespace 解析 backend。"""
from __future__ import annotations

from typing import Any


class SQLiteStateMemory:
    """SQLite 后端的 StateMemory 实现。"""

    def __init__(self, manager: Any):  # MemoryManager
        self._manager = manager

    def _backend(self, namespace: str) -> Any:
        return self._manager._get_backend_for_namespace(namespace)

    async def get(self, key: str, namespace: str) -> Any | None:
        return self._backend(namespace).state_get(key, namespace)

    async def set(self, key: str, value: Any, namespace: str) -> str:
        # TTL：由 manager 按 namespace 解析 scope 配置，后端写入时计算
        # expires_at=created_at+ttl（review round-4 issue 1）
        ttl = self._manager._ttl_for_namespace(namespace)
        return self._backend(namespace).state_set(key, value, namespace, ttl)

    async def delete(self, key: str, namespace: str) -> None:
        self._backend(namespace).state_delete(key, namespace)

    async def get_all(self, namespaces: list[str] | None = None) -> dict[str, Any]:
        """读取所有 state 键值。

        无参数路径（namespaces=None）同样基于激活 scope 的 namespace 列表
        （窄→宽），复用显式路径的"宽→窄"合并逻辑，使窄 scope 覆盖宽 scope。
        此前无参数路径用单条 SQL 跨全部 namespace 按 created_at 排序后
        update，同 key 冲突由"最后写入时间"决定归属，与显式路径的窄胜语义
        不一致——同一实例两条读取路径对同 key 给出不同值
        （review round-6 issue 3）。
        """
        if not namespaces:
            namespaces = self._manager.get_namespaces("state")
            if not namespaces:
                return {}
        # 按 namespace 分组到各自的 backend
        results: dict[str, Any] = {}
        # 优先级合并方向：get_namespaces 返回按 priority 升序（窄→宽）。basic.md
        # §4.2 语义"priority 越小越窄，同 key 窄覆盖宽"——因此按宽→窄迭代、
        # 后写覆盖先写，使窄 scope（最后 update）胜出。若按窄→宽迭代 update，
        # 宽 scope 会覆盖窄 scope，与设计语义相反（review round-4 issue 2）。
        for ns in reversed(namespaces):
            backend = self._backend(ns)
            # 逐个 ns 查询以确保 ns 优先级顺序在合并时正确
            partial = backend.state_get_all([ns])
            results.update(partial)
        return results

    async def list(self, namespace: str) -> list[str]:
        """列出指定 namespace 下所有 key。"""
        backend = self._backend(namespace)
        # Not directly supported; iterate via get_all
        result = backend.state_get_all([namespace])
        return list(result.keys())
