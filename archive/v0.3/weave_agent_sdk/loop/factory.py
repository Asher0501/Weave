"""Loop 注册表。内置策略 = 预注册默认值（见 docs/issues/010）。"""
from __future__ import annotations

from weave_agent_sdk.registry import Registry
from weave_agent_sdk.loop.iterative import IterativeLoop
from weave_agent_sdk.loop.scheduled import ScheduledLoop
from weave_agent_sdk.loop.simple import SimpleLoop

LOOP_REGISTRY = Registry("loop type")
LOOP_REGISTRY.register("simple", SimpleLoop)
LOOP_REGISTRY.register("iterative", IterativeLoop)
LOOP_REGISTRY.register("scheduled", ScheduledLoop)
