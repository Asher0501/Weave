"""示例：极薄的组装容器（调用方代码，不在 weave 包内）。

只演示"用原子件组合一个可调用对象"；要不要它、怎么命名、默认值是什么，
全由调用方决定。weave 本身不提供 Agent。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from weave.core.interfaces import EventSink, Provider, StateStore, ToolRegistry
from .context_builder import ContextBuilder, EXAMPLE_SYSTEM_PROMPT
from .loop import Outcome, run_turn
from .tool_registry import PythonToolRegistry


@dataclass(slots=True)
class DemoResult:
    output: str = ""
    iterations: int = 0
    elapsed_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    writes: dict[tuple[str, str], int] = field(default_factory=dict)

    @classmethod
    def from_outcome(cls, o: Outcome, elapsed_ms: int) -> "DemoResult":
        return cls(
            output=o.output,
            iterations=o.iterations,
            elapsed_ms=elapsed_ms,
            usage=dict(o.usage),
            writes=dict(o.writes),
        )


class DemoAgent:
    """最小容器示例：组装 provider/store/registry/builder/循环，全参数显式。"""

    def __init__(
        self,
        provider: Provider,
        state_store: StateStore,
        *,
        agent_id: str = "default",
        tool_registry: ToolRegistry | None = None,
        system_prompt: str | None = EXAMPLE_SYSTEM_PROMPT,
        history_locator: Callable[[str], tuple[str, str]] | None = None,
        max_iterations: int = 10,
    ) -> None:
        self.provider = provider
        self.state_store = state_store
        self.agent_id = agent_id
        self.tools = tool_registry if tool_registry is not None else PythonToolRegistry()
        self._builder = ContextBuilder(
            system_prompt=system_prompt, locate_history=history_locator
        )
        self._history_locator = history_locator
        self._max_iterations = max_iterations

    def tool(self, fn: Any = None, *, name: str | None = None, description: str | None = None) -> Any:
        return self.tools.tool(fn, name=name, description=description)

    async def call(self, user_input: str, sink: EventSink | None = None, **kwargs: Any) -> DemoResult:
        t0 = time.monotonic()
        outcome = await run_turn(
            agent_id=self.agent_id,
            provider=self.provider,
            state=self.state_store,
            context_builder=self._builder,
            tools=self.tools,
            initial_input=user_input,
            sink=sink,
            locate_history=self._history_locator,
            max_iterations=self._max_iterations,
            **kwargs,
        )
        return DemoResult.from_outcome(outcome, int((time.monotonic() - t0) * 1000))
