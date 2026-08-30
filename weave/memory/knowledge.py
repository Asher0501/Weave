"""KnowledgeMemory 实现 — 按 namespace 解析 backend。"""
from __future__ import annotations

from typing import Any

from weave.types import SearchResult


class SQLiteKnowledgeMemory:
    """SQLite 后端的 KnowledgeMemory 实现（LIKE 关键词搜索）。"""

    def __init__(self, manager: Any):  # MemoryManager
        self._manager = manager

    def _backend(self, namespace: str) -> Any:
        return self._manager._get_backend_for_namespace(namespace)

    async def add(self, content: str, namespace: str, metadata: dict[str, Any] | None = None) -> str:
        # TTL：由 manager 按 namespace 解析 scope 配置，后端写入时计算
        # expires_at=created_at+ttl（review round-4 issue 1）
        ttl = self._manager._ttl_for_namespace(namespace)
        return self._backend(namespace).knowledge_add(content, namespace, metadata, ttl)

    async def search(self, query: str, namespaces: list[str] | None = None, top_k: int = 5) -> list[SearchResult]:
        if not namespaces:
            # 无参数路径：先按激活 scope 的 knowledge namespace 解析并实例化
            # 后端。MemoryManager._backends 是懒加载的——新进程（尚未发生任何
            # 写/读）下为空，直接遍历会返回 []，尽管 DB 文件里已有历史数据
            # （review round-8 issue 1）。经 get_namespaces + _get_backend_
            # for_namespace 实例化后端（打开对应 DB 文件）后既有数据可见。
            for ns in self._manager.get_namespaces("knowledge"):
                self._manager._get_backend_for_namespace(ns)
            results: list[SearchResult] = []
            for backend in set(self._manager._backends.values()):
                results.extend(backend.knowledge_search(query, None, top_k))
            results.sort(key=lambda r: r.score, reverse=True)
            return results[:top_k]
        # 按 namespace → backend 分组收集
        all_results: list[SearchResult] = []
        for ns in namespaces:
            backend = self._backend(ns)
            all_results.extend(backend.knowledge_search(query, [ns], top_k))
        all_results.sort(key=lambda r: r.score, reverse=True)
        return all_results[:top_k]
