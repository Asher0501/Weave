"""状态回滚（checkpoint/rollback）测试（docs/issues/012）。

覆盖第一层 Memory 认知状态回滚：
- state 键值快照/恢复
- stream 时序流快照/回滚（时间水位线）
- fork 语义（快照 append-only，可反复回滚）
- keep 淘汰
- 边界（无快照 / 未知 id）
- 自动打点（checkpoint.enabled + after_each_tool）
"""
from __future__ import annotations

import asyncio
import textwrap

import pytest

from weave_agent_sdk import Weave
from weave_agent_sdk.checkpoint import CheckpointManager
from weave_agent_sdk.llm.base import BaseLLM, LLMResponse
from weave_agent_sdk.memory.manager import MemoryManager
from weave_agent_sdk.types import (
    CheckpointConfig,
    MemoryConfig,
    MemoryScopeConfig,
    ToolCall,
)


# ── 测试替身 ──────────────────────────────────────────────

class _FakeLLM(BaseLLM):
    async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="fake")

    async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        yield "fake"


def _make_manager(tmp_path, session_id: str = "s1") -> MemoryManager:
    """构造一个临时 sqlite 的 MemoryManager，激活 session scope。"""
    db = str(tmp_path / "memory.db")
    cfg = MemoryConfig(
        scopes={
            "session": MemoryScopeConfig(
                priority=0,
                stream={"backend": "sqlite", "path": db},
                state={"backend": "sqlite", "path": db},
            ),
        },
        default_path=db,
    )
    mem = MemoryManager(cfg)
    mem.activate_scopes({"session_id": session_id})
    return mem


def _run(coro):
    return asyncio.run(coro)


# ── 核心：state 快照/回滚 ───────────────────────────────

def test_state_checkpoint_rollback(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True))
        ns = mem.get_namespace("session", "state")

        await mem.state.set("topic", "A", ns)
        cp_id = await cp.checkpoint()

        await mem.state.set("topic", "B", ns)
        await mem.state.set("extra", "new", ns)

        await cp.rollback(cp_id)

        assert await mem.state.get("topic", ns) == "A"   # 恢复旧值
        assert await mem.state.get("extra", ns) is None  # 删除新增 key

    _run(scenario())


def test_stream_checkpoint_rollback(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True))
        sns = mem.get_namespace("session", "stream")

        await mem.stream.append({"role": "user", "content": "msg1"}, sns)
        cp_id = await cp.checkpoint()

        await mem.stream.append({"role": "user", "content": "msg2"}, sns)

        await cp.rollback(cp_id)

        msgs = await mem.stream.last(10, [sns])
        contents = [m.get("content") for m in msgs]
        assert contents == ["msg1"]  # 删除快照后新增的 msg2

    _run(scenario())


# ── fork 语义 ──────────────────────────────────────────

def test_fork_semantics_preserves_checkpoints(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True, keep=10))
        ns = mem.get_namespace("session", "state")

        await mem.state.set("topic", "A", ns)
        cp1 = await cp.checkpoint()
        await mem.state.set("topic", "B", ns)
        cp2 = await cp.checkpoint()

        await cp.rollback(cp1)  # 回滚到第一个

        # 两个快照都还在（append-only，不因回滚删除）
        cps = await cp.checkpoints()
        ids = [c["id"] for c in cps]
        assert cp1 in ids and cp2 in ids

    _run(scenario())


# ── 边界 ──────────────────────────────────────────────

def test_rollback_without_checkpoint_raises(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True))
        with pytest.raises(ValueError, match="No checkpoints"):
            await cp.rollback()

    _run(scenario())


def test_rollback_unknown_id_raises(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True))
        ns = mem.get_namespace("session", "state")
        await mem.state.set("topic", "A", ns)
        await cp.checkpoint()
        with pytest.raises(ValueError, match="not found"):
            await cp.rollback("cp_nonexistent")

    _run(scenario())


def test_rollback_default_targets_latest(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True))
        ns = mem.get_namespace("session", "state")

        await mem.state.set("topic", "A", ns)
        await cp.checkpoint()
        await mem.state.set("topic", "B", ns)
        await cp.checkpoint()

        await cp.rollback(None)  # 回滚到最近一个 = B

        assert await mem.state.get("topic", ns) == "B"

    _run(scenario())


# ── keep 淘汰 ─────────────────────────────────────────

def test_keep_enforcement_evicts_oldest(tmp_path):
    async def scenario():
        mem = _make_manager(tmp_path)
        cp = CheckpointManager(mem, CheckpointConfig(enabled=True, keep=2))
        ns = mem.get_namespace("session", "state")

        for i in range(3):
            await mem.state.set("topic", f"v{i}", ns)
            await cp.checkpoint()

        cps = await cp.checkpoints()
        assert len(cps) == 2  # 只保留最近 2 个
        topics = [c["state"][ns]["topic"] for c in cps]
        assert "v0" not in topics  # 最旧的 v0 被淘汰

    _run(scenario())


# ── 跨 session 隔离 ──────────────────────────────────

def test_checkpoint_isolated_by_session(tmp_path):
    async def scenario():
        mem_a = _make_manager(tmp_path, session_id="a")
        mem_b = _make_manager(tmp_path, session_id="b")
        cp_a = CheckpointManager(mem_a, CheckpointConfig(enabled=True))
        cp_b = CheckpointManager(mem_b, CheckpointConfig(enabled=True))

        await cp_a.checkpoint()
        await cp_a.checkpoint()

        # session b 看不到 session a 的快照
        assert await cp_b.checkpoints() == []

    _run(scenario())


# ── 自动打点集成测试 ────────────────────────────────────

class _ToolCallingLLM(BaseLLM):
    """第一次返回 tool_call，之后返回无 tool_call，驱动 IterativeLoop 打点。"""

    def __init__(self):
        self.n = 0

    async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        self.n += 1
        if self.n == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="do_it", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        yield "x"


def test_auto_checkpoint_after_tool(tmp_path):
    """checkpoint.enabled + trigger=after_each_tool 时，tool 执行后自动打点。"""
    db = str(tmp_path / "mem.db")
    cfg_path = tmp_path / "weave.yaml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            llm:
              provider: anthropic
              model: fake
            loop:
              type: iterative
              max_iterations: 5
            checkpoint:
              enabled: true
              trigger: after_each_tool
              keep: 10
            memory:
              scopes:
                session:
                  stream: {{backend: sqlite, path: {db}}}
                  state: {{backend: sqlite, path: {db}}}
        """),
        encoding="utf-8",
    )

    weave = Weave(str(cfg_path), llm=_ToolCallingLLM())

    @weave.tool
    def do_it() -> str:
        return "done tool"

    result = weave.run("go", scope_hints={"session_id": "s1"})

    assert result.output == "done"
    cps = weave.checkpoints()
    assert len(cps) == 1  # tool 执行后自动打了一个快照
    assert cps[0]["state_namespaces"] == ["session:s1:state"]
