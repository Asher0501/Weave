"""Weave — 非侵入式 Agent SDK。

为任意 Python 项目提供 Loop（循环）+ Memory（记忆）机制。

用法:
    from weave_agent_sdk import Weave
    weave = Weave("weave.yaml")
    result = weave.run("我应该学什么?")
"""

from weave_agent_sdk.agent import Weave
from weave_agent_sdk.config import load_config
from weave_agent_sdk.memory.manager import MemoryManager
from weave_agent_sdk.types import (
    LoopResult,
    MemoryConfig,
    MemoryEntry,
    Message,
    SearchResult,
    ToolCall,
    ToolResult,
    WeaveConfig,
    WeaveEvent,
)

# 版本号单点声明（pyproject.toml 经 dynamic 从本处读取，见 [tool.setuptools.dynamic]）
__version__ = "0.3.0"


# ── 插件注册（docs/issues/010） ──────────────────────────
# 内置实现 = 预注册默认值；宿主可注册自定义实现，并经 weave.yaml 引用
# （provider / loop.type / backend 名称）。

def register_llm(name: str, factory, *, replace: bool = False) -> None:
    """注册自定义 LLM adapter。``name`` 即 weave.yaml 中 ``llm.provider`` 的取值。"""
    from weave_agent_sdk.llm.factory import LLM_REGISTRY
    LLM_REGISTRY.register(name, factory, replace=replace)


def register_loop(name: str, factory, *, replace: bool = False) -> None:
    """注册自定义 Loop 策略。``name`` 即 weave.yaml 中 ``loop.type`` 的取值。"""
    from weave_agent_sdk.loop.factory import LOOP_REGISTRY
    LOOP_REGISTRY.register(name, factory, replace=replace)


def register_memory_backend(name: str, factory, *, replace: bool = False) -> None:
    """注册自定义 Memory 后端。``name`` 即 weave.yaml 中各 scope ``backend`` 的取值。"""
    from weave_agent_sdk.memory.manager import BACKEND_REGISTRY
    BACKEND_REGISTRY.register(name, factory, replace=replace)


__all__ = [
    "Weave",
    "load_config",
    "register_llm",
    "register_loop",
    "register_memory_backend",
    "WeaveConfig",
    "MemoryConfig",
    "MemoryManager",
    "LoopResult",
    "SearchResult",
    "MemoryEntry",
    "Message",
    "ToolCall",
    "ToolResult",
    "WeaveEvent",
    "__version__",
]
