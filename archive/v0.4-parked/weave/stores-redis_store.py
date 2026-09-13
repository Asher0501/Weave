"""RedisStateStore — Redis 参考实现（可选 extra；多进程共享，D5/T8）。

依赖 redis（pip install 'weave[redis]'）。命名空间→Redis Hash：namespace 为 key、
条目 (field=key, value=JSON)。命名空间按不透明字符串处理（T7）。
"""
from __future__ import annotations

import copy
import json
from typing import Any

from weave.core.interfaces import ListableStore, StateStore, TrimmableStore


class RedisStateStore(StateStore, ListableStore, TrimmableStore):
    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "wv:") -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError(
                "redis package required for RedisStateStore. "
                "Install with: pip install 'weave[redis]'"
            ) from None
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = prefix

    async def close(self) -> None:
        await self._redis.aclose()

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _decode(text: str) -> Any:
        return json.loads(text)

    # ── StateStore ────────────────────────────────────

    async def get(self, namespace: str, key: str) -> Any | None:
        raw = await self._redis.hget(self._prefix + namespace, key)
        if raw is None:
            return None
        return copy.deepcopy(self._decode(raw))

    async def set(self, namespace: str, key: str, value: Any) -> None:
        await self._redis.hset(self._prefix + namespace, key, self._encode(value))

    async def delete(self, namespace: str, key: str) -> None:
        await self._redis.hdel(self._prefix + namespace, key)

    async def append(self, namespace: str, key: str, value: Any) -> None:
        raw = await self._redis.hget(self._prefix + namespace, key)
        if raw is None:
            await self.set(namespace, key, [value])
            return
        existing = self._decode(raw)
        if not isinstance(existing, list):
            raise ValueError(
                f"append: key {key!r} in namespace {namespace!r} is not a list"
            )
        existing.append(value)
        await self.set(namespace, key, existing)

    # ── ListableStore ─────────────────────────────────

    async def list_namespaces(self) -> list[str]:
        keys: list[str] = []
        async for key in self._redis.scan_iter(match=self._prefix + "*", count=100):
            keys.append(key[len(self._prefix):])
        return keys

    async def list_keys(self, namespace: str) -> list[str]:
        return list(await self._redis.hkeys(self._prefix + namespace))

    # ── TrimmableStore ────────────────────────────────

    async def trim(self, namespace: str, key: str, max_items: int) -> None:
        if max_items < 0:
            raise ValueError("max_items must be >= 0")
        raw = await self._redis.hget(self._prefix + namespace, key)
        if raw is None:
            return
        value = self._decode(raw)
        if isinstance(value, list) and len(value) > max_items:
            if max_items == 0:
                await self.delete(namespace, key)
            else:
                await self.set(namespace, key, value[-max_items:])


__all__ = ["RedisStateStore"]
