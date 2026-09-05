"""状态回滚（Checkpoint / Rollback）。

CheckpointManager 实现第一层回滚：Memory 认知状态（state 键值 + stream 时序流）
的快照与恢复。纯非侵入——只操作 Weave 自己掌控的 Memory，不触碰宿主外部副作用。

设计（见 docs/issues/012）：
- 快照 append-only（fork 语义）：rollback 不删除快照，可反复回滚
- stream 回滚用时间水位线：快照时刻 time.time()，删除 created_at > watermark 的条目
- 存储复用"当前最窄 scope 的 state namespace"，key 加 __checkpoint_ 前缀隔离——
  这样 checkpoint 天然跟随宿主的 scope backend/path（session 隔离 + 测试干净），
  而非回退到全局默认 path（那会导致跨 session 污染）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from weave_agent_sdk.memory.manager import MemoryManager
from weave_agent_sdk.types import CheckpointConfig


# checkpoint 存储 key 前缀，与宿主 state 键隔离
_CP_PREFIX = "__checkpoint_"


class CheckpointManager:
    """管理 Memory 状态快照与回滚。"""

    def __init__(self, memory: MemoryManager, config: CheckpointConfig):
        self._memory = memory
        self._config = config

    # ── 内部辅助 ──────────────────────────────────────

    def _checkpoint_namespace(self) -> str:
        """返回 checkpoint 存储的 namespace。

        复用当前激活 scope 中 priority 最小（最窄）的 state namespace，使
        checkpoint 跟随宿主的 scope backend/path（session 隔离、测试干净），
        而非回退全局默认 path。
        """
        state_ns = self._memory.get_namespaces("state")
        if state_ns:
            return state_ns[0]
        return "default:default:state"

    @staticmethod
    def _cp_key(cp_id: str) -> str:
        return f"{_CP_PREFIX}{cp_id}"

    async def _load_checkpoints(self, ns: str) -> dict[str, dict[str, Any]]:
        """读取 namespace 下所有 checkpoint（过滤 __checkpoint_ 前缀），
        返回 {checkpoint_id: 快照}。"""
        all_state = await self._memory.state.get_all([ns])
        return {
            k[len(_CP_PREFIX):]: v
            for k, v in all_state.items()
            if k.startswith(_CP_PREFIX)
        }

    # ── 公开操作 ──────────────────────────────────────

    async def checkpoint(self) -> str:
        """打一个快照，返回 checkpoint_id。

        快照内容 = 当前所有激活 scope 的 state 键值 + stream 时间水位线。
        """
        state_ns = self._memory.get_namespaces("state")
        stream_ns = self._memory.get_namespaces("stream")

        # 快照 state（每个 namespace 的完整键值）
        state_snapshot: dict[str, dict[str, Any]] = {}
        for ns in state_ns:
            state_snapshot[ns] = await self._memory.state.get_all([ns])

        # watermark = 快照时刻，用于 stream 回滚的精确分隔
        watermark = time.time()

        cp_id = f"cp_{uuid.uuid4().hex[:12]}"
        cp_value: dict[str, Any] = {
            "id": cp_id,
            "created_at": watermark,
            "state": state_snapshot,
            "stream_watermark": watermark,
            "state_namespaces": state_ns,
            "stream_namespaces": stream_ns,
        }

        ns = self._checkpoint_namespace()
        await self._memory.state.set(self._cp_key(cp_id), cp_value, ns)

        await self._enforce_keep(ns)
        return cp_id

    async def checkpoints(self) -> list[dict[str, Any]]:
        """列出当前 session 的所有快照（按时间升序）。"""
        ns = self._checkpoint_namespace()
        cps = await self._load_checkpoints(ns)
        return sorted(cps.values(), key=lambda x: x.get("created_at", 0.0))

    async def rollback(self, checkpoint_id: str | None = None) -> str:
        """回滚到指定快照（默认最近一个）。

        Args:
            checkpoint_id: 目标快照 id；None 表示最近一个。

        Returns:
            实际回滚到的 checkpoint_id

        Raises:
            ValueError: 无任何快照，或指定 checkpoint_id 不存在
        """
        ns = self._checkpoint_namespace()
        cps = await self._load_checkpoints(ns)

        if not cps:
            raise ValueError("No checkpoints available to rollback to")

        if checkpoint_id is None:
            cp = max(cps.values(), key=lambda x: x.get("created_at", 0.0))
        else:
            if checkpoint_id not in cps:
                raise ValueError(
                    f"Checkpoint '{checkpoint_id}' not found. "
                    f"Available: {sorted(cps)}"
                )
            cp = cps[checkpoint_id]

        # 回滚 state：清空"宿主 state 键"（保留 checkpoint 自身的键）再恢复快照值
        for state_ns, snapshot in cp.get("state", {}).items():
            keys = await self._memory.state.list(state_ns)
            for k in keys:
                if k.startswith(_CP_PREFIX):
                    continue  # 不删除 checkpoint 自身
                await self._memory.state.delete(k, state_ns)
            for k, v in snapshot.items():
                if k.startswith(_CP_PREFIX):
                    continue  # 快照里不该有 checkpoint 键，防御性跳过
                await self._memory.state.set(k, v, state_ns)

        # 回滚 stream：删除快照后新增的条目
        watermark = float(cp.get("stream_watermark", 0.0))
        for stream_ns in cp.get("stream_namespaces", []):
            await self._memory.stream.delete_after(watermark, stream_ns)

        return cp["id"]

    async def _enforce_keep(self, namespace: str) -> None:
        """淘汰最旧的快照，保留最近 keep 个。"""
        keep = self._config.keep
        if keep <= 0:
            return
        cps = await self._load_checkpoints(namespace)
        if len(cps) <= keep:
            return
        sorted_cps = sorted(cps.items(), key=lambda x: x[1].get("created_at", 0.0))
        to_delete = sorted_cps[: len(sorted_cps) - keep]
        for cp_id, _ in to_delete:
            await self._memory.state.delete(self._cp_key(cp_id), namespace)
