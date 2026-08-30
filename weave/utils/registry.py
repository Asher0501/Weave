"""C3: 惰性注册表 — key→instance 映射，惰性初始化 + TTL 淘汰。"""
from __future__ import annotations

import time
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class LazyRegistry:
    """惰性注册表，用于管理内部组件实例。

    - 惰性初始化：首次 get() 时才调用 factory 创建
    - TTL 淘汰：超过 ttl 秒未访问的实例自动清理
    - 线程安全：Lock 保护

    用法:
        registry = LazyRegistry(ttl=600)
        adapter = registry.get("claude", lambda: AnthropicAdapter(key))
    """

    def __init__(self, ttl: float = 600):
        self._ttl = ttl
        self._instances: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, factory: Callable[[], T]) -> T:
        """获取或创建实例。

        Args:
            key: 注册 key
            factory: 创建函数（仅在首次创建或 TTL 过期时调用）

        Returns:
            注册的实例
        """
        now = time.monotonic()
        with self._lock:
            # 检查是否存在且未过期
            if key in self._instances:
                last_access, instance = self._instances[key]
                if self._ttl <= 0 or (now - last_access) < self._ttl:
                    self._instances[key] = (now, instance)
                    return instance
                # TTL 过期，清理
                del self._instances[key]

            # 新建
            instance = factory()
            self._instances[key] = (now, instance)
            return instance

    def invalidate(self, key: str) -> None:
        """手动淘汰指定 key 的实例。"""
        with self._lock:
            self._instances.pop(key, None)

    def reset(self) -> None:
        """清空所有实例（测试用）。"""
        with self._lock:
            self._instances.clear()

    def cleanup(self) -> int:
        """清理所有过期实例，返回清理数量。"""
        if self._ttl <= 0:
            return 0
        now = time.monotonic()
        removed = 0
        with self._lock:
            expired = [
                k for k, (last_access, _) in self._instances.items()
                if (now - last_access) >= self._ttl
            ]
            for k in expired:
                del self._instances[k]
                removed += 1
        return removed

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._instances)
