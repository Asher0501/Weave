"""能力层测试：TraceSink 钩子收集（经 demos/atomic 循环）+ CheckpointManager + 原子面检查。"""
import asyncio
import pathlib

import weave.core
from demos.atomic.agent import DemoAgent
from demos.atomic.loop import run_turn
from demos.atomic.context_builder import ContextBuilder
from weave.capabilities.checkpoint import CheckpointManager
from weave.capabilities.trace import TraceSink
from weave.providers.fake import FakeProvider, FakeTurn
from weave.stores.memory import InMemoryStateStore


def test_trace_sink_collects_events():
    agent = DemoAgent(
        FakeProvider(turns=[FakeTurn(content="hi")]),
        InMemoryStateStore(),
        agent_id="t",
    )
    sink = TraceSink()
    result = asyncio.run(agent.call("q", sink=sink))
    assert result.output == "hi"
    summary = sink.summary()
    assert summary.get("loop_start") == 1
    assert summary.get("iteration_start") == 1
    assert summary.get("llm") == 1
    assert summary.get("loop_end") == 1


def test_checkpoint_snapshot_restore():
    store = InMemoryStateStore()
    mgr = CheckpointManager(store)
    ns = "demo:a1:state"

    async def _t():
        await store.set(ns, "k", 1)
        cid = await mgr.snapshot(ns)
        await store.set(ns, "k", 2)
        assert await store.get(ns, "k") == 2
        await mgr.restore(ns, cid)
        assert await store.get(ns, "k") == 1
        assert cid in await mgr.list_checkpoints()

    asyncio.run(_t())


def test_core_is_atomic_no_import_of_demos_capabilities():
    """原子契约自检：weave.core 源码不 import 能力/示例/组合层（注释词不算）。"""
    import re

    core_dir = pathlib.Path(weave.core.__file__).parent
    offenders = []
    for py in core_dir.glob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*(import|from)\s+", line) and (
                "capabilities" in line or "demos" in line or "weave.agent" in line
            ):
                offenders.append(f"{py.name}: {line.strip()}")
    assert offenders == [], f"core 必须保持原子：{offenders}"


def test_weave_top_level_exposes_no_convenience():
    import weave

    for name in ("Agent", "agent", "context", "loops", "tools", "dx"):
        assert not hasattr(weave, name), f"weave 不应暴露组合便利：{name}"
    for removed in ("AgentResult", "LoopOutcome", "ContextBuilder", "Loop"):
        assert not hasattr(weave.core, removed), f"core 不应包含 {removed}"
