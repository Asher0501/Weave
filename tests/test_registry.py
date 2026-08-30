"""正向测试：插件注册与注入（docs/issues/010）。

验证 register_llm / register_loop / register_memory_backend 与
Weave(llm=, loop=) 注入确能生效——「可插拔」承诺的契约测试。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from weave import Weave, register_llm, register_loop, register_memory_backend
from weave.llm.base import BaseLLM, LLMResponse
from weave.llm.factory import LLM_REGISTRY, create_llm
from weave.loop.base import BaseLoop
from weave.loop.factory import LOOP_REGISTRY
from weave.memory.manager import BACKEND_REGISTRY, MemoryManager
from weave.types import LoopResult, MemoryConfig, MemoryScopeConfig


# ── 测试替身 ──────────────────────────────────────────────

class _FakeLLM(BaseLLM):
    async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="fake")

    async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
        yield "fake"


class _CustomLoop(BaseLoop):
    async def run(self, agent, user_input):
        return LoopResult(output="custom", elapsed_ms=0, iterations=1, memory_updated={})


class _CustomBackend:
    def __init__(self, db_path):
        self.db_path = db_path


def _minimal_config(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text(
        "agent:\n  name: t\nllm:\n  provider: anthropic\n  model: x\n"
        "loop:\n  type: simple\nmemory:\n  scopes: {}\n"
        "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
        encoding="utf-8",
    )
    return p


# ── Loop 注册 ─────────────────────────────────────────────

def test_register_loop_resolvable():
    register_loop("zzz_test_custom_loop", _CustomLoop)
    assert "zzz_test_custom_loop" in LOOP_REGISTRY.names()
    assert isinstance(LOOP_REGISTRY.create("zzz_test_custom_loop"), _CustomLoop)


def test_create_loop_picks_registered_loop():
    register_loop("zzz_test_custom_loop2", _CustomLoop)
    weave = Weave.__new__(Weave)
    weave._config = MagicMock()
    weave._config.loop.type = "zzz_test_custom_loop2"
    weave._tools = []
    weave._tool_map = {}
    assert isinstance(weave._create_loop(), _CustomLoop)


def test_register_loop_duplicate_raises():
    with pytest.raises(ValueError):
        register_loop("simple", _CustomLoop)  # 内置已存在


def test_register_loop_replace_allows_override():
    register_loop("zzz_test_replace", _CustomLoop)
    register_loop("zzz_test_replace", _CustomLoop, replace=True)
    assert LOOP_REGISTRY.get("zzz_test_replace").__name__ == "_CustomLoop"


def test_unknown_loop_lists_known_names():
    with pytest.raises(ValueError) as e:
        LOOP_REGISTRY.create("zzz_nope")
    msg = str(e.value)
    assert "Unknown loop type 'zzz_nope'" in msg
    assert "simple, iterative, scheduled" in msg


# ── LLM 注册 ─────────────────────────────────────────────

def test_register_llm_resolvable():
    def factory(api_key, model, base_url, auth_token=None):
        return _FakeLLM()

    register_llm("zzz_test_provider", factory)
    llm = create_llm(provider="zzz_test_provider", model="m", api_key="k")
    assert isinstance(llm, _FakeLLM)


# ── Memory 后端注册 ───────────────────────────────────────

def test_register_memory_backend_resolvable():
    register_memory_backend("zzz_test_backend", _CustomBackend)
    scopes = {"s": MemoryScopeConfig(stream={"backend": "zzz_test_backend", "path": ":memory:"})}
    manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))
    backend = manager._get_backend_for_namespace("s:s1:stream")
    assert isinstance(backend, _CustomBackend)


# ── Weave 注入 ────────────────────────────────────────────

def test_weave_llm_injection_skips_create_llm(tmp_path):
    fake = _FakeLLM()
    weave = Weave(str(_minimal_config(tmp_path)), llm=fake)
    assert weave._llm is fake


def test_weave_loop_injection_skips_create_loop(tmp_path):
    loop = _CustomLoop()
    weave = Weave(str(_minimal_config(tmp_path)), loop=loop)
    assert weave._loop is loop
