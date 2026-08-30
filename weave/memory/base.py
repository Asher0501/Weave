"""Memory 抽象接口。

StreamMemory / StateMemory / KnowledgeMemory 三种访问模式的 ABC。
写入方法 namespace: str (单数)，读取方法 namespaces: list[str] (复数)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from weave.types import MemoryEntry, SearchResult


class StreamMemory(ABC):
    """时序消息流 — 追加写入，按时间顺序读取最近 N 条。"""

    @abstractmethod
    async def append(self, entry: dict[str, Any], namespace: str) -> str:
        """追加一条消息，返回 entry ID。"""
        ...

    @abstractmethod
    async def last(self, n: int = 20, namespaces: list[str] | None = None) -> list[dict[str, Any]]:
        """获取最近 N 条消息（跨 namespace 合并，按 created_at 排序）。

        Args:
            n: 返回条数
            namespaces: 要查询的 namespace 列表，None = 所有激活的 namespace
        """
        ...

    @abstractmethod
    async def trim(self, max_items: int, namespace: str) -> None:
        """裁剪指定 namespace，保留最近 max_items 条。"""
        ...


class StateMemory(ABC):
    """键值状态 — 按 key 精确读写，新值覆盖旧值。"""

    @abstractmethod
    async def get(self, key: str, namespace: str) -> Any | None:
        """读取 key 的值。"""
        ...

    @abstractmethod
    async def set(self, key: str, value: Any, namespace: str) -> str:
        """写入键值（UPSERT），返回 entry ID。"""
        ...

    @abstractmethod
    async def delete(self, key: str, namespace: str) -> None:
        """删除 key。"""
        ...

    @abstractmethod
    async def get_all(self, namespaces: list[str] | None = None) -> dict[str, Any]:
        """获取所有键值对。多个 namespace 时，同 key 窄 scope 覆盖宽 scope。

        Args:
            namespaces: 要查询的 namespace 列表（须按 priority 升序排列），None = 全部激活
        """
        ...


class KnowledgeMemory(ABC):
    """知识搜索 — 追加写入，不覆盖，语义/全文搜索。"""

    @abstractmethod
    async def add(self, content: str, namespace: str, metadata: dict[str, Any] | None = None) -> str:
        """追加一条知识，返回 entry ID。"""
        ...

    @abstractmethod
    async def search(self, query: str, namespaces: list[str] | None = None, top_k: int = 5) -> list[SearchResult]:
        """全文/语义搜索相关知识。

        Args:
            query: 搜索查询
            namespaces: 要搜索的 namespace 列表，None = 所有激活的 knowledge namespace
            top_k: 返回结果数
        """
        ...
