"""StreamMemory 实现 — 按 namespace 解析 backend。"""
from __future__ import annotations

from typing import Any


class SQLiteStreamMemory:
    """SQLite 后端的 StreamMemory 实现。"""

    def __init__(self, manager: Any):  # MemoryManager
        self._manager = manager

    def _backend(self, namespace: str) -> Any:
        return self._manager._get_backend_for_namespace(namespace)

    async def append(self, entry: dict[str, Any], namespace: str) -> str:
        # TTL：由 manager 按 namespace 解析 scope 配置，后端写入时计算
        # expires_at=created_at+ttl（review round-4 issue 1）
        ttl = self._manager._ttl_for_namespace(namespace)
        return self._backend(namespace).stream_append(entry, namespace, ttl)

    async def last(self, n: int = 20, namespaces: list[str] | None = None) -> list[dict[str, Any]]:
        """获取最近的 n 条 stream 记录。

        当 namespaces 为 None 时，收集所有 backend 的数据并按时间合并排序，
        确保跨多个 db 文件时不会遗漏数据。
        """
        if not namespaces:
            # 无参数路径：先按激活 scope 的 stream namespace 解析并实例化
            # 后端。MemoryManager._backends 是懒加载的——新进程（尚未发生任何
            # 写/读）下为空，直接遍历会返回 []，尽管 DB 文件里已有历史数据
            # （review round-8 issue 1）。经 get_namespaces + _get_backend_
            # for_namespace 实例化后端（打开对应 DB 文件）后既有数据可见。
            for ns in self._manager.get_namespaces("stream"):
                self._manager._get_backend_for_namespace(ns)
            # 收集所有 backend 的结果，按时间全局排序后取前 n 条
            all_results: list[dict[str, Any]] = []
            for backend in self._manager._backends.values():
                # chroma 后端仅支持 knowledge（向量语义搜索），无 stream_last
                # 方法——跳过，避免 AttributeError（review round-8 issue 1）。
                if not hasattr(backend, "stream_last"):
                    continue
                # 每个 backend 返回的条目按时间升序（最旧在前），含 _created_at 内部字段
                backend_results = backend.stream_last(n, None)
                all_results.extend(backend_results)

            if not all_results:
                return []

            # 按 _created_at 降序排列（最新在前），取前 n 条
            all_results.sort(key=lambda x: x.get("_created_at", 0), reverse=True)
            top_n = all_results[:n]

            # 清理内部字段，按时间升序返回（最旧在前）
            for entry in top_n:
                entry.pop("_created_at", None)
            top_n.reverse()
            return top_n

        # 有指定的 namespaces：按 namespace 分组到对应的 backend
        # 收集所有涉及的后端结果，合并排序
        backend_to_ns: dict[str, list[str]] = {}
        for ns in namespaces:
            backend = self._backend(ns)
            backend_key = backend._db_path if hasattr(backend, '_db_path') else str(id(backend))
            if backend_key not in backend_to_ns:
                backend_to_ns[backend_key] = []
            backend_to_ns[backend_key].append(ns)

        if not backend_to_ns:
            return []

        # 只有一个后端，直接查询（优化路径）
        if len(backend_to_ns) == 1:
            backend = self._backend(namespaces[0])
            results = backend.stream_last(n, namespaces)
            # 清理内部 _created_at 字段
            for entry in results:
                entry.pop("_created_at", None)
            return results

        # 多个后端：收集、排序、合并
        all_results = []
        for backend_key, ns_list in backend_to_ns.items():
            # 找到对应的 backend 实例
            target_backend = None
            for b in self._manager._backends.values():
                bk = b._db_path if hasattr(b, '_db_path') else str(id(b))
                if bk == backend_key:
                    target_backend = b
                    break
            if target_backend:
                all_results.extend(target_backend.stream_last(n, ns_list))

        if not all_results:
            return []

        all_results.sort(key=lambda x: x.get("_created_at", 0), reverse=True)
        top_n = all_results[:n]
        for entry in top_n:
            entry.pop("_created_at", None)
        top_n.reverse()
        return top_n

    async def trim(self, max_items: int, namespace: str) -> None:
        self._backend(namespace).stream_trim(max_items, namespace)
