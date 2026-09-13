"""原子组装 + 示例容器测试：无状态默认 / 显式历史选址的多轮连续性 / 工具循环 /
错误恢复 / 自定义上下文来源 / 业务自选命名空间隔离。

weave 只提供原子件；本文件通过 demos/atomic 的调用方示例组合来验证语义。
"""
import asyncio

from weave.core.interfaces import StateStore
from weave.core.types import Message, ToolCall
from weave.providers.fake import FakeProvider, FakeTurn
from weave.stores.memory import InMemoryStateStore
from demos.atomic.agent import DemoAgent
from demos.atomic.context_builder import ContextBuilder
from demos.atomic.loop import run_turn
from demos.atomic.tool_registry import PythonToolRegistry


def run(coro):
    return asyncio.run(coro)


# 业务自选的历史选址（零约定示例）
def LOC(agent_id: str) -> tuple[str, str]:
    return (f"conv:{agent_id}", "log")


def test_default_is_stateless_without_locator():
    store = InMemoryStateStore()
    prov = FakeProvider(turns=[FakeTurn(content="ans1"), FakeTurn(content="ans2")])
    agent = DemoAgent(prov, store, agent_id="x")
    r1 = run(agent.call("你好"))
    r2 = run(agent.call("继续"))
    assert r1.output == "ans1" and r2.output == "ans2"
    assert r1.writes == {} and r2.writes == {}
    contents = [(m.role, m.content) for m in prov.calls[1]]
    assert ("assistant", "ans1") not in contents  # 第二轮看不到第一轮


def test_multiturn_continuity_with_explicit_locator():
    store = InMemoryStateStore()
    prov = FakeProvider(turns=[FakeTurn(content="first-ans"), FakeTurn(content="second-ans")])
    agent = DemoAgent(prov, store, agent_id="a1", history_locator=LOC)
    assert run(agent.call("你好")).output == "first-ans"
    r2 = run(agent.call("继续"))
    assert r2.output == "second-ans"
    contents = [(m.role, m.content) for m in prov.calls[1]]
    assert ("user", "你好") in contents
    assert ("assistant", "first-ans") in contents
    assert contents.count(("user", "继续")) == 1
    assert ("conv:a1", "log") in r2.writes


def test_tool_loop_end_to_end():
    store = InMemoryStateStore()
    prov = FakeProvider(
        turns=[
            FakeTurn(tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})]),
            FakeTurn(content="和是 3"),
        ]
    )
    reg = PythonToolRegistry()

    @reg.tool
    def add(a: int, b: int) -> int:
        return a + b

    agent = DemoAgent(prov, store, agent_id="calc", tool_registry=reg, history_locator=LOC)
    result = run(agent.call("1+2?"))
    assert result.output == "和是 3"
    assert result.iterations == 2
    transcript = run(store.get("conv:calc", "log"))
    assert [m["role"] for m in transcript] == ["user", "assistant", "tool", "assistant"]


def test_tool_error_recovered_and_continues():
    store = InMemoryStateStore()
    prov = FakeProvider(
        turns=[
            FakeTurn(tool_calls=[ToolCall(id="c1", name="boom", arguments={})]),
            FakeTurn(content="已处理失败"),
        ]
    )
    reg = PythonToolRegistry()

    @reg.tool
    def boom() -> int:
        raise ValueError("kaboom")

    agent = DemoAgent(prov, store, agent_id="e", tool_registry=reg, history_locator=LOC)
    assert run(agent.call("触发")).output == "已处理失败"
    tool_msgs = [m for m in run(store.get("conv:e", "log")) if m["role"] == "tool"]
    assert "[tool error]" in tool_msgs[-1]["content"]


class _ExternalSourceBuilder:
    """DoD A2：自定义上下文来源（外部文本），任意对象满足 build 即可被原子循环使用。"""

    def __init__(self, external: str):
        self._external = external

    async def build(self, *, agent_id: str, state: StateStore, current_input: str | None, **context):
        return [
            Message(role="system", content=self._external),
            Message(role="user", content=f"[external] {current_input}"),
        ]


def test_custom_context_source_with_atomic_loop():
    store = InMemoryStateStore()
    prov = FakeProvider(turns=[FakeTurn(content="ok")])
    outcome = run(
        run_turn(
            agent_id="x",
            provider=prov,
            state=store,
            context_builder=_ExternalSourceBuilder("来自外部系统的规则"),
            tools=None,
            initial_input="问",
        )
    )
    assert outcome.output == "ok"
    seen = prov.calls[0]
    assert any(m.role == "system" and m.content == "来自外部系统的规则" for m in seen)
    assert seen[-1].content == "[external] 问"


def test_business_defined_namespaces_isolate():
    store = InMemoryStateStore()
    a = DemoAgent(FakeProvider(turns=[FakeTurn(content="A答")]), store, agent_id="pa", history_locator=LOC)
    b = DemoAgent(FakeProvider(turns=[FakeTurn(content="B答")]), store, agent_id="pb", history_locator=LOC)
    run(a.call("q"))
    run(b.call("q"))
    a_ans = run(store.get("conv:pa", "log"))[1]["content"]
    b_ans = run(store.get("conv:pb", "log"))[1]["content"]
    assert a_ans == "A答" and b_ans == "B答" and a_ans != b_ans


def test_sample_context_builder_no_double_inject():
    """示例 builder 与循环配合：当前输入在投影里只出现一次（尾部去重）。"""
    store = InMemoryStateStore()
    prov = FakeProvider(turns=[FakeTurn(content="ok"), FakeTurn(content="ok2")])
    agent = DemoAgent(prov, store, agent_id="d", history_locator=LOC)
    run(agent.call("hi"))
    run(agent.call("hi"))  # 再次相同输入：仍是一个新轮次
    last_msgs = prov.calls[1]
    roles_content = [(m.role, m.content) for m in last_msgs]
    # 结尾必须是单个 user 'hi'（未被重复注入），其前是上轮 assistant
    assert roles_content[-1] == ("user", "hi")
    assert roles_content[-2] == ("assistant", "ok")
    assert ("user", "hi") in roles_content
    assert roles_content.count(("user", "hi")) == 2  # 第1轮 + 第2轮各一次

