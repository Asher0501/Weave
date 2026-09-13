"""CheckpointManager — 状态快照/回滚参考实现（基于存储可选能力 ListableStore）。

业务无关：快照 = 把某命名空间全部 (key, value) 复制到 ``checkpoint:{id}`` 命名空间；
回滚 = 清空目标命名空间后从快照还原。
"""
from __future__ import annotations

import uuid
from typing import Any

from weave.core.interfaces import ListableStore, StateStore


class CheckpointManager:
    def __init__(self, store: StateStore) -> None:
        self._store = store
        if not isinstance(store, ListableStore):
            raise TypeError(
                "checkpoint requires a ListableStore (store must implement "
                "list_namespaces/list_keys)"
            )

    async def snapshot(self, namespace: str) -> str:
        """快照整个命名空间，返回 checkpoint id。"""
        cid = uuid.uuid4().hex
        snap_ns = f"checkpoint:{cid}"
        for key in await self._store.list_keys(namespace):
            value = await self._store.get(namespace, key)
            if value is not None:
                await self._store.set(snap_ns, key, value)
        return cid

    async def restore(self, namespace: str, checkpoint_id: str) -> None:
        """从快照还原（先清空目标命名空间）。"""
        snap_ns = f"checkpoint:{checkpoint_id}"
        keys = await self._store.list_keys(namespace)
        for key in keys:
            await self._store.delete(namespace, key)
        for key in await self._store.list_keys(snap_ns):
            value = await self._store.get(snap_ns, key)
            if value is not None:
                await self._store.set(namespace, key, value)

    async def list_checkpoints(self, target_ns: str | None = None) -> list[str]:
        """列出当前全部 checkpoint id（可按目标命名空间过滤元信息由用户实现）。"""
        nss = await self._store.list_namespaces()
        return [ns.split(":", 1)[1] for ns in nss if ns.startswith("checkpoint:")]


__all__ = ["CheckpointManager"]
