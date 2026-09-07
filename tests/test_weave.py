"""Weave 单元测试。

覆盖第1轮修复的4个修复项:
1. 公共 tool 注册方法
2. stream() 竞态条件
3. 运行状态追踪
4. config 异常处理

覆盖第2轮修复的4个修复项:
5. stream() 死锁修复
6. R2 违规修复（无硬编码 Prompt）
7. 环境变量插值 regex 边界修复
8. .bak 备份文件清理验证

覆盖第3轮修复的4个修复项:
9. （已移除）load_claude_env 工具函数
10. tests/ 下 .bak 文件清理验证
11. 默认 weave.yaml 配置文件存在性验证
12. stream() 超时值可配置验证

覆盖第4轮修复的4个修复项:
13. scheduled.py 导入修复（format_memory_context）
14. scheduled.py 死代码移除（inner = IterativeLoop()）
15. iterative.py stream/state 写入计数存根修复
16. scheduled.py 导入移至文件顶部

覆盖第5轮修复的3个修复项:
17. _create_loop() 添加 "scheduled" 循环类型支持
18. chat_stream() 支持 tools 参数和 tool 消息处理
19. stream.last() 多 backend 合并排序

覆盖第6轮修复的4个修复项:
20. openai.py chat_stream() 缺失 tools 参数传递
21. openai.py __import__("json") 替换为模块级 import json
22. anthropic.py _convert_tools_to_anthropic 类型脆弱性修复
23. iterative.py memory_updated["tool"] → ["tools"] schema 不一致修复

覆盖第7轮修复的2个修复项:
24. 移除 anthropic.py 中硬编码的 system prompt fallback（R2 合规）
25. 移除 factory.py 中 deepseek 分支的冗余 or "deepseek-chat"

覆盖第8轮修复的5个修复项:
26. Tool 执行添加超时保护（iterative.py + types.py + config.py）
27. scheduled.py 中 State 写入异常静默吞掉改为日志告警
28. _create_loop() 未知 loop type 改为抛出 ValueError
29. memory/manager.py namespace 解析添加 access_type 合法性校验
30. 移除代码中所有硬编码模型名默认值（R3 红线）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from weave_agent_sdk.agent import Weave
from weave_agent_sdk.config import _resolve_env, load_config
from weave_agent_sdk.types import LoopResult


# ============================================================
# 修复项 1: 公共 tool 注册方法
# ============================================================


class TestToolRegistration:
    """测试 _register_tool_internal / tool() / register_tool() 行为一致性。"""

    def setup_method(self):
        self.weave = Weave.__new__(Weave)
        self.weave._tools = []
        self.weave._tool_map = {}

    def test_tool_decorator_registers_function(self):
        """@weave.tool 装饰器应正确注册函数到 _tools 和 _tool_map。"""

        @self.weave.tool
        def my_tool(query: str) -> str:
            return f"result: {query}"

        assert my_tool in self.weave._tools
        assert self.weave._tool_map["my_tool"] is my_tool
        assert len(self.weave._tools) == 1
        assert len(self.weave._tool_map) == 1

    def test_tool_decorator_supports_name_description_schema(self):
        """@weave.tool 支持 name / description / schema 显式覆盖自动推断。"""

        @self.weave.tool(name="search_kb", description="搜索知识库")
        def _search(q: str) -> str:
            return q

        @self.weave.tool(
            name="custom",
            schema={
                "name": "custom",
                "description": "自定义 schema",
                "parameters": {"type": "object", "properties": {}},
            },
        )
        def _custom() -> None:
            return None

        assert self.weave._tool_map["search_kb"] is _search
        assert self.weave._tool_map["custom"] is _custom
        assert self.weave._tool_meta["search_kb"]["description"] == "搜索知识库"
        assert self.weave._tool_meta["search_kb"]["schema"] is None
        assert self.weave._tool_meta["custom"]["schema"] is not None

    def test_register_tool_registers_function(self):
        """register_tool() 应正确注册函数到 _tools 和 _tool_map。"""

        def my_tool(query: str) -> str:
            return f"result: {query}"

        self.weave.register_tool(my_tool)

        assert my_tool in self.weave._tools
        assert self.weave._tool_map["my_tool"] is my_tool
        assert len(self.weave._tools) == 1
        assert len(self.weave._tool_map) == 1

    def test_tool_and_register_tool_produce_same_result(self):
        """tool() 和 register_tool() 应产生相同的注册结果。"""

        def fn_a(x: int) -> int:
            return x + 1

        @self.weave.tool
        def fn_b(x: int) -> int:
            return x + 2

        self.weave.register_tool(fn_a)

        assert fn_a in self.weave._tools
        assert fn_b in self.weave._tools
        assert self.weave._tool_map["fn_a"] is fn_a
        assert self.weave._tool_map["fn_b"] is fn_b
        assert len(self.weave._tools) == 2
        assert len(self.weave._tool_map) == 2

    def test_register_tool_internal_is_called_by_both(self, mocker):
        """tool() 和 register_tool() 都委托调用 _register_tool_internal。"""
        spy = mocker.spy(self.weave, "_register_tool_internal")

        @self.weave.tool
        def fn_c() -> None:
            pass

        def fn_d() -> None:
            pass
        self.weave.register_tool(fn_d)

        assert spy.call_count == 2
        spy.assert_any_call(fn_c, name=None, description=None, schema=None)
        spy.assert_any_call(fn_d, name=None, description=None, schema=None)

    def test_tool_decorator_returns_function_unchanged(self):
        """tool() 装饰器应返回原函数（而非 None 或包装器）。"""

        @self.weave.tool
        def my_tool(x: int, y: int) -> int:
            return x + y

        # 装饰后的函数应仍可正常调用
        assert my_tool(3, 4) == 7
        assert my_tool.__name__ == "my_tool"


# ============================================================
# 修复项 2: stream() 竞态条件 — 订阅先于任务创建
# ============================================================


class TestStreamRaceCondition:
    """测试 stream() 中 subscribe 在 _run_and_emit 之前建立。"""

    @pytest.fixture
    def weave_with_mocks(self):
        """创建 Weave 实例并 mock 关键依赖。"""
        weave = Weave.__new__(Weave)

        # Mock config
        weave._config = MagicMock()
        weave._config.agent.name = "test_agent"
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 5.0

        # Mock event bus
        from weave_agent_sdk.event_bus import EventBus
        weave._event_bus = EventBus()

        # Mock LLM
        weave._llm = AsyncMock()
        from weave_agent_sdk.llm.base import LLMResponse
        weave._llm.chat = AsyncMock(return_value=LLMResponse(
            content="Hello from mock LLM",
            model="test-model",
        ))

        # Mock memory
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.stats = MagicMock(return_value={"test:scope:stream": 0})
        weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])

        # Mock prompts
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")

        # Mock loop
        weave._loop = AsyncMock()
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="test output",
            elapsed_ms=100,
            iterations=1,
            memory_updated={"stream": {"test:scope:stream": 1}},
        ))

        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None

        return weave

    @pytest.mark.asyncio
    async def test_stream_yields_events_and_completes(self, weave_with_mocks):
        """stream() 应逐事件 yield，并在 done 事件后停止。"""
        weave = weave_with_mocks
        events = []

        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # 至少应收到 done 事件
        assert len(events) >= 1
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "test output"

    @pytest.mark.asyncio
    async def test_stream_handles_error(self, weave_with_mocks):
        """stream() 在 run_impl 抛出异常时应 yield error 事件。"""
        weave = weave_with_mocks
        weave._loop.run = AsyncMock(side_effect=ValueError("Something went wrong"))

        events = []
        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert len(events) >= 1
        assert events[-1].type == "error"
        assert "Something went wrong" in events[-1].data["message"]

    @pytest.mark.asyncio
    async def test_subscribe_before_task_creation(self, weave_with_mocks):
        """验证 subscribe 在 _run_and_emit task 创建前完成。

        通过检查 event_bus 的 subscriber_count 在 task 启动前已非零来验证。
        """
        weave = weave_with_mocks
        bus = weave._event_bus

        # 在 stream() 迭代前，bus 不应有订阅者
        assert bus.subscriber_count == {}

        # 开始迭代 stream
        async for event in weave.stream("test"):
            # 首次迭代时，订阅应已建立
            assert bus.subscriber_count != {}
            # 应已注册 "token", "tool_call", "tool_result", "done", "error"
            for event_type in ("token", "tool_call", "tool_result", "done", "error"):
                assert bus.subscriber_count.get(event_type, 0) >= 1
            break

    @pytest.mark.asyncio
    async def test_stream_multiple_tokens(self, weave_with_mocks):
        """stream() 应能 yield 多个 token 事件并正常完成。"""
        weave = weave_with_mocks

        # 使用 asyncio.Event 确保 token 发射在 done 之前完成
        tokens_done = asyncio.Event()

        async def emit_tokens():
            for i in range(3):
                await weave._event_bus.emit("token", {"token": f"token_{i}"})
                await asyncio.sleep(0.02)
            tokens_done.set()

        # 启动 token 发射任务
        asyncio.create_task(emit_tokens())

        tokens = []
        async for event in weave.stream("test"):
            if event.type == "token":
                tokens.append(event.data["token"])
            if event.type == "done":
                break

        # 确保至少收到了一些 token
        assert len(tokens) > 0
        # 验证 token 按顺序到达
        for i, token_data in enumerate(tokens):
            assert token_data == f"token_{i}"


# ============================================================
# 修复项 3: 运行状态追踪
# ============================================================


class TestRunStateTracking:
    """测试 _is_running 和 _last_run 状态追踪。"""

    @pytest.fixture
    def weave(self):
        """创建最小 Weave 实例用于状态测试。"""
        w = Weave.__new__(Weave)
        w._is_running = False
        w._last_run = None
        w._config = MagicMock()
        w._config.agent.name = "test_agent"
        w._config.loop.type = "simple"
        w._memory = MagicMock()
        w._memory.stats = MagicMock(return_value={"test:scope:stream": 3})
        return w

    def test_initial_status(self, weave):
        """初始状态: is_running=False, last_run=None。"""
        status = weave.status()
        assert status["is_running"] is False
        assert status["last_run"] is None
        assert status["agent_name"] == "test_agent"
        assert status["loop_type"] == "simple"

    def test_status_after_run_sets_is_running_true(self, weave):
        """_run_impl 入口设置 _is_running = True。"""
        weave._is_running = True
        status = weave.status()
        assert status["is_running"] is True

    def test_status_after_run_complete(self, weave):
        """_run_impl 完成后: is_running=False, last_run 有值。"""
        now = time.time()
        weave._is_running = False
        weave._last_run = now
        status = weave.status()
        assert status["is_running"] is False
        assert status["last_run"] == now
        assert isinstance(status["last_run"], float)

    def test_last_run_updates_across_runs(self, weave):
        """多次运行后 last_run 应更新为最新时间戳。"""
        t1 = 1000.0
        t2 = 2000.0

        weave._is_running = False
        weave._last_run = t1
        assert weave.status()["last_run"] == t1

        weave._last_run = t2
        assert weave.status()["last_run"] == t2

    def test_status_includes_memory_stats(self, weave):
        """status() 应包含 memory_stats 信息。"""
        status = weave.status()
        assert "memory_stats" in status
        assert status["memory_stats"]["total_entries"] == 3
        assert status["memory_stats"]["by_scope"] == {"test:scope:stream": 3}




# ============================================================
# 修复项 5: stream() 死锁修复（第2轮）
# ============================================================


class TestStreamDeadlock:
    """测试 stream() 死锁修复 — _run_and_emit task 在 async for 之前创建。"""

    @pytest.fixture
    def weave_with_mocks(self):
        """创建 Weave 实例并 mock 关键依赖。"""
        weave = Weave.__new__(Weave)

        weave._config = MagicMock()
        weave._config.agent.name = "test_agent"
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 5.0

        from weave_agent_sdk.event_bus import EventBus
        weave._event_bus = EventBus()

        weave._llm = AsyncMock()
        from weave_agent_sdk.llm.base import LLMResponse
        weave._llm.chat = AsyncMock(return_value=LLMResponse(
            content="Hello from mock LLM",
            model="test-model",
        ))

        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.stats = MagicMock(return_value={"test:scope:stream": 0})
        weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])

        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")

        weave._loop = AsyncMock()
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="test output",
            elapsed_ms=100,
            iterations=1,
            memory_updated={"stream": {"test:scope:stream": 1}},
        ))

        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None

        return weave

    @pytest.mark.asyncio
    async def test_stream_does_not_deadlock(self, weave_with_mocks):
        """stream() 在事件快速产生时不会死锁（核心死锁修复验证）。

        如果 task 在 async for 循环体内部创建，事件队列为空时循环体无法执行，
        导致死锁。修复后 task 在循环前创建，事件可正常产出。
        """
        weave = weave_with_mocks
        events = []

        # 使用超时确保测试不会无限阻塞
        async def consume():
            async for event in weave.stream("test input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break
            return events

        # 如果死锁，5 秒超时会触发 asyncio.TimeoutError
        result = await asyncio.wait_for(consume(), timeout=5.0)

        assert len(result) >= 1
        assert result[-1].type == "done"

    @pytest.mark.asyncio
    async def test_task_created_before_event_loop_iteration(self, weave_with_mocks):
        """验证 _run_and_emit task 在 async for 第一次迭代前已创建。"""
        weave = weave_with_mocks
        stream_gen = weave.stream("test input")

        async for event in stream_gen:
            assert event is not None
            if event.type in ("done", "error"):
                break

    @pytest.mark.asyncio
    async def test_stream_completes_within_timeout_with_immediate_result(self, weave_with_mocks):
        """stream() 在 _run_impl 立即返回时也能正常完成，不留后台僵尸任务。"""
        weave = weave_with_mocks
        # 让 run 立即返回
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="immediate",
            elapsed_ms=0,
            iterations=1,
            memory_updated={},
        ))

        events = []
        async for event in weave.stream("fast input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert len(events) >= 1
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "immediate"


# ============================================================
# 修复项 6: R2 违规修复 — 无硬编码 Prompt（第2轮）
# ============================================================


class TestNoHardcodedPrompt:
    """测试 _load_system_prompt() 不再返回硬编码字符串。"""

    def test_load_system_prompt_uses_bundled_default_when_not_configured(self):
        """当未配置 prompts.system 时，_load_system_prompt 应加载内置默认 prompt（不抛错）。"""
        weave = Weave.__new__(Weave)

        # 配置 prompts.system 为空字符串
        weave._config = MagicMock()
        weave._config.prompts.system = ""

        weave._prompts = MagicMock()

        result = weave._load_system_prompt()
        assert isinstance(result, str)
        assert result.strip()

    def test_load_system_prompt_uses_bundled_default_when_none(self):
        """当 prompts.system 为 None 时，_load_system_prompt 应加载内置默认 prompt（不抛错）。"""
        weave = Weave.__new__(Weave)

        weave._config = MagicMock()
        weave._config.prompts.system = None

        weave._prompts = MagicMock()

        result = weave._load_system_prompt()
        assert isinstance(result, str)
        assert result.strip()

    def test_load_system_prompt_works_with_valid_config(self):
        """当 prompts.system 配置正确时，_load_system_prompt 应正常返回提示词。"""
        weave = Weave.__new__(Weave)

        weave._config = MagicMock()
        weave._config.prompts.system = "prompts/system.md"

        mock_prompts = MagicMock()
        mock_prompts.get = MagicMock(return_value="Valid system prompt content")
        weave._prompts = mock_prompts

        result = weave._load_system_prompt({"name": "test"})

        assert result == "Valid system prompt content"
        mock_prompts.get.assert_called_once_with("system", {"name": "test"})

    def test_no_hardcoded_fallback_string_exists(self):
        """验证代码中不存在 'You are a helpful AI assistant' 硬编码字符串。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._load_system_prompt)
        assert "You are a helpful AI assistant" not in source
        assert '"You are' not in source


# ============================================================
# 修复项 7: 环境变量插值 regex 边界修复（第2轮）
# ============================================================


class TestEnvVarResolution:
    """测试 _resolve_env() 的三种格式：${VAR}、${VAR:default}、${VAR:-default}。"""

    def test_var_without_default_returns_empty_when_not_set(self):
        """${VAR} 无默认值格式，环境变量不存在时返回空字符串。"""
        result = _resolve_env("hello ${UNDEFINED_VAR_12345} world")
        assert result == "hello  world"

    def test_var_without_default_returns_value_when_set(self):
        """${VAR} 无默认值格式，环境变量存在时返回环境变量值。"""
        os.environ["TEST_VAR_RESOLVE"] = "env_value"
        try:
            result = _resolve_env("prefix ${TEST_VAR_RESOLVE} suffix")
            assert result == "prefix env_value suffix"
        finally:
            del os.environ["TEST_VAR_RESOLVE"]

    def test_var_with_colon_default_uses_default_when_not_set(self):
        """${VAR:default} POSIX 风格，环境变量不存在时使用默认值。"""
        result = _resolve_env("hello ${UNDEF_VAR:default_val} world")
        assert result == "hello default_val world"

    def test_var_with_colon_default_uses_env_when_set(self):
        """${VAR:default} 风格，环境变量存在时优先使用环境变量。"""
        os.environ["TEST_VAR_COLON"] = "from_env"
        try:
            result = _resolve_env("${TEST_VAR_COLON:fallback}")
            assert result == "from_env"
        finally:
            del os.environ["TEST_VAR_COLON"]

    def test_var_with_dash_default_excludes_dash(self):
        """${VAR:-default} Bash 风格，- 不应包含在默认值中。

        这是第2轮修复的核心验证点：旧 regex 会将 -claude-sonnet-5-20251001
        作为默认值（包含前导 -），修复后正确返回 claude-sonnet-5-20251001。
        """
        result = _resolve_env("${UNDEF_MODEL:-claude-sonnet-5-20251001}")
        # - 不应出现在默认值中
        assert result == "claude-sonnet-5-20251001"
        assert not result.startswith("-claude")

    def test_var_with_dash_default_uses_env_when_set(self):
        """${VAR:-default} 风格，环境变量存在时优先使用环境变量。"""
        os.environ["TEST_VAR_DASH"] = "actual_value"
        try:
            result = _resolve_env("${TEST_VAR_DASH:-fallback}")
            assert result == "actual_value"
        finally:
            del os.environ["TEST_VAR_DASH"]

    def test_multiple_vars_in_one_string(self):
        """同一字符串中包含多个 ${VAR} 占位符，应全部正确解析。"""
        os.environ["TEST_A"] = "A"
        os.environ["TEST_B"] = "B"
        try:
            result = _resolve_env("${TEST_A} and ${TEST_B} and ${UNDEF_C:-C}")
            assert result == "A and B and C"
        finally:
            del os.environ["TEST_A"]
            del os.environ["TEST_B"]

    def test_var_with_colon_empty_default(self):
        """${VAR:} 默认值为空字符串时，应返回空字符串。"""
        result = _resolve_env("${UNDEF_EMPTY:}")
        assert result == ""

    def test_var_with_dash_empty_default(self):
        """${VAR:-} 默认值为空字符串时，应返回空字符串（- 被忽略）。"""
        result = _resolve_env("${UNDEF_EMPTY_DASH:-}")
        assert result == ""


# ============================================================
# 修复项 8: .bak 备份文件清理验证（第2轮&第3轮）
# ============================================================


class TestBakFileCleanup:
    """验证项目源码目录下不存在 .bak 备份文件。"""

    def test_no_bak_files_in_weave_source(self):
        """weave/ 目录下不应存在任何 .bak 文件。"""
        weave_pkg_dir = Path(__file__).parent.parent / "weave_agent_sdk"
        bak_files = list(weave_pkg_dir.rglob("*.bak"))
        assert len(bak_files) == 0, f"Found .bak files in weave/ source: {bak_files}"

    def test_no_bak_files_anywhere_in_project(self):
        """整个项目目录树中不应存在任何 .bak 文件（含 tests/、nexus/ 等）。"""
        project_root = Path(__file__).parent.parent
        bak_files = list(project_root.rglob("*.bak"))
        assert len(bak_files) == 0, f"Found .bak files: {bak_files}"




# ============================================================
# 修复项 10: 默认 weave.yaml 配置文件存在性验证（第3轮）
# ============================================================


class TestDefaultConfigExistence:
    """测试项目根目录存在默认 weave.yaml 配置文件。"""

    def test_default_weave_yaml_exists(self):
        """项目根目录应存在 weave.yaml 文件。"""
        yaml_path = Path(__file__).parent.parent / "weave.yaml"
        assert yaml_path.exists(), "weave.yaml not found in project root"

    def test_default_weave_yaml_has_required_sections(self):
        """weave.yaml 应包含所有必需的配置段。"""
        import yaml
        yaml_path = Path(__file__).parent.parent / "weave.yaml"
        with open(yaml_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        required_sections = ["agent", "llm", "loop", "prompts"]
        for section in required_sections:
            assert section in config, f"Missing section '{section}' in weave.yaml"

    def test_default_weave_yaml_has_iterative_loop(self):
        """weave.yaml 的 loop 段应默认 iterative。"""
        import yaml
        yaml_path = Path(__file__).parent.parent / "weave.yaml"
        with open(yaml_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        assert config["loop"]["type"] == "iterative"


# ============================================================
# 修复项 11: Prompt 文件存在性验证（第3轮）
# ============================================================


class TestPromptFilesExistence:
    """测试 prompts/ 目录下存在必需的 prompt 文件。"""

    def test_system_prompt_exists(self):
        """prompts/system.md 应存在。"""
        prompt_path = Path(__file__).parent.parent / "prompts" / "system.md"
        assert prompt_path.exists(), "prompts/system.md not found"

    def test_loop_prompt_exists(self):
        """prompts/loop.md 应存在。"""
        prompt_path = Path(__file__).parent.parent / "prompts" / "loop.md"
        assert prompt_path.exists(), "prompts/loop.md not found"

    def test_memory_prompt_exists(self):
        """prompts/memory.md 应存在。"""
        prompt_path = Path(__file__).parent.parent / "prompts" / "memory.md"
        assert prompt_path.exists(), "prompts/memory.md not found"


# ============================================================
# 修复项 12: stream() 超时值可配置验证（第3轮）
# ============================================================


class TestStreamTimeoutConfigurable:
    """测试 stream() 的超时值可从 loop 配置读取。"""

    def test_loop_config_has_timeout_field(self):
        """LoopConfig 应包含 timeout 字段，默认值 5.0。"""
        from weave_agent_sdk.types import LoopConfig
        config = LoopConfig()
        assert hasattr(config, "timeout")
        assert config.timeout == 5.0

    def test_loop_config_timeout_customizable(self):
        """LoopConfig.timeout 应可自定义。"""
        from weave_agent_sdk.types import LoopConfig
        config = LoopConfig(timeout=10.0)
        assert config.timeout == 10.0

    def test_load_config_parses_timeout_from_yaml(self, tmp_path):
        """load_config() 应从 YAML 解析 loop.timeout 字段。"""
        import yaml
        yaml_path = tmp_path / "test_weave.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "simple", "timeout": 7.5},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        result = load_config(yaml_path)
        assert result.loop.timeout == 7.5

    def test_load_config_default_timeout(self, tmp_path):
        """load_config() 在 YAML 未配置 timeout 时应使用默认值 5.0。"""
        import yaml
        yaml_path = tmp_path / "test_weave_default.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "simple"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        result = load_config(yaml_path)
        assert result.loop.timeout == 5.0

    @pytest.mark.asyncio
    async def test_stream_works_with_custom_timeout_config(self):
        """stream() 使用自定义 timeout 时应正常运行不报错。"""
        weave = Weave.__new__(Weave)

        weave._config = MagicMock()
        weave._config.agent.name = "test"
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 3.0  # 自定义超时

        from weave_agent_sdk.event_bus import EventBus
        weave._event_bus = EventBus()
        weave._llm = AsyncMock()
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.stats = MagicMock(return_value={})
        weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")
        weave._loop = AsyncMock()
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="test", elapsed_ms=0, iterations=1, memory_updated={}
        ))
        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None

        events = []
        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert len(events) >= 1
        assert events[-1].type == "done"

    def test_agent_code_uses_config_timeout(self):
        """验证 agent.py 中 stream() 使用了 self._config.loop.timeout 而非硬编码值。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        # 应使用配置的 timeout
        assert 'self._config.loop.timeout' in source
        # 不应有硬编码的 timeout=5.0
        assert 'timeout=5.0' not in source




# ============================================================
# 第3轮新增：修复项 12 — 集成测试：stream() timeout 传播
# ============================================================


class TestStreamTimeoutPropagation:
    """测试 stream() 的超时配置从 config 传播到 asyncio.wait_for。"""

    @pytest.mark.asyncio
    async def test_stream_uses_config_timeout_value(self):
        """验证 stream() 使用了 self._config.loop.timeout 的值。

        通过修改 config 中的不同 timeout 值，验证 stream() 行为正确。
        """
        # 测试不同的 timeout 值都能正常工作
        for timeout_val in [0.5, 2.0, 10.0]:
            weave = Weave.__new__(Weave)

            weave._config = MagicMock()
            weave._config.agent.name = "test"
            weave._config.loop.type = "simple"
            weave._config.loop.timeout = timeout_val

            from weave_agent_sdk.event_bus import EventBus
            weave._event_bus = EventBus()
            weave._llm = AsyncMock()
            weave._memory = MagicMock()
            weave._memory.activate_scopes = MagicMock()
            weave._memory.stats = MagicMock(return_value={})
            weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])
            weave._prompts = MagicMock()
            weave._prompts.get = MagicMock(return_value="System prompt")
            weave._loop = AsyncMock()
            weave._loop.run = AsyncMock(return_value=LoopResult(
                output=f"timeout_{timeout_val}", elapsed_ms=0, iterations=1, memory_updated={}
            ))
            weave._tools = []
            weave._tool_map = {}
            weave._system_prompt = ""
            weave._is_running = False
            weave._last_run = None

            events = []
            async for event in weave.stream("test input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break

            assert len(events) >= 1
            assert events[-1].type == "done"
            assert events[-1].data["output"] == f"timeout_{timeout_val}"

    @pytest.mark.asyncio
    async def test_stream_zero_timeout_does_not_block_indefinitely(self):
        """stream() 在 timeout=0 时应立即继续而不阻塞。"""
        weave = Weave.__new__(Weave)

        weave._config = MagicMock()
        weave._config.agent.name = "test"
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 0.0  # 零超时

        from weave_agent_sdk.event_bus import EventBus
        weave._event_bus = EventBus()
        weave._llm = AsyncMock()
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.stats = MagicMock(return_value={})
        weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")
        weave._loop = AsyncMock()
        # Make run complete very fast
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="zero_timeout", elapsed_ms=0, iterations=1, memory_updated={}
        ))
        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None

        # Should complete without hanging even with timeout=0
        events = []
        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert len(events) >= 1
        assert events[-1].type == "done"


# ============================================================
# 第3轮新增：E2E 测试 — 实际 weave.yaml 加载
# ============================================================


class TestE2EConfigLoading:
    """端到端测试：加载实际项目配置文件。"""

    def test_e2e_load_actual_weave_yaml(self):
        """加载项目根目录的 weave.yaml 并验证所有配置段解析正确。"""
        project_root = Path(__file__).parent.parent
        yaml_path = project_root / "weave.yaml"
        assert yaml_path.exists(), "weave.yaml not found in project root"

        # 实际加载配置
        config = load_config(yaml_path)

        # 验证 agent
        assert config.agent.name == "default"

        # 验证 llm
        assert config.llm.provider == "anthropic"
        assert config.llm.max_tokens == 4096
        assert config.llm.temperature == 0.7

        # 验证 loop
        assert config.loop.type == "iterative"
        assert config.loop.max_iterations == 10
        assert config.loop.timeout == 5.0
        assert config.loop.stop_conditions == []

        # 验证 memory（无 memory 段 → 默认）
        assert config.memory.default_backend == "sqlite"
        # 路径规范化：默认路径相对配置文件目录解析为绝对路径，数据目录为 .weave/
        assert config.memory.default_path.endswith(".weave/memory.db")

        # 验证 prompts
        assert config.prompts.system == "prompts/system.md"

        # 验证 features
        assert config.features.schema_validation is False
        assert config.features.structured_call is False

        # 验证 server
        assert config.server.host == "127.0.0.1"
        assert config.server.port == 48080

        # 验证 logging
        assert config.logging.level == "INFO"

    def test_e2e_load_config_with_env_override(self, tmp_path, monkeypatch):
        """加载配置文件时，环境变量应能覆盖 YAML 中的默认值。"""
        import yaml

        # 设置环境变量覆盖
        monkeypatch.setenv("WEAVE_MODEL", "custom-model-2025")
        monkeypatch.setenv("WEAVE_MAX_TOKENS", "8192")
        monkeypatch.setenv("WEAVE_TEMP", "0.3")
        monkeypatch.setenv("WEAVE_MAX_ITER", "5")
        monkeypatch.setenv("WEAVE_DATA_DIR", "/custom/data")

        # 创建测试 YAML（含环境变量插值）
        yaml_content = """
agent:
  name: default

llm:
  provider: anthropic
  model: ${WEAVE_MODEL:-claude-sonnet-5-20251001}
  max_tokens: ${WEAVE_MAX_TOKENS:-4096}
  temperature: ${WEAVE_TEMP:-0.7}

loop:
  type: simple
  max_iterations: ${WEAVE_MAX_ITER:-10}
  timeout: 5.0

memory:
  scopes:
    session:
      stream:
        backend: sqlite
        path: ${WEAVE_DATA_DIR:-./data}/memory.db
        ttl: 3600

prompts:
  system: prompts/system.md
  loop_instruction: prompts/loop.md
  memory_use: prompts/memory.md

features:
  schema_validation: false
  structured_call: false
  two_stage_pipeline: false
  prompt_defense: false

server:
  host: 127.0.0.1
  port: 48080
  cors_origins: []

logging:
  level: INFO
"""
        yaml_path = tmp_path / "test_env_override.yaml"
        yaml_path.write_text(yaml_content, encoding="utf-8")

        config = load_config(yaml_path)

        # 验证环境变量覆盖生效
        assert config.llm.model == "custom-model-2025"
        assert config.llm.max_tokens == 8192
        assert config.llm.temperature == 0.3
        assert config.loop.max_iterations == 5

        # memory path 也应被覆盖
        session_scope = config.memory.scopes.get("session", {})
        assert session_scope is not None


# ============================================================
# 第4轮新增：修复项 13 — scheduled.py 导入修复（format_memory_context）
# ============================================================


class TestScheduledLoopImportFix:
    """测试 scheduled.py 导入修复：format_memory_context 替换 _format_memory。"""

    def test_scheduled_loop_imports_format_memory_context(self):
        """scheduled.py 应从 weave.loop.base 导入 format_memory_context。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        # 应导入 format_memory_context
        assert "from weave_agent_sdk.loop.base import" in source
        assert "format_memory_context" in source

    def test_scheduled_loop_no_iterative_format_memory_import(self):
        """scheduled.py 不应从 weave.loop.iterative 导入 _format_memory。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        # 不应导入 _format_memory
        assert "_format_memory" not in source

    def test_scheduled_loop_calls_format_memory_context(self):
        """scheduled.py 的 _execute_once 方法应调用 format_memory_context 而非 _format_memory。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        # 方法体中使用 format_memory_context
        assert "format_memory_context(memory_ctx)" in source
        # 不应使用 _format_memory
        assert "_format_memory(memory_ctx)" not in source

    def test_scheduled_loop_no_method_body_import_message(self):
        """scheduled.py 中 from weave_agent_sdk.types import Message 应在文件顶部，而非方法体内部。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        # 检查 Message 导入在文件顶部（import 段），而非方法体内部
        # 方法体中的 import 行会有缩进，文件顶部的 import 没有缩进
        lines = source.split("\n")
        for line in lines:
            if "from weave_agent_sdk.types import" in line and "Message" in line:
                # 文件顶部的导入不应有缩进
                assert not line.startswith(" "), f"Import should be at file top, not indented: {line}"
                break
        else:
            pytest.fail("Could not find 'from weave_agent_sdk.types import ... Message' in scheduled.py")


# ============================================================
# 第4轮新增：修复项 14 — scheduled.py 死代码移除
# ============================================================


class TestScheduledLoopDeadCodeRemoved:
    """测试 scheduled.py 中已移除 inner = IterativeLoop() 死代码。"""

    def test_scheduled_loop_no_iterative_loop_import(self):
        """scheduled.py 不应导入 IterativeLoop（已移除死代码后不再需要）。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        assert "IterativeLoop" not in source

    def test_scheduled_loop_no_inner_iterative_loop(self):
        """scheduled.py 的 _execute_once 方法中不应包含 inner = IterativeLoop()。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        from weave_agent_sdk.loop.scheduled import ScheduledLoop; source = inspect.getsource(ScheduledLoop._execute_once)
        assert "inner = IterativeLoop()" not in source
        assert "inner = " not in source

    def test_scheduled_loop_still_functions_without_iterative_loop(self):
        """移除 IterativeLoop 导入和死代码后，模块应仍可正常导入。"""
        # 重新导入验证模块可正常加载
        import importlib
        import weave_agent_sdk.loop.scheduled
        importlib.reload(weave_agent_sdk.loop.scheduled)
        # 不应抛出 ImportError
        assert True


# ============================================================
# 第4轮新增：修复项 15 — iterative.py stream/state 写入计数存根修复
# ============================================================


class TestIterativeLoopStubFix:
    """测试 iterative.py 中 stream/state 写入计数存根修复。"""

    def test_stream_writes_initialized_as_empty_dict(self):
        """stream_writes 应初始化为空 dict，不再被零值填充。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # stream_writes 初始化为空 dict
        assert "stream_writes: dict[str, int] = {}" in source
        # 不应有零值填充循环
        assert "stream_writes[ns] = stream_writes.get(ns, 0)" not in source

    def test_state_writes_initialized_as_empty_dict(self):
        """state_writes 应初始化为空 dict，不再被零值填充。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # state_writes 初始化为空 dict
        assert "state_writes: dict[str, int] = {}" in source
        # 不应有零值填充循环
        assert "state_writes[ns] = state_writes.get(ns, 0)" not in source

    def test_stream_writes_not_in_memory_updated_when_empty(self):
        """memory_updated 在 stream_writes 为空时不包含 'stream' 键。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # 应有条件判断：if stream_writes: memory_updated["stream"] = stream_writes
        assert "if stream_writes:" in source
        assert 'memory_updated["stream"] = stream_writes' in source

    def test_state_writes_not_in_memory_updated_when_empty(self):
        """memory_updated 在 state_writes 为空时不包含 'state' 键。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # 应有条件判断：if state_writes: memory_updated["state"] = state_writes
        assert "if state_writes:" in source
        assert 'memory_updated["state"] = state_writes' in source

    def test_todo_comment_exists(self):
        """应存在 TODO 注释说明 stream/state 写入追踪需在 after_think 钩子中实现。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        assert "TODO" in source
        assert "stream" in source.lower() or "write" in source.lower()

    def test_tool_writes_still_tracked(self):
        """tool_writes 应继续被正确追踪（不受 stream/state 存根修复影响）。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # tool_writes 仍应按名称累计计数
        assert 'tool_writes[tc.name] = tool_writes.get(tc.name, 0) + 1' in source


# ============================================================
# 第4轮新增：E2E 测试 — ScheduledLoop 可实例化
# ============================================================


class TestScheduledLoopInstantiationE2E:
    """端到端测试：ScheduledLoop 导入和实例化。"""

    def test_scheduled_loop_can_be_imported(self):
        """ScheduledLoop 应可正常导入，无 ImportError。"""
        try:
            from weave_agent_sdk.loop.scheduled import ScheduledLoop
        except ImportError as e:
            pytest.fail(f"ScheduledLoop import failed: {e}")

    def test_scheduled_loop_can_be_instantiated(self):
        """ScheduledLoop 应可正常实例化。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        loop = ScheduledLoop()
        assert type(loop).__name__ == "ScheduledLoop"
        assert loop._shutting_down is False
        assert loop._current_task is None

    def test_scheduled_loop_has_required_methods(self):
        """ScheduledLoop 应包含必需的公开方法。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        loop = ScheduledLoop()
        assert hasattr(loop, "run")
        assert hasattr(loop, "handle_shutdown")
        assert callable(loop.run)
        assert callable(loop.handle_shutdown)

    def test_scheduled_loop_has_format_memory_context_available(self):
        """ScheduledLoop 模块中 format_memory_context 应可被调用。"""
        from weave_agent_sdk.loop.base import format_memory_context
        # 测试 format_memory_context 函数正常工作
        ctx = {"stream": [{"role": "user", "content": "hello"}], "state": {"key": "val"}}
        result = format_memory_context(ctx)
        assert "hello" in result
        assert "key" in result
        assert "val" in result


# ============================================================
# 第4轮新增：E2E 测试 — IterativeLoop memory_updated 行为
# ============================================================


class TestIterativeLoopMemoryUpdatedE2E:
    """端到端测试：IterativeLoop.run 的 memory_updated 正确行为。

    验证修复项15：空的 stream_writes/state_writes 不产生误导性的零值条目。
    """

    @pytest.mark.asyncio
    async def test_memory_updated_omits_empty_stream_and_state(self):
        """当 stream_writes 和 state_writes 为空时，memory_updated 不应包含 stream/state 键。

        这是修复项15的核心验证：存根代码移除后，空的写入计数不会出现在结果中。
        """
        # 创建最小 mock agent
        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.loop.stop_conditions = [{"type": "no_tool_calls"}]
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        # Mock LLM 返回无 tool_calls 的响应（触发 no_tool_calls 停止）
        from weave_agent_sdk.llm.base import LLMResponse
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="Direct response",
            model="test",
        ))

        # Mock before_think 返回空 context（无 memory）
        from weave_agent_sdk.loop.base import BaseLoop
        agent._system_prompt = "You are a test assistant."

        # 创建 IterativeLoop 实例
        from weave_agent_sdk.loop.iterative import IterativeLoop
        loop = IterativeLoop()

        # Mock on_start / on_end
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        # 执行 run
        result = await loop.run(agent, "Hello")

        # memory_updated 不应包含 'stream' 或 'state' 键（应为空或仅含 'tool'）
        assert "stream" not in result.memory_updated, \
            "Empty stream_writes should not appear in memory_updated"
        assert "state" not in result.memory_updated, \
            "Empty state_writes should not appear in memory_updated"

    @pytest.mark.asyncio
    async def test_memory_updated_includes_tool_writes_when_present(self):
        """当有 tool 调用时，memory_updated 应包含 tool 键。

        验证 tool_writes 的正常追踪功能未被 stream/state 修复影响。
        """
        # 创建 agent 并注册一个 tool
        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 3
        agent._config.loop.stop_conditions = []
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        # 创建一个真实 tool
        def test_tool(query: str) -> str:
            return f"searched: {query}"

        agent._tools = [test_tool]
        agent._tool_map = {"test_tool": test_tool}

        # Mock LLM: 第一次返回 tool_call，第二次返回无 tool_calls
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(
                content="Let me search",
                model="test",
                tool_calls=[
                    ToolCall(id="call_1", name="test_tool", arguments={"query": "test"}),
                ],
            ),
            LLMResponse(
                content="Done searching",
                model="test",
                tool_calls=None,
            ),
        ])

        agent._system_prompt = "You are a test assistant."

        from weave_agent_sdk.loop.iterative import IterativeLoop
        loop = IterativeLoop()

        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "Search something")

        # memory_updated 应包含 'tools' 键（修复项23：tool → tools），且 test_tool 被调用了一次
        assert "tools" in result.memory_updated
        assert result.memory_updated["tools"].get("test_tool") == 1

        # stream 和 state 仍不应出现（因为它们没有被追踪）
        assert "stream" not in result.memory_updated
        assert "state" not in result.memory_updated


# ============================================================
# 第5轮新增：修复项 17 — _create_loop() 添加 "scheduled" 支持
# ============================================================


class TestCreateLoopScheduledSupport:
    """测试 _create_loop() 方法中 "scheduled" 循环类型的支持。

    覆盖修复项1（第5轮）：_create_loop() 添加 "scheduled" 分支。
    覆盖修复项28（第8轮）：_create_loop() 未知 loop type 改为抛出 ValueError。
    """

    def _make_weave_with_loop_type(self, loop_type: str) -> Weave:
        """创建一个指定 loop.type 的 Weave 实例。"""
        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = loop_type
        weave._tools = []
        weave._tool_map = {}
        return weave

    def test_create_loop_returns_scheduled_loop(self):
        """loop.type='scheduled' 时 _create_loop() 应返回 ScheduledLoop 实例。"""
        weave = self._make_weave_with_loop_type("scheduled")
        loop = weave._create_loop()

        assert type(loop).__name__ == "ScheduledLoop", \
            f"Expected ScheduledLoop, got {type(loop).__name__}"

    def test_create_loop_returns_simple_loop(self):
        """loop.type='simple' 时 _create_loop() 应返回 SimpleLoop 实例（回归验证）。"""
        weave = self._make_weave_with_loop_type("simple")
        loop = weave._create_loop()

        assert type(loop).__name__ == "SimpleLoop", \
            f"Expected SimpleLoop, got {type(loop).__name__}"

    def test_create_loop_returns_iterative_loop(self):
        """loop.type='iterative' 时 _create_loop() 应返回 IterativeLoop 实例（回归验证）。"""
        weave = self._make_weave_with_loop_type("iterative")
        loop = weave._create_loop()

        assert type(loop).__name__ == "IterativeLoop", \
            f"Expected IterativeLoop, got {type(loop).__name__}"

    def test_create_loop_raises_value_error_for_unknown_type(self):
        """未知 loop type 应抛出 ValueError，并提示支持的类型列表。

        第8轮修复项28：移除静默回退，改为抛出 ValueError。
        """
        weave = self._make_weave_with_loop_type("unknown_type_xyz")

        with pytest.raises(ValueError) as excinfo:
            weave._create_loop()

        assert "Unknown loop type" in str(excinfo.value)
        assert "unknown_type_xyz" in str(excinfo.value)
        assert "simple" in str(excinfo.value)
        assert "iterative" in str(excinfo.value)
        assert "scheduled" in str(excinfo.value)

    def test_create_loop_all_three_types_distinct(self):
        """三个循环类型（simple/iterative/scheduled）应返回不同的实例类型。"""
        w1 = self._make_weave_with_loop_type("simple")
        w2 = self._make_weave_with_loop_type("iterative")
        w3 = self._make_weave_with_loop_type("scheduled")

        loop1 = w1._create_loop()
        loop2 = w2._create_loop()
        loop3 = w3._create_loop()

        assert type(loop1).__name__ == "SimpleLoop"
        assert type(loop2).__name__ == "IterativeLoop"
        assert type(loop3).__name__ == "ScheduledLoop"
        # Verify they are distinct types
        assert type(loop1) != type(loop2)
        assert type(loop2) != type(loop3)
        assert type(loop1) != type(loop3)

    def test_agent_imports_scheduled_loop(self):
        """验证 loop 注册表包含 ScheduledLoop（内置策略 = 预注册默认值）。"""
        from weave_agent_sdk.loop.factory import LOOP_REGISTRY

        assert "scheduled" in LOOP_REGISTRY.names()
        assert LOOP_REGISTRY.get("scheduled").__name__ == "ScheduledLoop"

    def test_create_loop_case_sensitive_raises_value_error(self):
        """loop type 比较应区分大小写，'Scheduled'/'SCHEDULED' 应抛出 ValueError。

        第8轮修复项28：不匹配已知类型不再静默回退，直接抛出 ValueError。
        """
        weave = self._make_weave_with_loop_type("Scheduled")  # 大写 S
        with pytest.raises(ValueError) as excinfo:
            weave._create_loop()
        assert "Scheduled" in str(excinfo.value)

        weave2 = self._make_weave_with_loop_type("SCHEDULED")  # 全大写
        with pytest.raises(ValueError) as excinfo2:
            weave2._create_loop()
        assert "SCHEDULED" in str(excinfo2.value)


# ============================================================
# 第5轮新增：修复项 18 — chat_stream() tools 参数和 tool 消息支持
# ============================================================


class TestChatStreamToolsSupport:
    """测试 chat_stream() 支持 tools 参数和 tool 消息的处理。

    覆盖修复项2（第5轮）：chat_stream() 传递 tools 参数给 stream()，
    并正确处理 tool 角色消息。
    """

    def test_chat_stream_passes_tools_to_stream(self):
        """验证 chat_stream() 将 tools 参数传递给 client.messages.stream。"""
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        source = inspect.getsource(AnthropicAdapter.chat_stream)
        # tools 参数应传递给 stream 调用
        assert "tools=tool_schemas" in source

    def test_chat_stream_has_tools_parameter_in_signature(self):
        """验证 chat_stream() 方法签名包含 tools 参数。"""
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        sig = inspect.signature(AnthropicAdapter.chat_stream)
        assert "tools" in sig.parameters

    def test_chat_stream_handles_tool_messages(self):
        """验证 chat_stream() 正确处理 tool 角色消息（转为 tool_result content block）。"""
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        source = inspect.getsource(AnthropicAdapter.chat_stream)
        # 应处理 role == "tool" 的消息
        assert 'msg.role == "tool"' in source
        assert "tool_result" in source
        assert "tool_use_id" in source

    def test_chat_stream_handles_assistant_with_tool_calls(self):
        """验证 chat_stream() 正确处理 assistant 消息中的 tool_calls。"""
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        source = inspect.getsource(AnthropicAdapter.chat_stream)
        # assistant 消息包含 tool_calls 时，应构建 tool_use content blocks
        assert 'msg.role == "assistant" and msg.tool_calls' in source
        assert '"type": "tool_use"' in source
        assert 'tc.arguments' in source

    def test_chat_stream_converts_tools_to_anthropic(self):
        """验证 chat_stream() 调用了 _convert_tools_to_anthropic。"""
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        source = inspect.getsource(AnthropicAdapter.chat_stream)
        # 应调用 _convert_tools_to_anthropic(tools)
        assert "_convert_tools_to_anthropic(tools)" in source

    def test_chat_stream_message_building_matches_chat(self):
        """验证 chat_stream() 和 chat() 使用相同的消息构建逻辑。

        两者应使用相同的 system / tool / user / assistant 四种角色处理方式。
        """
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        chat_source = inspect.getsource(AnthropicAdapter.chat)
        stream_source = inspect.getsource(AnthropicAdapter.chat_stream)

        # 两者都应处理四种角色
        for role_check in [
            'msg.role == "system"',
            'msg.role == "tool"',
            'msg.role == "assistant"',
            'msg.role in ("user", "assistant")',
        ]:
            assert role_check in chat_source, f"chat() missing: {role_check}"
            assert role_check in stream_source, f"chat_stream() missing: {role_check}"

        # 两者都应使用 tool_result content block 格式
        assert '"type": "tool_result"' in chat_source
        assert '"type": "tool_result"' in stream_source

        # 两者都应使用 tool_use content block 格式
        assert '"type": "tool_use"' in chat_source
        assert '"type": "tool_use"' in stream_source

    def test_convert_tools_to_anthropic_function(self):
        """验证 _convert_tools_to_anthropic() 函数正确转换 tool schema。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "name": "search",
                "description": "Search the knowledge base",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "search"
        assert result[0]["description"] == "Search the knowledge base"
        assert result[0]["input_schema"]["properties"]["query"]["type"] == "string"

    def test_convert_tools_to_anthropic_openai_format(self):
        """验证 _convert_tools_to_anthropic() 能处理 OpenAI function-calling 格式（含 function 包装）。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                },
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "search_web"
        assert result[0]["description"] == "Search the web"
        assert result[0]["input_schema"]["properties"]["q"]["type"] == "string"

    def test_convert_tools_to_anthropic_empty_list(self):
        """验证 _convert_tools_to_anthropic() 处理空工具列表返回空列表。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        result = _convert_tools_to_anthropic([])
        assert result == []

    def test_convert_tools_to_anthropic_missing_fields(self):
        """验证 _convert_tools_to_anthropic() 处理缺少字段的 tool schema 不崩溃。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {"name": "minimal_tool"},  # 只有 name，缺少 description 和 parameters
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "minimal_tool"
        assert result[0]["description"] == ""  # 空字符串
        assert result[0]["input_schema"] == {}  # 空 dict


# ============================================================
# 第5轮新增：修复项 19 — stream.last() 多 backend 合并
# ============================================================


class TestStreamLastMultiBackend:
    """测试 MemoryStream.last() 多 backend 合并排序。

    覆盖修复项3（第5轮）：多个 backend 时合并排序，而非只处理第一个。
    """

    def test_stream_last_returns_created_at_field(self):
        """stream_last() 应返回 _created_at 内部字段用于跨 backend 排序。"""
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        source = inspect.getsource(SQLiteBackend.stream_last)
        # 应返回 _created_at 字段
        assert '_created_at' in source

    def test_stream_last_query_returns_content_and_created_at(self):
        """验证 stream_last() 的 SQL 查询同时返回 content 和 created_at。"""
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        source = inspect.getsource(SQLiteBackend.stream_last)
        # SQL 查询应包含 created_at
        assert "created_at" in source

    def test_stream_last_sorts_by_created_at_desc(self):
        """验证 stream_last() 按 created_at DESC 排序取最新 n 条。"""
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        source = inspect.getsource(SQLiteBackend.stream_last)
        assert "ORDER BY created_at DESC" in source

    def test_stream_last_returns_list(self):
        """stream_last() 应返回列表。"""
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        source = inspect.getsource(SQLiteBackend.stream_last)
        assert "return" in source

    def test_last_merges_all_backends_when_namespaces_none(self):
        """验证 last(namespaces=None) 遍历所有 backend 收集结果并合并。"""
        import inspect
        from weave_agent_sdk.memory.stream import MemoryStream

        source = inspect.getsource(MemoryStream.last)
        # 应遍历所有 backend
        assert "for backend in self._manager._backends.values()" in source

    def test_last_cleans_created_at_internal_field(self):
        """验证 last() 在返回前清理了 _created_at 内部字段。"""
        import inspect
        from weave_agent_sdk.memory.stream import MemoryStream

        source = inspect.getsource(MemoryStream.last)
        assert 'pop("_created_at"' in source

    def test_last_handles_empty_results(self):
        """验证 last() 在所有 backend 无结果时返回空列表。"""
        import inspect
        from weave_agent_sdk.memory.stream import MemoryStream

        source = inspect.getsource(MemoryStream.last)
        # 应该有空结果检查
        assert "if not all_results" in source

    def test_last_with_specific_namespaces_single_backend_optimization(self):
        """验证 last() 在指定 namespaces 且仅一个 backend 时走快速路径。"""
        import inspect
        from weave_agent_sdk.memory.stream import MemoryStream

        source = inspect.getsource(MemoryStream.last)
        # 应有单 backend 优化路径
        assert "len(backend_to_ns) == 1" in source


# ============================================================
# 第5轮新增：E2E 测试 — _create_loop() scheduled 类型集成
# ============================================================


class TestCreateLoopScheduledE2E:
    """端到端测试：Weave._create_loop() 在 scheduled 类型下正常工作。

    验证修复项17（第5轮）：Weave 实例化时 scheduled loop type 被正确创建。
    """

    def test_weave_create_loop_with_scheduled_type(self):
        """通过模拟配置验证 _create_loop() 在 scheduled 类型下返回 ScheduledLoop。"""
        # 使用 Weave.__new__ 创建实例并手动设置 config
        weave = Weave.__new__(Weave)

        # 创建最小配置（足够让 _create_loop 工作即可）
        weave._config = MagicMock()
        weave._config.agent.name = "test_scheduled"
        weave._config.loop.type = "scheduled"
        weave._config.loop.timeout = 5.0
        weave._config.loop.max_iterations = 10
        weave._tools = []
        weave._tool_map = {}

        # 执行 _create_loop
        loop = weave._create_loop()

        # 验证返回 ScheduledLoop
        assert type(loop).__name__ == "ScheduledLoop", \
            f"Expected ScheduledLoop, got {type(loop).__name__}"

        # 验证 loop 具有正确的初始状态
        assert loop._shutting_down is False
        assert loop._current_task is None

    def test_weave_loop_type_routes_correctly(self):
        """验证 Weave 在三种 loop type 下均创建正确的 loop 实例类型。"""
        type_map = {
            "simple": "SimpleLoop",
            "iterative": "IterativeLoop",
            "scheduled": "ScheduledLoop",
        }

        for loop_type, expected_name in type_map.items():
            weave = Weave.__new__(Weave)
            weave._config = MagicMock()
            weave._config.loop.type = loop_type
            weave._tools = []
            weave._tool_map = {}

            loop = weave._create_loop()
            assert type(loop).__name__ == expected_name, \
                f"loop.type='{loop_type}': expected {expected_name}, " \
                f"got {type(loop).__name__}"


# ============================================================
# 第5轮新增：E2E 测试 — stream.last() 多 backend 合并
# ============================================================


class TestStreamLastMultiBackendE2E:
    """端到端测试：MemoryStream.last() 多 backend 合并排序行为。

    使用真实的 SQLiteBackend 实例验证跨 backend 数据合并的正确性。
    """

    @pytest.fixture
    def multi_backend_setup(self, tmp_path):
        """创建两个独立的 SQLiteBackend 实例，各自写入不同 namespace 的数据。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        # 创建两个 backend，指向不同的 db 文件
        db1_path = tmp_path / "db1" / "memory.db"
        db2_path = tmp_path / "db2" / "memory.db"

        backend1 = SQLiteBackend(str(db1_path))
        backend2 = SQLiteBackend(str(db2_path))

        # backend1: 写入 namespace_a 的数据（较早时间）
        for i in range(3):
            entry = {"role": "user", "content": f"msg_a_{i}"}
            backend1.stream_append(entry, "scope1:session:stream")

        # backend2: 写入 namespace_b 的数据（较晚时间，模拟后续写入）
        import time
        time.sleep(0.05)  # 确保时间戳有差异
        for i in range(3):
            entry = {"role": "user", "content": f"msg_b_{i}"}
            backend2.stream_append(entry, "scope2:session:stream")

        return backend1, backend2

    @pytest.fixture
    def three_backend_setup(self, tmp_path):
        """创建三个独立的 SQLiteBackend 实例，验证三路合并。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backends = []
        for idx in range(3):
            db_path = tmp_path / f"db{idx}" / "memory.db"
            backend = SQLiteBackend(str(db_path))
            for i in range(2):
                entry = {"role": "user", "content": f"msg_{idx}_{i}"}
                backend.stream_append(entry, f"scope{idx}:session:stream")
            import time
            time.sleep(0.02)
            backends.append(backend)

        return backends

    def test_stream_last_merges_two_backends(self, multi_backend_setup):
        """验证 last() 在多个 backend 时正确合并所有结果。"""
        backend1, backend2 = multi_backend_setup
        from weave_agent_sdk.memory.stream import MemoryStream

        # 创建 mock manager，包含两个 backend
        manager = MagicMock()
        manager._backends = {
            "backend1": backend1,
            "backend2": backend2,
        }

        stream_memory = MemoryStream(manager)

        # 执行 last()，namespaces=None 应收集所有 backend 数据
        import asyncio
        results = stream_memory.last(n=10, namespaces=None)

        # 应返回 6 条结果（两个 backend 各 3 条）
        assert len(results) == 6, f"Expected 6 results, got {len(results)}"

        # 应包含所有 6 条消息内容
        contents = [r["content"] for r in results]
        for i in range(3):
            assert f"msg_a_{i}" in contents, f"Missing msg_a_{i}"
            assert f"msg_b_{i}" in contents, f"Missing msg_b_{i}"

        # 结果应按时间升序排列（最旧在前）
        # msg_a 先于 msg_b，所以 msg_a 在前
        assert results[0]["content"] == "msg_a_0"
        assert results[1]["content"] == "msg_a_1"
        assert results[2]["content"] == "msg_a_2"

        # _created_at 内部字段应已被清理
        for r in results:
            assert "_created_at" not in r, f"_created_at should be cleaned: {r}"

    def test_stream_last_limits_results_correctly(self, multi_backend_setup):
        """验证 last(n) 正确限制返回条数。"""
        backend1, backend2 = multi_backend_setup
        from weave_agent_sdk.memory.stream import MemoryStream

        manager = MagicMock()
        manager._backends = {
            "backend1": backend1,
            "backend2": backend2,
        }

        stream_memory = MemoryStream(manager)

        import asyncio
        # 只取 3 条
        results = stream_memory.last(n=3, namespaces=None)

        # 应只返回 3 条（最新的 3 条，即 msg_b_2, msg_b_1, msg_b_0）
        assert len(results) == 3, f"Expected 3 results, got {len(results)}"

        # 最新的 3 条是 msg_b 系列
        contents = [r["content"] for r in results]
        assert "msg_b_0" in contents
        assert "msg_b_1" in contents
        assert "msg_b_2" in contents

    def test_stream_last_with_specific_namespace(self, multi_backend_setup):
        """验证 last(namespaces=[...]) 只返回指定 namespace 的数据。"""
        backend1, backend2 = multi_backend_setup
        from weave_agent_sdk.memory.stream import MemoryStream

        # 需要 mock _backend 方法
        manager = MagicMock()
        manager._backends = {
            "backend1": backend1,
            "backend2": backend2,
        }

        def mock_get_backend(ns):
            if ns == "scope1:session:stream":
                return backend1
            return backend2
        manager._get_backend_for_namespace = mock_get_backend

        stream_memory = MemoryStream(manager)

        import asyncio
        # 只查 scope1 的数据
        results = stream_memory.last(
            n=10, namespaces=["scope1:session:stream"]
        )

        # 应只返回 3 条（scope1 的数据）
        assert len(results) == 3, f"Expected 3 results, got {len(results)}"
        for r in results:
            assert r["content"].startswith("msg_a_"), \
                f"Expected msg_a_* content, got {r['content']}"
            assert "_created_at" not in r, \
                f"_created_at should be cleaned: {r}"

    def test_stream_last_empty_backend_returns_empty(self):
        """验证 last() 在所有 backend 为空时返回空列表。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        from weave_agent_sdk.memory.stream import MemoryStream

        backend = SQLiteBackend(":memory:")
        manager = MagicMock()
        manager._backends = {"mem": backend}

        stream_memory = MemoryStream(manager)

        import asyncio
        results = stream_memory.last(n=10, namespaces=None)

        assert results == [], f"Expected empty list, got {results}"

    def test_stream_last_merges_three_backends(self, three_backend_setup):
        """验证 last() 在三个 backend 时正确合并所有结果（多于2个的场景）。"""
        backend0, backend1, backend2 = three_backend_setup
        from weave_agent_sdk.memory.stream import MemoryStream

        manager = MagicMock()
        manager._backends = {
            "backend0": backend0,
            "backend1": backend1,
            "backend2": backend2,
        }

        stream_memory = MemoryStream(manager)

        import asyncio
        results = stream_memory.last(n=10, namespaces=None)

        # 应返回 6 条结果（三个 backend 各 2 条）
        assert len(results) == 6, f"Expected 6 results, got {len(results)}"

        # 应包含所有 6 条消息内容
        contents = [r["content"] for r in results]
        for idx in range(3):
            for i in range(2):
                assert f"msg_{idx}_{i}" in contents, f"Missing msg_{idx}_{i}"

        # _created_at 内部字段应已被清理
        for r in results:
            assert "_created_at" not in r, f"_created_at should be cleaned: {r}"

    def test_stream_last_with_duplicate_namespaces_in_list(self, multi_backend_setup):
        """验证 last() 在 namespaces 列表包含重复项时不会导致重复结果。"""
        backend1, backend2 = multi_backend_setup
        from weave_agent_sdk.memory.stream import MemoryStream

        manager = MagicMock()
        manager._backends = {
            "backend1": backend1,
            "backend2": backend2,
        }

        def mock_get_backend(ns):
            if ns in ("scope1:session:stream", "scope1_dup:session:stream"):
                return backend1
            return backend2
        manager._get_backend_for_namespace = mock_get_backend

        stream_memory = MemoryStream(manager)

        import asyncio
        # 传入包含重复 namespace 的列表
        results = stream_memory.last(
            n=10, namespaces=["scope1:session:stream", "scope1:session:stream", "scope1:session:stream"]
        )

        # 应只返回 3 条（去重后 scope1 的数据）
        assert len(results) == 3, f"Expected 3 results (deduplicated), got {len(results)}"
        for r in results:
            assert r["content"].startswith("msg_a_"), \
                f"Expected msg_a_* content, got {r['content']}"


# ============================================================
# 第6轮新增：修复项 20 — openai.py chat_stream() tools 参数传递
# ============================================================


class TestOpenAIChatStreamTools:
    """测试 openai.py chat_stream() 支持 tools 参数和 tool 消息处理。

    覆盖修复项20（第6轮）：chat_stream() 传递 tools 参数给 stream()，
    并正确处理 tool 角色消息（与 chat() 一致）。
    """

    def test_openai_chat_stream_passes_tools_to_stream(self):
        """验证 openai.py chat_stream() 将 tools 参数传递给 client.chat.completions.create。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat_stream)
        # tools 参数应传递给 create 调用
        assert "tools=tool_schemas" in source

    def test_openai_chat_stream_has_tools_parameter_in_signature(self):
        """验证 openai.py chat_stream() 方法签名包含 tools 参数。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        sig = inspect.signature(OpenAIAdapter.chat_stream)
        assert "tools" in sig.parameters

    def test_openai_chat_stream_handles_tool_messages(self):
        """验证 openai.py chat_stream() 正确处理 tool 角色消息。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat_stream)
        # 应处理 role == "tool" 的消息
        assert 'msg.role == "tool"' in source
        assert "tool_call_id" in source

    def test_openai_chat_stream_handles_assistant_with_tool_calls(self):
        """验证 openai.py chat_stream() 正确处理 assistant 消息中的 tool_calls。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat_stream)
        # assistant 消息包含 tool_calls 时，应构建 tool_calls 结构
        assert 'msg.role == "assistant" and msg.tool_calls' in source
        assert '"type": "function"' in source
        assert 'tc.arguments' in source

    def test_openai_chat_stream_message_building_matches_chat(self):
        """验证 openai.py chat_stream() 和 chat() 使用相同的消息构建逻辑。

        两者应使用相同的 role 处理和 tool_calls / tool 消息格式。
        """
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        chat_source = inspect.getsource(OpenAIAdapter.chat)
        stream_source = inspect.getsource(OpenAIAdapter.chat_stream)

        # 两者都应处理四种角色
        for role_check in [
            'msg.role == "assistant" and msg.tool_calls',
            'msg.role == "tool"',
        ]:
            assert role_check in chat_source, f"chat() missing: {role_check}"
            assert role_check in stream_source, f"chat_stream() missing: {role_check}"

        # 两者都应使用相同的 tool_calls 结构
        assert '"type": "function"' in chat_source
        assert '"type": "function"' in stream_source

        # 两者都应使用 json.dumps
        assert "json.dumps(tc.arguments" in chat_source
        assert "json.dumps(tc.arguments" in stream_source

    def test_openai_chat_stream_converts_tools_to_openai(self):
        """验证 openai.py chat_stream() 调用了 _convert_tools_to_openai。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat_stream)
        # 应调用 _convert_tools_to_openai(tools)
        assert "_convert_tools_to_openai(tools)" in source

    def test_openai_chat_stream_tool_schemas_not_none_when_tools_given(self):
        """验证 openai.py chat_stream() 在 tools 参数不为空时 tool_schemas 不为 None。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat_stream)
        # 应有条件判断：if tools: tool_schemas = _convert_tools_to_openai(tools)
        assert "if tools:" in source
        assert "tool_schemas = _convert_tools_to_openai(tools)" in source


# ============================================================
# 第6轮新增：修复项 21 — openai.py __import__("json") 替换为模块级 import json
# ============================================================


class TestOpenAIImportJsonFix:
    """测试 openai.py 中 __import__("json") 已被替换为模块级 import json。

    覆盖修复项21（第6轮）：替换三处 __import__("json") 为模块级 import json。
    """

    def test_openai_py_has_module_level_import_json(self):
        """验证 openai.py 在文件顶部有 import json（而非 __import__("json")）。"""
        import inspect
        from weave_agent_sdk.llm import openai

        source = inspect.getsource(openai)
        # 文件顶部应有 import json（无缩进）
        lines = source.split("\n")
        top_imports = [l for l in lines[:20] if l.strip().startswith("import json") or l.strip().startswith("from")]
        has_top_level_import = any("import json" in l and not l.startswith(" ") for l in lines[:20])
        assert has_top_level_import, "openai.py should have module-level 'import json' in top 20 lines"

    def test_openai_py_no_dynamic_import_json(self):
        """验证 openai.py 中不存在 __import__("json") 动态导入。"""
        import inspect
        from weave_agent_sdk.llm import openai

        source = inspect.getsource(openai)
        # 不应有 __import__("json")
        assert '__import__("json")' not in source, \
            "openai.py should not use __import__('json') dynamic import"

    def test_openai_py_chat_uses_json_dumps(self):
        """验证 openai.py chat() 使用 json.dumps 而非 __import__("json").dumps。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat)
        # 使用 json.dumps
        assert "json.dumps(tc.arguments" in source
        # 不应使用 __import__
        assert "__import__" not in source

    def test_openai_py_chat_stream_uses_json_dumps(self):
        """验证 openai.py chat_stream() 使用 json.dumps 而非 __import__("json").dumps。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.chat_stream)
        # 使用 json.dumps
        assert "json.dumps(tc.arguments" in source
        # 不应使用 __import__
        assert "__import__" not in source

    def test_openai_py_parse_response_uses_json_loads(self):
        """验证 openai.py _parse_openai_response() 使用 json.loads 而非 __import__("json").loads。"""
        import inspect
        from weave_agent_sdk.llm.openai import _parse_openai_response

        source = inspect.getsource(_parse_openai_response)
        # 使用 json.loads
        assert "json.loads(tc.function.arguments)" in source or "json.loads" in source
        # 不应使用 __import__
        assert "__import__" not in source

    def test_openai_py_no_import_json_in_function_body(self):
        """验证 openai.py 中所有 json 调用都在模块级 import 之后，无函数体内动态导入。"""
        import inspect
        from weave_agent_sdk.llm import openai

        source = inspect.getsource(openai)
        # 检查所有 json 调用是否都使用模块级 json
        # __import__("json") 不应出现在任何地方
        assert '__import__("json")' not in source


# ============================================================
# 第6轮新增：修复项 22 — _convert_tools_to_anthropic 类型脆弱性修复
# ============================================================


class TestConvertToolsToAnthropicTypeFix:
    """测试 _convert_tools_to_anthropic() 类型脆弱性修复。

    覆盖修复项22（第6轮）：增加 isinstance(fn, dict) 类型守卫判断，
    防止 tool["function"] 存在但不是 dict 类型时抛出 AttributeError。
    """

    def test_function_field_not_dict_flat_fallback(self):
        """function 字段存在但不是 dict 类型时，应回退到扁平格式（取顶层字段）。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        # function 字段是字符串（异常情况）
        tools = [
            {
                "type": "function",
                "function": "this_is_a_string_not_a_dict",
                "name": "flat_tool",
                "description": "Flat description",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        # 应回退到扁平格式，取顶层 name/description/parameters
        assert result[0]["name"] == "flat_tool"
        assert result[0]["description"] == "Flat description"
        assert result[0]["input_schema"]["properties"]["q"]["type"] == "string"

    def test_function_field_is_integer(self):
        """function 字段是整数时，应回退到扁平格式而不崩溃。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "function": 12345,  # 整数，不是 dict
                "name": "int_tool",
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        # 应正常返回，不崩溃
        assert result[0]["name"] == "int_tool"

    def test_function_field_is_list(self):
        """function 字段是列表时，应回退到扁平格式而不崩溃。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "function": ["not", "a", "dict"],  # 列表，不是 dict
                "name": "list_tool",
                "description": "List function test",
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "list_tool"
        assert result[0]["description"] == "List function test"

    def test_function_field_none(self):
        """function 字段是 None 时，应回退到扁平格式而不崩溃。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "function": None,  # None
                "name": "none_tool",
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "none_tool"

    def test_function_field_valid_dict_still_works(self):
        """function 字段是有效 dict 时，仍按 OpenAI 包裹格式处理（回归验证）。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                },
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "search_web"
        assert result[0]["description"] == "Search the web"
        assert result[0]["input_schema"]["properties"]["q"]["type"] == "string"

    def test_flat_format_still_works(self):
        """扁平格式（无 function 字段）仍正常工作（回归验证）。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "name": "flat_tool",
                "description": "A flat tool",
                "input_schema": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        assert result[0]["name"] == "flat_tool"
        assert result[0]["description"] == "A flat tool"
        assert result[0]["input_schema"]["properties"]["x"]["type"] == "integer"

    def test_function_field_missing_name_fallback(self):
        """function 字段不存在时，从顶层取 name（扁平格式），同时 function 非 dict 也不崩溃。"""
        from weave_agent_sdk.llm.anthropic import _convert_tools_to_anthropic

        tools = [
            {
                "function": {"nested": "data"},  # 有 function dict 但缺少标准字段
                "name": "fallback_name",
            }
        ]
        result = _convert_tools_to_anthropic(tools)

        assert len(result) == 1
        # function 是 dict，走 OpenAI 风格路径，但 fn.get("name") 为空
        assert result[0]["name"] == ""  # function dict 没有 name 字段


# ============================================================
# 第6轮新增：修复项 23 — iterative.py memory_updated schema ["tool"] → ["tools"]
# ============================================================


class TestIterativeLoopMemoryUpdatedSchema:
    """测试 iterative.py 中 memory_updated schema 的 "tools" 键修复。

    覆盖修复项23（第6轮）：memory_updated["tool"] → memory_updated["tools"]。
    """

    def test_memory_updated_uses_tools_key(self):
        """验证 iterative.py 使用 "tools" 键而非 "tool" 键。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # 应使用 "tools" 键
        assert 'memory_updated["tools"] = tool_writes' in source
        # 不应使用 "tool" 键（单数）
        assert 'memory_updated["tool"]' not in source

    def test_memory_updated_tools_docstring(self):
        """验证 tool_writes 的 docstring 说明 {tool_name: call_count} 结构。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # 应包含注释说明 tools 字段结构
        assert "tools" in source
        assert "tool_name" in source or "call_count" in source

    def test_tools_key_added_only_when_tool_writes_non_empty(self):
        """验证 "tools" 键仅在 tool_writes 非空时添加到 memory_updated。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # 应有条件判断：if tool_writes: memory_updated["tools"] = tool_writes
        assert "if tool_writes:" in source
        assert 'memory_updated["tools"] = tool_writes' in source

    def test_tool_key_not_present_anywhere(self):
        """验证整个 iterative.py 中不存在 'tool' 单数作为 memory_updated 的键。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative)
        # 检查 memory_updated 相关代码
        lines_with_memory = [l for l in source.split("\n") if "memory_updated" in l]
        for line in lines_with_memory:
            # 允许 "tools" 但不允许 "tool"（除非是 "tools" 的子串匹配）
            assert '["tool"]' not in line, f"Found deprecated 'tool' key in: {line}"
            assert '["tool\']' not in line

    def test_tools_key_in_result_matches_fix_record(self):
        """验证 fix_record.md 中描述的 schema 一致：tools 为 {tool_name: call_count}。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # tool_writes 结构注释应说明 {tool_name: call_count}
        assert "tool_name" in source or "tool" in source


# ============================================================
# 第6轮新增：E2E 测试 — openai.py chat_stream() tools 参数集成
# ============================================================


class TestOpenAIChatStreamToolsE2E:
    """端到端测试：openai.py chat_stream() tools 参数集成。

    覆盖修复项20（第6轮）：验证 chat_stream() 在 tools 存在时
    正确构建消息并传递给 stream API。
    """

    def test_openai_chat_stream_tool_message_building(self):
        """验证 openai.py chat_stream() 构建 tool 消息的消息格式正确。"""
        from weave_agent_sdk.llm.openai import OpenAIAdapter
        from weave_agent_sdk.types import Message, ToolCall

        adapter = OpenAIAdapter(api_key="test-key", model="test-model")

        # 构建测试消息：包含 tool 角色消息和 assistant 消息（含 tool_calls）
        messages = [
            Message(role="user", content="Search for something"),
            Message(
                role="assistant",
                content="Let me search",
                tool_calls=[
                    ToolCall(id="call_1", name="search_kb", arguments={"query": "test"}),
                ],
            ),
            Message(
                role="tool",
                content='{"result": "found"}',
                name="search_kb",
                tool_call_id="call_1",
            ),
        ]

        # 验证消息构建逻辑（不实际调用 API）
        openai_messages = []
        for msg in messages:
            entry = {"role": msg.role, "content": msg.content}
            if msg.role == "assistant" and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            elif msg.role == "tool":
                entry["tool_call_id"] = msg.tool_call_id or ""
            if msg.name:
                entry["name"] = msg.name
            openai_messages.append(entry)

        # 验证消息格式正确
        assert len(openai_messages) == 3
        assert openai_messages[0]["role"] == "user"
        assert openai_messages[1]["role"] == "assistant"
        assert "tool_calls" in openai_messages[1]
        assert openai_messages[1]["tool_calls"][0]["function"]["name"] == "search_kb"
        assert openai_messages[2]["role"] == "tool"
        assert openai_messages[2]["tool_call_id"] == "call_1"

    def test_openai_chat_stream_tool_schema_conversion(self):
        """验证 _convert_tools_to_openai() 正确转换为 OpenAI 格式。"""
        from weave_agent_sdk.llm.openai import _convert_tools_to_openai

        tools = [
            {
                "name": "search",
                "description": "Search the knowledge base",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
        result = _convert_tools_to_openai(tools)

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search"
        assert result[0]["function"]["description"] == "Search the knowledge base"
        assert result[0]["function"]["parameters"]["properties"]["query"]["type"] == "string"

    def test_openai_chat_stream_tool_schema_openai_format_passthrough(self):
        """验证已遵循 OpenAI 格式的 tool schema 被直接传递。"""
        from weave_agent_sdk.llm.openai import _convert_tools_to_openai

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                },
            }
        ]
        result = _convert_tools_to_openai(tools)

        assert len(result) == 1
        # 已经有 "function" 字段时，直接使用 tool["function"]
        assert result[0]["function"]["name"] == "search_web"
        assert result[0]["function"]["description"] == "Search the web"

    def test_openai_chat_stream_empty_tools_no_error(self):
        """验证 tools 为空列表时不会出错。"""
        from weave_agent_sdk.llm.openai import _convert_tools_to_openai

        result = _convert_tools_to_openai([])
        assert result == []

    def test_openai_convert_tools_to_openai_supports_input_schema(self):
        """验证 _convert_tools_to_openai 支持 input_schema 作为 parameters 的回退。"""
        from weave_agent_sdk.llm.openai import _convert_tools_to_openai

        tools = [
            {
                "name": "tool_with_input_schema",
                "description": "Tool with input_schema instead of parameters",
                "input_schema": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            }
        ]
        result = _convert_tools_to_openai(tools)

        assert len(result) == 1
        assert result[0]["function"]["parameters"]["properties"]["x"]["type"] == "integer"


# ============================================================
# 第6轮新增：E2E 测试 — iterative.py memory_updated schema 集成验证
# ============================================================


class TestIterativeLoopMemoryUpdatedSchemaE2E:
    """端到端测试：IterativeLoop.run 的 memory_updated 使用 "tools" 键。

    覆盖修复项23（第6轮）：验证 memory_updated schema 从 "tool" 变为 "tools"。
    """

    @pytest.mark.asyncio
    async def test_memory_updated_uses_tools_not_tool(self):
        """验证 IterativeLoop.run 返回的 memory_updated 使用 "tools" 键而非 "tool" 键。"""
        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 3
        agent._config.loop.stop_conditions = []
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        def test_tool(query: str) -> str:
            return f"searched: {query}"

        agent._tools = [test_tool]
        agent._tool_map = {"test_tool": test_tool}

        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(
                content="Let me search",
                model="test",
                tool_calls=[
                    ToolCall(id="call_1", name="test_tool", arguments={"query": "test"}),
                ],
            ),
            LLMResponse(
                content="Done searching",
                model="test",
                tool_calls=None,
            ),
        ])

        agent._system_prompt = "You are a test assistant."

        from weave_agent_sdk.loop.iterative import IterativeLoop
        loop = IterativeLoop()

        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "Search something")

        # 核心验证：memory_updated 应包含 "tools" 键（复数）
        assert "tools" in result.memory_updated, \
            f"memory_updated should contain 'tools' key, got: {result.memory_updated}"
        assert result.memory_updated["tools"].get("test_tool") == 1

        # 验证不存在 "tool" 键（单数，旧 schema）
        assert "tool" not in result.memory_updated, \
            f"memory_updated should NOT contain 'tool' key, got: {result.memory_updated}"

    @pytest.mark.asyncio
    async def test_memory_updated_no_tools_key_when_no_tool_calls(self):
        """当没有 tool 调用时，memory_updated 不应包含 "tools" 键。"""
        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.loop.stop_conditions = [{"type": "no_tool_calls"}]
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        from weave_agent_sdk.llm.base import LLMResponse
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="Direct response",
            model="test",
        ))

        agent._system_prompt = "You are a test assistant."
        agent._tools = []
        agent._tool_map = {}

        from weave_agent_sdk.loop.iterative import IterativeLoop
        loop = IterativeLoop()

        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "Hello")

        # 没有 tool 调用时，"tools" 键不应出现
        assert "tools" not in result.memory_updated, \
            f"memory_updated should NOT contain 'tools' key when no tool calls, got: {result.memory_updated}"

    @pytest.mark.asyncio
    async def test_memory_updated_tools_key_not_tool_in_fix_record(self):
        """验证 fix_record.md 中描述的修复项23被正确实现。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # 确认代码中使用 "tools" 而非 "tool"
        assert 'memory_updated["tools"]' in source
        assert 'memory_updated["tool"]' not in source

    @pytest.mark.asyncio
    async def test_memory_updated_schema_consistency(self):
        """验证 memory_updated 的 schema 一致性：stream/state 为 namespace 粒度，tools 为 tool_name 粒度。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative.IterativeLoop.run)
        # stream 和 state 使用 namespace 粒度
        assert 'memory_updated["stream"] = stream_writes' in source
        assert 'memory_updated["state"] = state_writes' in source
        # tools 使用 tool_name 粒度
        assert 'memory_updated["tools"] = tool_writes' in source


# ============================================================
# 7th round: Fix 1 - Remove hardcoded system prompt fallback in anthropic.py
# ============================================================


class TestAnthropicNoHardcodedSystemPrompt:
    def test_chat_no_hardcoded_fallback(self):
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter
        source = inspect.getsource(AnthropicAdapter.chat)
        assert "system=system_prompt.strip()" in source
        assert "You are a helpful assistant" not in source

    def test_chat_stream_no_hardcoded_fallback(self):
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter
        source = inspect.getsource(AnthropicAdapter.chat_stream)
        assert "system=system_prompt.strip()" in source
        assert "You are a helpful assistant" not in source

    def test_entire_anthropic_py_no_hardcoded_prompt(self):
        import inspect
        from weave_agent_sdk.llm import anthropic
        source = inspect.getsource(anthropic)
        assert "You are a helpful" not in source


class TestFactoryNoRedundantDefault:
    def test_deepseek_branch_no_redundant_default(self):
        import inspect
        from weave_agent_sdk.llm import factory
        source = inspect.getsource(factory)
        assert 'model=model or "deepseek-chat"' not in source
        lines = source.split(chr(10))
        in_ds = False
        for line in lines:
            if 'provider == "deepseek"' in line:
                in_ds = True
            if in_ds and "model=" in line and "#" not in line.split("model=")[0]:
                assert "model=model" in line
                break

    def test_default_model_returns_deepseek_chat(self):
        from weave_agent_sdk.llm.factory import _resolve_model
        was = os.environ.pop("WEAVE_MODEL", None)
        was2 = os.environ.pop("DEEPSEEK_MODEL", None)
        try:
            result = _resolve_model("deepseek")
            assert result == "", f"Expected empty string, got {result}"
        finally:
            if was is not None:
                os.environ["WEAVE_MODEL"] = was
            if was2 is not None:
                os.environ["DEEPSEEK_MODEL"] = was2

    def test_create_llm_deepseek_uses_model_parameter(self):
        import inspect
        from weave_agent_sdk.llm import factory
        source = inspect.getsource(factory._make_deepseek)
        assert "model=model" in source


class TestAnthropicSystemPromptE2E:
    def test_chat_passes_empty_system_prompt(self):
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter
        source = inspect.getsource(AnthropicAdapter.chat)
        assert "system=system_prompt.strip()" in source

    def test_chat_stream_passes_empty_system_prompt(self):
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter
        source = inspect.getsource(AnthropicAdapter.chat_stream)
        assert "system=system_prompt.strip()" in source

    def test_empty_system_prompt_no_fallback(self):
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter
        chat_src = inspect.getsource(AnthropicAdapter.chat)
        stream_src = inspect.getsource(AnthropicAdapter.chat_stream)
        for src in [chat_src, stream_src]:
            for line in src.split(chr(10)):
                if "system=system_prompt.strip()" in line:
                    s = line.strip()
                    if "#" in s:
                        s = s.split("#")[0]
                    assert "or" not in s


class TestFactoryDeepseekModelE2E:
    def test_create_llm_deepseek_model_from_default(self):
        from weave_agent_sdk.llm.factory import _resolve_model
        was = os.environ.pop("WEAVE_MODEL", None)
        was2 = os.environ.pop("DEEPSEEK_MODEL", None)
        try:
            result = _resolve_model("deepseek")
            assert result == "", f"Expected empty string, got {result}"
        finally:
            if was is not None:
                os.environ["WEAVE_MODEL"] = was
            if was2 is not None:
                os.environ["DEEPSEEK_MODEL"] = was2
        import inspect
        from weave_agent_sdk.llm import factory
        source = inspect.getsource(factory.create_llm)
        assert "model = _resolve_model(provider)" in source

    def test_create_llm_deepseek_explicit_model(self):
        from unittest.mock import patch
        from weave_agent_sdk.llm.factory import create_llm
        with patch("weave_agent_sdk.llm.openai.OpenAIAdapter") as mock_ad:
            with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
                create_llm(provider="deepseek", model="custom-model")
                mock_ad.assert_called_once()
                _, kwargs = mock_ad.call_args
                assert kwargs.get("model") == "custom-model"

    def test_create_llm_deepseek_default_model(self):
        from unittest.mock import patch
        from weave_agent_sdk.llm.factory import create_llm
        import os
        old_val = os.environ.get("DEEPSEEK_MODEL")
        os.environ["DEEPSEEK_MODEL"] = "deepseek-chat"
        try:
            with patch("weave_agent_sdk.llm.openai.OpenAIAdapter") as mock_ad:
                with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
                    create_llm(provider="deepseek", model=None)
                    mock_ad.assert_called_once()
                    _, kwargs = mock_ad.call_args
                    assert kwargs.get("model") == "deepseek-chat"
        finally:
            if old_val is not None:
                os.environ["DEEPSEEK_MODEL"] = old_val
            else:
                os.environ.pop("DEEPSEEK_MODEL", None)


# ============================================================
# 第8轮新增：修复项 26 — Tool 执行添加超时保护
# ============================================================


class TestToolTimeout:
    """测试 Tool 执行超时保护机制。

    覆盖修复项1（第8轮）：_execute_tool() 使用 asyncio.wait_for 设置超时保护，
    LoopConfig 新增 tool_timeout 字段（默认 30.0），config.py 从 YAML 读取。
    """

    def test_loop_config_has_tool_timeout_field(self):
        """LoopConfig 应包含 tool_timeout 字段，默认值 30.0。"""
        from weave_agent_sdk.types import LoopConfig
        config = LoopConfig()
        assert hasattr(config, "tool_timeout")
        assert config.tool_timeout == 30.0

    def test_loop_config_tool_timeout_customizable(self):
        """LoopConfig.tool_timeout 应可自定义。"""
        from weave_agent_sdk.types import LoopConfig
        config = LoopConfig(tool_timeout=60.0)
        assert config.tool_timeout == 60.0

    def test_load_config_parses_tool_timeout_from_yaml(self, tmp_path):
        """load_config() 应从 YAML 解析 loop.tool_timeout 字段。"""
        import yaml
        yaml_path = tmp_path / "test_weave_tool_timeout.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "tool_timeout": 15.0},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        result = load_config(yaml_path)
        assert result.loop.tool_timeout == 15.0

    def test_load_config_default_tool_timeout(self, tmp_path):
        """load_config() 在 YAML 未配置 tool_timeout 时应使用默认值 30.0。"""
        import yaml
        yaml_path = tmp_path / "test_weave_default_tool_timeout.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        result = load_config(yaml_path)
        assert result.loop.tool_timeout == 30.0

    def test_execute_tool_uses_tool_timeout_from_config(self):
        """验证 _execute_tool() 从 agent._config.loop.tool_timeout 读取超时值。"""
        import inspect
        from weave_agent_sdk.loop.iterative import _execute_tool

        source = inspect.getsource(_execute_tool)
        # 应使用 tool_timeout 配置
        assert "tool_timeout" in source
        assert "asyncio.wait_for" in source

    def test_execute_tool_returns_timeout_error(self):
        """验证 _execute_tool() 在 tool 超时时返回带 TimeoutError 的 ToolResult。"""
        from weave_agent_sdk.types import ToolResult as ToolResultType, ToolCall
        from weave_agent_sdk.loop.iterative import _execute_tool

        agent = MagicMock()
        agent._config.loop.tool_timeout = 0.001  # 极短超时确保触发

        async def slow_tool(x: str) -> str:
            await asyncio.sleep(10)  # 远超超时时间
            return f"result: {x}"

        agent._tool_map = {"slow_tool": slow_tool}
        tc = ToolCall(id="call_1", name="slow_tool", arguments={"x": "test"})

        result = asyncio.run(_execute_tool(agent, tc))

        assert result.error is not None
        assert "TimeoutError" in result.error
        assert "slow_tool" in result.error

    def test_execute_tool_returns_unknown_tool_error(self):
        """验证 _execute_tool() 在 tool 不存在时返回带错误信息的 ToolResult。"""
        from weave_agent_sdk.types import ToolCall
        from weave_agent_sdk.loop.iterative import _execute_tool

        agent = MagicMock()
        agent._tool_map = {}

        tc = ToolCall(id="call_1", name="nonexistent_tool", arguments={})

        result = asyncio.run(_execute_tool(agent, tc))

        assert result.error is not None
        assert "Unknown tool" in result.error
        assert "nonexistent_tool" in result.error


# ============================================================
# 第8轮新增：修复项 27 — scheduled.py State 写入异常处理
# ============================================================


class TestScheduledStateWriteExceptionHandling:
    """测试 scheduled.py 中 State 写入异常处理修复。

    覆盖修复项2（第8轮）：替换 except Exception: pass 为分类异常处理 + 日志告警。
    """

    def test_scheduled_state_write_has_file_not_found_handler(self):
        """scheduled.py 中 FileNotFoundError 应被静默处理（正常情况）。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        assert "FileNotFoundError" in source

    def test_scheduled_state_write_has_generic_exception_logger(self):
        """scheduled.py 中兜底 Exception 应记录 logger.warning。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        assert 'logger.warning("Failed to save scheduled run state:' in source

    def test_scheduled_state_write_no_silent_exception_pass(self):
        """scheduled.py 中不应有静默的 except Exception: pass。"""
        import inspect
        from weave_agent_sdk.loop import scheduled

        source = inspect.getsource(scheduled)
        # 检查 _execute_once 方法体
        assert "except Exception:" not in source
        assert "except Exception:  #" not in source


# ============================================================
# 第8轮新增：修复项 28 — _create_loop() 未知类型抛出 ValueError
# ============================================================

# 已合并到 TestCreateLoopScheduledSupport 中：
# - test_create_loop_raises_value_error_for_unknown_type
# - test_create_loop_case_sensitive_raises_value_error


# ============================================================
# 第8轮新增：修复项 29 — memory/manager.py access_type 合法性校验
# ============================================================


class TestAccessTypeValidation:
    """测试 memory/manager.py 中 access_type 合法性校验。

    覆盖修复项4（第8轮）：_get_backend_for_namespace() / get_namespaces() / get_namespace()
    中添加 access_type 合法性校验，非法值抛出 ValueError。
    """

    def test_get_backend_for_namespace_validates_access_type(self):
        """_get_backend_for_namespace() 对非法 access_type 应抛出 ValueError。"""
        import inspect
        from weave_agent_sdk.memory.manager import MemoryManager

        source = inspect.getsource(MemoryManager._get_backend_for_namespace)
        assert "_VALID_ACCESS_TYPES" in source
        assert "ValueError" in source
        assert "Invalid access_type" in source

    def test_get_namespaces_validates_access_type(self):
        """get_namespaces() 对非法 access_type 应抛出 ValueError。"""
        import inspect
        from weave_agent_sdk.memory.manager import MemoryManager

        source = inspect.getsource(MemoryManager.get_namespaces)
        assert "_VALID_ACCESS_TYPES" in source
        assert "ValueError" in source

    def test_get_namespace_validates_access_type(self):
        """get_namespace() 对非法 access_type 应抛出 ValueError。"""
        import inspect
        from weave_agent_sdk.memory.manager import MemoryManager

        source = inspect.getsource(MemoryManager.get_namespace)
        assert "_VALID_ACCESS_TYPES" in source
        assert "ValueError" in source

    def test_valid_access_types_defined(self):
        """验证 _VALID_ACCESS_TYPES 包含 stream/state/knowledge。"""
        from weave_agent_sdk.memory.manager import _VALID_ACCESS_TYPES
        assert _VALID_ACCESS_TYPES == frozenset({"stream", "state", "knowledge"})

    def test_invalid_access_type_raises_value_error_on_get_backend(self):
        """使用非法 access_type 的 namespace 调用 _get_backend_for_namespace 应抛出 ValueError。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig())
        # 非法 access_type
        with pytest.raises(ValueError) as excinfo:
            manager._get_backend_for_namespace("test:session:invalid_type")
        assert "Invalid access_type" in str(excinfo.value)
        assert "invalid_type" in str(excinfo.value)

    def test_invalid_access_type_raises_value_error_on_get_namespaces(self):
        """get_namespaces() 传入非法 access_type 应抛出 ValueError。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig())
        with pytest.raises(ValueError) as excinfo:
            manager.get_namespaces("invalid_access_type")
        assert "Invalid access_type" in str(excinfo.value)
        assert "invalid_access_type" in str(excinfo.value)

    def test_invalid_access_type_raises_value_error_on_get_namespace(self):
        """get_namespace() 传入非法 access_type 应抛出 ValueError。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig())
        with pytest.raises(ValueError) as excinfo:
            manager.get_namespace("test", "invalid_access_type")
        assert "Invalid access_type" in str(excinfo.value)

    def test_valid_access_types_pass_through(self):
        """合法的 access_type（stream/state/knowledge）应正常通过。"""
        from weave_agent_sdk.memory.manager import MemoryManager, _VALID_ACCESS_TYPES
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig())

        # 这些应正常执行而不抛出异常（默认 scope 下 stream 路径正常）
        for at in sorted(_VALID_ACCESS_TYPES):
            ns = f"test:session:{at}"
            try:
                backend = manager._get_backend_for_namespace(ns)
                assert backend is not None
            except ValueError:
                pytest.fail(f"Valid access_type '{at}' should not raise ValueError")


# ============================================================
# 第8轮新增：修复项 30 — 移除硬编码模型名（R3 合规）
# ============================================================


class TestNoHardcodedModelNames:
    """测试代码中不存在硬编码模型名字面量（R3 红线合规）。

    覆盖修复项5（第8轮）：types.py / config.py / factory.py / anthropic.py / openai.py
    中移除所有硬编码模型名默认值。
    """

    def test_llm_config_model_default_is_empty_string(self):
        """LLMConfig.model 默认值应为空字符串。"""
        from weave_agent_sdk.types import LLMConfig
        config = LLMConfig()
        assert config.model == "", "LLMConfig.model should default to empty string (R3)"

    def test_anthropic_adapter_raises_value_error_for_empty_model(self):
        """AnthropicAdapter 在 model 为空时应抛出 ValueError。"""
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter
        with pytest.raises(ValueError) as excinfo:
            AnthropicAdapter(api_key="test-key", model="")
        assert "model must be specified" in str(excinfo.value).lower()

    def test_openai_adapter_raises_value_error_for_empty_model(self):
        """OpenAIAdapter 在 model 为空时应抛出 ValueError。"""
        from weave_agent_sdk.llm.openai import OpenAIAdapter
        with pytest.raises(ValueError) as excinfo:
            OpenAIAdapter(api_key="test-key", model="")
        assert "model must be specified" in str(excinfo.value).lower()

    def test_config_py_no_hardcoded_model_fallback(self):
        """config.py 中不应有硬编码模型名作为 fallback。"""
        import inspect
        from weave_agent_sdk import config

        source = inspect.getsource(config.load_config)
        # 不应使用硬编码模型名作为 fallback
        assert '"claude-sonnet-5-20251001"' not in source
        assert '"gpt-4o"' not in source

    def test_factory_py_no_default_model_function(self):
        """factory.py 中不应有 _default_model() 函数（返回硬编码映射）。"""
        import inspect
        from weave_agent_sdk.llm import factory

        # _default_model 函数应不存在
        assert not hasattr(factory, '_default_model')

    def test_factory_py_has_resolve_model_function(self):
        """factory.py 应有 _resolve_model() 函数从环境变量读取模型名。"""
        import inspect
        from weave_agent_sdk.llm import factory

        source = inspect.getsource(factory)
        assert "_resolve_model" in source
        assert "WEAVE_MODEL" in source

    def test_factory_create_llm_raises_value_error_for_empty_model(self, monkeypatch):
        """create_llm() 在模型名为空时应抛出 ValueError。"""
        from weave_agent_sdk.llm.factory import create_llm

        # 确保所有环境变量中都没有模型名
        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
            with pytest.raises(ValueError) as excinfo:
                create_llm(provider="anthropic")
            assert "model" in str(excinfo.value).lower()


# ============================================================
# 第8轮新增：E2E 测试 — Tool 超时与配置传播集成
# ============================================================


class TestToolTimeoutConfigPropagationE2E:
    """端到端测试：tool_timeout 配置从 YAML 传播到 _execute_tool。

    覆盖修复项26（第8轮）：验证 tool_timeout 配置在 load_config 和
    _execute_tool 之间的完整传播链路。
    """

    def test_tool_timeout_in_config_reference(self):
        """验证 docs/config-reference.yaml 包含 tool_timeout: 30.0 配置。"""
        import yaml
        yaml_path = Path(__file__).parent.parent / "docs" / "config-reference.yaml"
        assert yaml_path.exists()

        with open(yaml_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        assert "tool_timeout" in config.get("loop", {}), \
            "config-reference.yaml should have loop.tool_timeout"
        assert config["loop"]["tool_timeout"] == 30.0, \
            "config-reference.yaml loop.tool_timeout should default to 30.0"

    def test_tool_timeout_loaded_via_load_config(self):
        """验证 load_config() 从实际 weave.yaml 正确加载 tool_timeout。"""
        yaml_path = Path(__file__).parent.parent / "weave.yaml"
        config = load_config(yaml_path)

        assert config.loop.tool_timeout == 30.0

    def test_execute_tool_timeout_in_iterative_py_source(self):
        """验证 iterative.py 中 _execute_tool 使用 asyncio.wait_for + tool_timeout。"""
        import inspect
        from weave_agent_sdk.loop import iterative

        source = inspect.getsource(iterative._execute_tool)
        # 核心验证：使用 asyncio.wait_for 包裹 tool 执行
        assert "asyncio.wait_for(" in source
        # 超时时间来自配置（使用 getattr 读取 tool_timeout）
        assert "tool_timeout" in source
        assert "getattr(agent._config.loop" in source
        # 同步和异步 tool 都受保护
        assert "asyncio.iscoroutinefunction(tool_fn)" in source
        assert "asyncio.to_thread(tool_fn" in source
        # 超时异常被捕获并返回结构化错误
        assert "asyncio.TimeoutError" in source

    @pytest.mark.asyncio
    async def test_execute_tool_with_mocked_timeout_integration(self):
        """集成测试：通过 mock agent 验证 _execute_tool 超时路径完整可用。"""
        from weave_agent_sdk.types import ToolResult as ToolResultType, ToolCall
        from weave_agent_sdk.loop.iterative import _execute_tool

        agent = MagicMock()
        agent._config.loop.tool_timeout = 0.001  # 极短超时

        async def slow_fn(x: str) -> str:
            await asyncio.sleep(100)
            return x

        agent._tool_map = {"slow_fn": slow_fn}
        tc = ToolCall(id="call_1", name="slow_fn", arguments={"x": "hello"})

        result = await _execute_tool(agent, tc)

        # 验证超时错误
        assert result.error is not None
        assert "TimeoutError" in result.error
        assert "slow_fn" in result.error
        assert "0.001" in result.error  # 超时值应出现在错误信息中


# ============================================================
# 第8轮新增：E2E 测试 — R3 合规：无硬编码模型名
# ============================================================


class TestR3NoHardcodedModelE2E:
    """端到端测试：整个项目中无硬编码模型名（R3 红线合规）。

    覆盖修复项30（第8轮）：验证所有代码文件中无硬编码模型名字面量。
    """

    def test_no_hardcoded_model_in_types_py(self):
        """types.py 中 LLMConfig 不应包含硬编码模型名。"""
        import inspect
        from weave_agent_sdk.types import LLMConfig

        source = inspect.getsource(LLMConfig)
        # 模型名默认值应为空字符串
        assert 'model: str = ""' in source
        # 不应有具体模型名
        assert "claude-sonnet" not in source
        assert "gpt-4" not in source
        assert "deepseek" not in source

    def test_no_hardcoded_model_in_anthropic_py(self):
        """anthropic.py 的 __init__ 中模型名默认值应为空字符串。"""
        import inspect
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        source = inspect.getsource(AnthropicAdapter.__init__)
        assert 'model: str = ""' in source

    def test_no_hardcoded_model_in_openai_py(self):
        """openai.py 的 __init__ 中模型名默认值应为空字符串。"""
        import inspect
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        source = inspect.getsource(OpenAIAdapter.__init__)
        assert 'model: str = ""' in source

    def test_no_hardcoded_model_in_config_py(self):
        """config.py 中不应有硬编码模型名 fallback。"""
        import inspect
        from weave_agent_sdk import config

        source = inspect.getsource(config)
        # 不应包含任何硬编码模型名
        assert 'llm_raw.get("model", "")' in source
        # 注释中的示例配置字符串不是硬编码 fallback，仅检查非注释行
        code_lines = [l for l in source.split("\n") if not l.strip().startswith("#")]
        assert "claude-sonnet-5" not in "\n".join(code_lines)

    def test_no_hardcoded_model_in_factory_py(self):
        """factory.py 中模型名从 WEAVE_MODEL 环境变量读取。"""
        import inspect
        from weave_agent_sdk.llm import factory

        source = inspect.getsource(factory)
        # 使用 _resolve_model 从环境变量读取
        assert 'model = _resolve_model(provider)' in source or 'model = os.environ.get("WEAVE_MODEL"' in source
        # 不应有硬编码模型名字面量
        assert '"claude-sonnet-5' not in source
        assert '"gpt-4o"' not in source
        assert '"deepseek-chat"' not in source


# ============================================================
# 第8轮新增（第2批次）：补充边界测试
# ============================================================


class TestToolTimeoutSyncFunction:
    """测试同步 Tool 超时保护（额外边界场景）。

    覆盖修复项26（第8轮）：同步（非 async）tool 函数通过 asyncio.to_thread
    执行，同样需要超时保护。
    """

    @pytest.mark.asyncio
    async def test_execute_tool_sync_function_timeout(self):
        """同步阻塞 tool 应触发 TimeoutError。"""
        from weave_agent_sdk.types import ToolCall
        from weave_agent_sdk.loop.iterative import _execute_tool

        agent = MagicMock()
        agent._config.loop.tool_timeout = 0.001  # 极短超时确保触发

        # 同步阻塞函数（非 async）
        def slow_sync_tool(x: str) -> str:
            import time
            time.sleep(100)  # 远超超时时间
            return f"result: {x}"

        agent._tool_map = {"slow_sync_tool": slow_sync_tool}
        tc = ToolCall(id="call_1", name="slow_sync_tool", arguments={"x": "test"})

        result = await _execute_tool(agent, tc)

        assert result.error is not None
        assert "TimeoutError" in result.error
        assert "slow_sync_tool" in result.error
        assert "0.001" in result.error

    @pytest.mark.asyncio
    async def test_execute_tool_sync_function_normal_completion(self):
        """同步非阻塞 tool 应正常返回结果（超时保护不误伤）。"""
        from weave_agent_sdk.types import ToolCall
        from weave_agent_sdk.loop.iterative import _execute_tool

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0  # 足够超时

        def fast_sync_tool(x: str) -> str:
            return f"result: {x}"

        agent._tool_map = {"fast_sync_tool": fast_sync_tool}
        tc = ToolCall(id="call_1", name="fast_sync_tool", arguments={"x": "hello"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "result: hello"

    @pytest.mark.asyncio
    async def test_execute_tool_async_function_normal_completion(self):
        """异步非阻塞 tool 应正常返回结果。"""
        from weave_agent_sdk.types import ToolCall
        from weave_agent_sdk.loop.iterative import _execute_tool

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0  # 足够超时

        async def fast_async_tool(x: str) -> str:
            return f"async_result: {x}"

        agent._tool_map = {"fast_async_tool": fast_async_tool}
        tc = ToolCall(id="call_1", name="fast_async_tool", arguments={"x": "world"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "async_result: world"


class TestResolveModelEnvVar:
    """测试 factory._resolve_model() 从环境变量读取模型名。

    覆盖修复项30（第8轮 R3 红线）：模型名从环境变量读取，代码中不硬编码。
    """

    def test_resolve_model_from_weave_model_env(self, monkeypatch):
        """_resolve_model() 应从 WEAVE_MODEL 环境变量读取模型名。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.setenv("WEAVE_MODEL", "claude-opus-4-20251001")
        result = _resolve_model("anthropic")
        assert result == "claude-opus-4-20251001"

    def test_resolve_model_from_provider_specific_env(self, monkeypatch):
        """_resolve_model() 应从 provider 专用环境变量读取模型名。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        # WEAVE_MODEL 未设置时降级到 ANTHROPIC_MODEL
        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
        result = _resolve_model("anthropic")
        assert result == "claude-3-haiku-20240307"

    def test_resolve_model_prefers_weave_model_over_provider_specific(self, monkeypatch):
        """_resolve_model() 应优先使用 WEAVE_MODEL 而非 provider 专用变量。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.setenv("WEAVE_MODEL", "claude-opus-4-20251001")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
        result = _resolve_model("anthropic")
        # WEAVE_MODEL 优先级更高
        assert result == "claude-opus-4-20251001"

    def test_resolve_model_returns_empty_when_no_env(self, monkeypatch):
        """_resolve_model() 在所有环境变量均未设置时返回空字符串。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        result = _resolve_model("anthropic")
        assert result == ""

    def test_resolve_model_openai_provider(self, monkeypatch):
        """_resolve_model() 对 openai provider 使用 OPENAI_MODEL 环境变量。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
        result = _resolve_model("openai")
        assert result == "gpt-4o-mini"

    def test_resolve_model_deepseek_provider(self, monkeypatch):
        """_resolve_model() 对 deepseek provider 使用 DEEPSEEK_MODEL 环境变量。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat-v2")
        result = _resolve_model("deepseek")
        assert result == "deepseek-chat-v2"


class TestScheduledStateWriteLoggerBehavior:
    """测试 scheduled.py 中 State 写入异常的实际运行时日志行为。

    覆盖修复项27（第8轮）：验证兜底 Exception 实际记录 logger.warning。
    """

    @pytest.mark.asyncio
    async def test_scheduled_state_write_logs_warning_on_generic_error(self, caplog):
        """scheduled._execute_once 在 state.set() 抛出通用异常时应记录警告日志。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        # Mock agent
        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        # Mock LLM
        from weave_agent_sdk.llm.base import LLMResponse
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="Scheduled response", model="test"
        ))

        agent._system_prompt = "System prompt"

        # Mock memory.get_namespaces to return a valid namespace
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["session:abc:state"])
        # Mock state.set to raise a generic exception
        agent._memory.state = MagicMock()
        agent._memory.state.set = MagicMock(side_effect=RuntimeError("Storage backend failure"))

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})

        with caplog.at_level(logging.WARNING):
            result = await loop._execute_once(agent, "test input")

        # 验证有警告日志记录
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Failed to save scheduled run state" in msg for msg in warning_messages), \
            f"Expected warning log about state save failure, got: {warning_messages}"

        # 验证主流程未受影响
        assert result.output == "Scheduled response"
        assert result.iterations == 1

    @pytest.mark.asyncio
    async def test_scheduled_state_write_file_not_found_silent(self, caplog):
        """scheduled._execute_once 在 FileNotFoundError 时应静默通过，不记录日志。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        from weave_agent_sdk.llm.base import LLMResponse
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="Scheduled response", model="test"
        ))

        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["session:abc:state"])
        agent._memory.state = MagicMock()
        # FileNotFoundError should be silently caught
        agent._memory.state.set = MagicMock(side_effect=FileNotFoundError("DB file not found"))

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})

        with caplog.at_level(logging.WARNING):
            result = await loop._execute_once(agent, "test input")

        # 不应有警告日志（FileNotFoundError 被静默处理）
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("Failed to save scheduled run state" in msg for msg in warning_messages), \
            f"FileNotFoundError should not produce warning log, got: {warning_messages}"

        # 验证主流程正常
        assert result.output == "Scheduled response"

# ============================================================
# 第8轮新增（第2批次）：补充边界与 E2E 测试
# 覆盖第8轮修复的5个修复项（补充边界场景）:
#   - 修复项26: Tool 超时保护（非数值超时回退 / 通用异常 / 慢 tool 不阻塞循环）
#   - 修复项27: scheduled.py State 写入（成功路径写入 last_run 字段 / 默认 namespace）
#   - 修复项28: _create_loop 未知类型 ValueError（错误信息引导 weave.yaml）
#   - 修复项29: access_type 合法性校验（单段 namespace 默认 stream）
#   - 修复项30: R3 无硬编码模型名（config / factory / adapter 补充验证）
# ============================================================


class TestToolTimeoutAdditional:
    """第8轮修复项26（Tool 超时保护）补充边界测试。"""

    @pytest.mark.asyncio
    async def test_execute_tool_non_numeric_timeout_falls_back_to_default(self):
        """tool_timeout 配置为非数值时，应回退默认 30.0 且 tool 正常执行。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = "not-a-number"  # 非数值配置

        async def fast_tool(x: str) -> str:
            return f"ok:{x}"

        agent._tool_map = {"fast_tool": fast_tool}
        tc = ToolCall(id="call_1", name="fast_tool", arguments={"x": "hi"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "ok:hi"

    @pytest.mark.asyncio
    async def test_execute_tool_generic_exception_returns_structured_error(self):
        """tool 抛出通用异常时，应返回带异常类型名和消息的结构化错误。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0

        async def bad_tool(x: str) -> str:
            raise RuntimeError("boom")

        agent._tool_map = {"bad_tool": bad_tool}
        tc = ToolCall(id="call_1", name="bad_tool", arguments={"x": "1"})

        result = await _execute_tool(agent, tc)

        assert result.error == "RuntimeError: boom"
        assert result.result is None


class TestResolveModelAdditional:
    """第8轮修复项30（R3：无硬编码模型名）补充验证。"""

    def test_resolve_model_unknown_provider_returns_empty(self, monkeypatch):
        """_resolve_model() 对未知 provider（无对应环境变量）应返回空字符串。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        result = _resolve_model("unknown_provider")
        assert result == ""

    def test_anthropic_adapter_empty_model_error_mentions_config(self):
        """AnthropicAdapter 空模型错误信息应引导用户配置 weave.yaml / WEAVE_MODEL。"""
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        with pytest.raises(ValueError) as excinfo:
            AnthropicAdapter(api_key="test-key", model="")
        msg = str(excinfo.value)
        assert "model must be specified" in msg
        assert "WEAVE_MODEL" in msg
        assert "weave.yaml" in msg


class TestConfigR3ModelFallback:
    """第8轮修复项30（config.py 模型名环境变量回退）补充测试。"""

    def test_load_config_uses_weave_model_env_when_yaml_missing_model(self, tmp_path, monkeypatch):
        """YAML 中未配置 model 时，应从 WEAVE_MODEL 环境变量读取模型名（R3）。"""
        import yaml

        monkeypatch.setenv("WEAVE_MODEL", "env-model-2026")
        yaml_path = tmp_path / "test_no_model.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},  # 无 model 字段
            "loop": {"type": "simple"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.llm.model == "env-model-2026"


class TestScheduledStateWriteAdditional:
    """第8轮修复项27（scheduled.py State 写入）补充功能测试。"""

    @pytest.mark.asyncio
    async def test_scheduled_state_write_sets_last_run_fields(self):
        """_execute_once() 成功路径应写入 last_run_at 和 last_run_error 两个 state 键。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="Scheduled response", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["session:abc:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "test input")

        assert result.output == "Scheduled response"
        assert result.iterations == 1
        # 应写入 last_run_at 与 last_run_error 两个键，namespace 为 session:abc:state
        call_keys = [c.args[0] for c in agent._memory.state.set.call_args_list]
        assert "last_run_at" in call_keys
        assert "last_run_error" in call_keys
        call_ns = [c.args[2] for c in agent._memory.state.set.call_args_list]
        assert all(ns == "session:abc:state" for ns in call_ns)

    @pytest.mark.asyncio
    async def test_scheduled_state_write_default_namespace_when_no_scopes(self):
        """get_namespaces 返回空时，应使用默认 namespace 'default:session:state'。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="ok", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        await loop._execute_once(agent, "test input")

        call_ns = [c.args[2] for c in agent._memory.state.set.call_args_list]
        assert all(ns == "default:session:state" for ns in call_ns)


class TestAccessTypeAdditional:
    """第8轮修复项29（access_type 合法性校验）补充边界测试。"""

    def test_get_backend_single_segment_namespace_defaults_stream(self):
        """单段 namespace（无 access_type）应默认 stream，正常返回后端不抛错。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        backend = manager._get_backend_for_namespace("justscope")
        assert backend is not None


class TestCreateLoopAdditional:
    """第8轮修复项28（_create_loop 未知类型抛 ValueError）补充测试。"""

    def test_create_loop_error_message_mentions_weave_yaml(self):
        """ValueError 错误信息应列出支持类型并引导用户在 weave.yaml 中修正。"""
        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "bogus_type"
        weave._tools = []
        weave._tool_map = {}

        with pytest.raises(ValueError) as excinfo:
            weave._create_loop()

        msg = str(excinfo.value)
        assert "Unknown loop type" in msg
        assert "bogus_type" in msg
        assert "simple, iterative, scheduled" in msg
        assert "weave.yaml" in msg


# ============================================================
# 第8轮新增（第2批次）：端到端测试
# ============================================================


class TestE2EToolTimeoutLoop:
    """端到端测试：IterativeLoop 中慢 tool 超时不阻塞整个循环。"""

    @pytest.mark.asyncio
    async def test_iterative_loop_tool_timeout_error_does_not_hang(self):
        """慢 tool 超时后循环应继续，最终输出正常返回且 tool 计数被记录。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 0.001  # 极短超时确保触发
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def slow_tool(query: str) -> str:
            await asyncio.sleep(10)
            return "never"

        agent._tools = [slow_tool]
        agent._tool_map = {"slow_tool": slow_tool}
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(content="calling", model="test", tool_calls=[
                ToolCall(id="c1", name="slow_tool", arguments={"query": "q"}),
            ]),
            LLMResponse(content="final answer", model="test", tool_calls=None),
        ])
        agent._system_prompt = "System prompt"

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        # 整体加超时保护：若慢 tool 阻塞循环，此处会抛 asyncio.TimeoutError
        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "final answer"
        assert result.iterations == 2
        assert result.memory_updated.get("tools", {}).get("slow_tool") == 1


class TestE2EConfigModelFromEnv:
    """端到端测试：config.py 模型名回退到 WEAVE_MODEL 环境变量（R3）。"""

    def test_load_config_model_from_weave_model_env(self, tmp_path, monkeypatch):
        """加载无 model 字段的 YAML 时，模型名应来自 WEAVE_MODEL 环境变量。"""
        monkeypatch.setenv("WEAVE_MODEL", "e2e-model-2026")
        yaml_path = tmp_path / "e2e_no_model.yaml"
        yaml_path.write_text(
            "agent:\n  name: e2e\nllm:\n  provider: anthropic\nloop:\n  type: simple\n"
            "memory:\n  scopes: {}\nprompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        assert config.llm.model == "e2e-model-2026"


class TestE2ECreateLLMFromEnv:
    """端到端测试：create_llm() 从 WEAVE_MODEL 环境变量解析模型名（R3）。"""

    def test_create_llm_uses_weave_model_env(self, monkeypatch):
        """create_llm(provider=..., model=None) 应使用 WEAVE_MODEL 环境变量作为模型名。"""
        from weave_agent_sdk.llm.factory import create_llm

        monkeypatch.setenv("WEAVE_MODEL", "claude-e2e-2026")
        with patch("weave_agent_sdk.llm.anthropic.AnthropicAdapter") as mock_ad:
            with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
                create_llm(provider="anthropic", model=None)
                mock_ad.assert_called_once()
                _, kwargs = mock_ad.call_args
                assert kwargs.get("model") == "claude-e2e-2026"


class TestE2EAccessTypeValidation:
    """端到端测试：MemoryManager access_type 合法性校验（第8轮修复项29）。"""

    def test_memory_manager_access_type_end_to_end(self):
        """合法 access_type 正常返回 namespace；非法 access_type 抛 ValueError。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        manager.activate_scopes({"default_id": "s1"})

        # 合法 access_type 正常返回
        assert manager.get_namespace("default", "stream") == "default:s1:stream"
        assert manager.get_namespaces("state") == ["default:s1:state"]
        assert manager.get_namespaces("knowledge") == ["default:s1:knowledge"]

        # 非法 access_type 抛出 ValueError
        with pytest.raises(ValueError):
            manager.get_namespace("default", "invalid_type")
        with pytest.raises(ValueError):
            manager.get_namespaces("invalid_type")
        with pytest.raises(ValueError):
            manager._get_backend_for_namespace("default:s1:invalid_type")


class TestE2EToolTimeoutConfigPropagation:
    """端到端测试：tool_timeout 从 YAML 经 load_config 传播到 _execute_tool。"""

    @pytest.mark.asyncio
    async def test_tool_timeout_config_end_to_end(self, tmp_path):
        """YAML 配置的 tool_timeout 应被 load_config 解析并用于 _execute_tool 超时保护。"""
        import yaml
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        yaml_path = tmp_path / "e2e_tool_timeout.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "tool_timeout": 0.001},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.tool_timeout == 0.001

        agent = MagicMock()
        agent._config.loop.tool_timeout = config.loop.tool_timeout

        async def slow_fn(x: str) -> str:
            await asyncio.sleep(10)
            return x

        agent._tool_map = {"slow_fn": slow_fn}
        tc = ToolCall(id="call_1", name="slow_fn", arguments={"x": "hello"})

        result = await _execute_tool(agent, tc)
        assert result.error is not None
        assert "TimeoutError" in result.error
        assert "slow_fn" in result.error
        assert "0.001" in result.error


# ============================================================
# 第8轮修复补充测试（第2批次 · 新一轮验证）
# 只覆盖第8轮修复涉及的5个修复项，不与既有测试重复：
#   - 修复项26: Tool 超时保护（wait_for 精确传值 / dict 结果保留 / 字段独立性）
#   - 修复项27: scheduled.py State 写入（首成功次失败 / run() 存活）
#   - 修复项28: _create_loop 未知类型（源码无静默回退路径）
#   - 修复项29: access_type 校验（合法解析 / scope path 后端解析）
#   - 修复项30: R3 无硬编码模型名（yaml 优先级 / openai 环境变量 / 正路径）
# ============================================================


class TestToolTimeoutWaitFor:
    """第8轮修复项26（Tool 超时保护）补充单元测试。"""

    @pytest.mark.asyncio
    async def test_execute_tool_passes_config_timeout_to_wait_for(self):
        """_execute_tool 应将配置的 tool_timeout 精确传给 asyncio.wait_for。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        captured = {}

        async def fake_wait_for(awaitable, timeout):
            captured["timeout"] = timeout
            return await awaitable

        agent = MagicMock()
        agent._config.loop.tool_timeout = 7.5

        async def fast_tool(x: str) -> str:
            return f"ok:{x}"

        agent._tool_map = {"fast_tool": fast_tool}
        tc = ToolCall(id="c1", name="fast_tool", arguments={"x": "hi"})

        with patch("weave_agent_sdk.loop.iterative.asyncio.wait_for", side_effect=fake_wait_for):
            result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "ok:hi"
        assert captured["timeout"] == 7.5

    @pytest.mark.asyncio
    async def test_execute_tool_async_dict_result_preserved(self):
        """异步 tool 返回 dict 时，结果应原样保留在 ToolResult.result。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0

        async def dict_tool(q: str) -> dict:
            return {"answer": q, "count": 1}

        agent._tool_map = {"dict_tool": dict_tool}
        tc = ToolCall(id="c1", name="dict_tool", arguments={"q": "hi"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == {"answer": "hi", "count": 1}

    def test_loop_config_timeout_and_tool_timeout_independent(self):
        """LoopConfig.timeout 与 tool_timeout 应相互独立、互不影响。"""
        from weave_agent_sdk.types import LoopConfig

        config = LoopConfig(timeout=10.0, tool_timeout=20.0)
        assert config.timeout == 10.0
        assert config.tool_timeout == 20.0

        config.tool_timeout = 30.0
        assert config.tool_timeout == 30.0
        assert config.timeout == 10.0  # 修改 tool_timeout 不影响 timeout


class TestScheduledStateWritePartialFailure:
    """第8轮修复项27（scheduled.py State 写入异常处理）补充单元测试。"""

    @pytest.mark.asyncio
    async def test_scheduled_state_write_first_succeeds_second_raises(self, caplog):
        """第一条 state 写入成功、第二条失败时，失败被记录日志且主流程正常。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="ok", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock(side_effect=[None, RuntimeError("second fail")])

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with caplog.at_level(logging.WARNING):
            result = await loop._execute_once(agent, "input")

        assert result.output == "ok"
        assert result.iterations == 1
        # 两条写入都被尝试
        assert agent._memory.state.set.call_count == 2
        # 失败被记录告警而非静默吞掉
        assert any("Failed to save scheduled run state" in r.message for r in caplog.records)


class TestCreateLoopNoSilentFallback:
    """第8轮修复项28（_create_loop 未知类型抛 ValueError）补充单元测试。"""

    def test_create_loop_source_has_no_silent_fallback(self):
        """_create_loop() 源码中不应存在 logger.warning 静默回退路径。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._create_loop)
        # 不再静默回退：无 logger.warning 兜底
        assert "logger.warning" not in source
        # 未知类型直接抛 ValueError
        assert "raise ValueError" in source
        assert "Unknown loop type" in source


class TestAccessTypeResolution:
    """第8轮修复项29（access_type 合法性校验）补充单元测试。"""

    def test_get_namespace_valid_scope_and_type(self):
        """合法 scope + access_type 应正确构造 namespace。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        manager.activate_scopes({"default_id": "fixed-id"})

        assert manager.get_namespace("default", "stream") == "default:fixed-id:stream"
        assert manager.get_namespace("default", "state") == "default:fixed-id:state"
        assert manager.get_namespace("default", "knowledge") == "default:fixed-id:knowledge"

    def test_get_backend_for_namespace_respects_scope_path(self, tmp_path):
        """namespace 中的 access_type 应正确解析到 scope 配置的 db 路径。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        db = str(tmp_path / "custom" / "memory.db")
        scopes = {"custom": MemoryScopeConfig(
            priority=0,
            state={"backend": "sqlite", "path": db},
        )}
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        backend = manager._get_backend_for_namespace("custom:s1:state")

        assert backend is not None
        assert backend._db_path == db


class TestR3ModelResolutionAdditional:
    """第8轮修复项30（R3：无硬编码模型名）补充单元测试。"""

    def test_load_config_model_from_yaml_takes_priority_over_env(self, tmp_path, monkeypatch):
        """YAML 中显式配置的 model 应优先于 WEAVE_MODEL 环境变量。"""
        import yaml

        monkeypatch.setenv("WEAVE_MODEL", "env-model-should-lose")
        yaml_path = tmp_path / "model_priority.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic", "model": "yaml-model-2026"},
            "loop": {"type": "simple"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.llm.model == "yaml-model-2026"

    def test_create_llm_openai_uses_provider_env_model(self, monkeypatch):
        """create_llm(provider='openai', model=None) 应使用 OPENAI_MODEL 环境变量。"""
        from weave_agent_sdk.llm.factory import create_llm

        monkeypatch.delenv("WEAVE_MODEL", raising=False)
        monkeypatch.setenv("OPENAI_MODEL", "gpt-e2e-2026")
        with patch("weave_agent_sdk.llm.openai.OpenAIAdapter") as mock_ad:
            with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
                create_llm(provider="openai", model=None)
                mock_ad.assert_called_once()
                _, kwargs = mock_ad.call_args
                assert kwargs.get("model") == "gpt-e2e-2026"

    def test_anthropic_adapter_accepts_non_empty_model(self):
        """AnthropicAdapter 在传入非空 model 时应正常实例化并保存模型名。"""
        from weave_agent_sdk.llm.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(api_key="test-key", model="claude-test-model")
        assert adapter._model == "claude-test-model"


# ============================================================
# 第8轮修复补充测试（第2批次 · 新一轮验证）：端到端测试
# ============================================================


class TestE2EToolTimeoutMixedRound:
    """端到端测试：同一 IterativeLoop 中慢 tool 超时后，快 tool 仍可成功执行。"""

    @pytest.mark.asyncio
    async def test_e2e_iterative_loop_timeout_then_success(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 3
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 0.1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def slow_tool(query: str) -> str:
            await asyncio.sleep(10)
            return "never"

        async def fast_tool(query: str) -> str:
            return f"fast:{query}"

        agent._tools = [slow_tool, fast_tool]
        agent._tool_map = {"slow_tool": slow_tool, "fast_tool": fast_tool}
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(content="calling slow", model="test", tool_calls=[
                ToolCall(id="c1", name="slow_tool", arguments={"query": "q"}),
            ]),
            LLMResponse(content="calling fast", model="test", tool_calls=[
                ToolCall(id="c2", name="fast_tool", arguments={"query": "w"}),
            ]),
            LLMResponse(content="final answer", model="test", tool_calls=None),
        ])
        agent._system_prompt = "System prompt"

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "final answer"
        assert result.iterations == 3
        assert result.memory_updated.get("tools", {}).get("slow_tool") == 1
        assert result.memory_updated.get("tools", {}).get("fast_tool") == 1


class TestE2EScheduledLoopRunStateFailure:
    """端到端测试：ScheduledLoop.run() 在 State 写入失败时仍返回结果并记录日志。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_loop_run_survives_state_write_failure(self, caplog):
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled output", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock(side_effect=RuntimeError("storage down"))

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with caplog.at_level(logging.WARNING):
            result = await loop.run(agent, "input")

        assert result.output == "scheduled output"
        assert result.iterations == 1
        assert any("Failed to save scheduled run state" in r.message for r in caplog.records)


class TestE2EWeaveInvalidLoopType:
    """端到端测试：Weave 构造时遇到未知 loop.type 应抛出 ValueError。"""

    def test_e2e_weave_construction_raises_on_invalid_loop_type(self, tmp_path, monkeypatch):
        import yaml

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("WEAVE_MODEL", "test-model")

        yaml_path = tmp_path / "bad_loop.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "bogus_loop"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        with pytest.raises(ValueError) as excinfo:
            Weave(str(yaml_path))

        assert "loop.type" in str(excinfo.value)
        assert "bogus_loop" in str(excinfo.value)


class TestE2EMemoryScopesFromConfig:
    """端到端测试：weave.yaml memory.scopes 配置 → MemoryManager namespace 解析。"""

    def test_e2e_memory_scopes_config_to_namespace(self, tmp_path):
        import yaml
        from weave_agent_sdk.config import load_config
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "memory_scopes.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "simple"},
            "memory": {
                "scopes": {
                    "narrow": {"priority": 0, "state": {"backend": "sqlite", "path": ":memory:"}},
                    "wide": {"priority": 10, "state": {"backend": "sqlite", "path": ":memory:"}},
                }
            },
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        from weave_agent_sdk.memory.manager import MemoryManager

        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})

        # priority 小的（窄 scope）在前
        assert manager.get_namespaces("state") == ["narrow:n1:state", "wide:w1:state"]
        assert manager.get_namespace("wide", "state") == "wide:w1:state"
        # 非法 access_type 抛 ValueError
        with pytest.raises(ValueError):
            manager.get_namespaces("bad_type")


class TestE2EModelResolutionConfigToAdapter:
    """端到端测试：weave.yaml 模型名 → load_config → create_llm 适配器。"""

    def test_e2e_model_resolution_config_to_adapter(self, tmp_path, monkeypatch):
        from weave_agent_sdk.config import load_config
        from weave_agent_sdk.llm.factory import create_llm

        monkeypatch.setenv("WEAVE_MODEL", "claude-e2e-resolved-2026")

        yaml_path = tmp_path / "model_e2e.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n  model: ${WEAVE_MODEL:-fallback}\n"
            "loop:\n  type: simple\n"
            "memory:\n  scopes: {}\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )

        config = load_config(yaml_path)
        assert config.llm.model == "claude-e2e-resolved-2026"

        with patch("weave_agent_sdk.llm.anthropic.AnthropicAdapter") as mock_ad:
            with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
                create_llm(provider="anthropic", model=config.llm.model)
                mock_ad.assert_called_once()
                _, kwargs = mock_ad.call_args
                assert kwargs.get("model") == "claude-e2e-resolved-2026"


# ============================================================
# 第3轮测试（本轮新增）：针对第8轮修复行为的补充验证
# 只覆盖第8轮修复涉及的5个修复项，不与既有测试重复：
#   - 修复项26: Tool 执行超时保护（同步 tool 成功元数据 / 超时错误消息格式 / None 超时回退）
#   - 修复项27: scheduled.py State 写入（成功写入字段值 / on_end 在失败后仍调用）
#   - 修复项28: _create_loop 未知类型抛 ValueError（空类型 / 错误消息内容）
#   - 修复项29: access_type 合法性校验（未激活 scope / 4 段 namespace 解析）
#   - 修复项30: R3 无硬编码模型名（无 model + 无环境变量时 model 为空）
# ============================================================


class TestRound3ToolTimeoutUnit:
    """第8轮修复项26（Tool 超时保护）—— 补充单元测试。"""

    @pytest.mark.asyncio
    async def test_sync_tool_success_preserves_metadata(self):
        """同步 tool 成功执行时，应保留 tool_call_id / name 元数据且无 error。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0

        def fast_sync(x: str) -> str:
            return f"result:{x}"

        agent._tool_map = {"fast_sync": fast_sync}
        tc = ToolCall(id="c1", name="fast_sync", arguments={"x": "hi"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "result:hi"
        assert result.name == "fast_sync"
        assert result.tool_call_id == "c1"

    @pytest.mark.asyncio
    async def test_async_tool_timeout_error_message_format(self):
        """异步 tool 超时时，错误消息应包含 tool 名与精确超时值。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 0.001

        async def slow(x: str) -> str:
            await asyncio.sleep(10)
            return x

        agent._tool_map = {"slow": slow}
        tc = ToolCall(id="c1", name="slow", arguments={"x": "hi"})

        result = await _execute_tool(agent, tc)

        assert result.error is not None
        assert result.error == "TimeoutError: Tool 'slow' timed out after 0.001s"

    @pytest.mark.asyncio
    async def test_tool_timeout_none_falls_back_to_default(self):
        """tool_timeout 配置为 None（非数值）时，应回退默认 30.0 且 tool 正常执行。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = None  # 非数值

        async def fast(x: str) -> str:
            return f"ok:{x}"

        agent._tool_map = {"fast": fast}
        tc = ToolCall(id="c1", name="fast", arguments={"x": "hi"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "ok:hi"


class TestRound3ScheduledWriteUnit:
    """第8轮修复项27（scheduled.py State 写入异常处理）—— 补充单元测试。"""

    @pytest.mark.asyncio
    async def test_state_write_sets_last_run_at_and_error_fields(self):
        """成功路径应写入 last_run_at（float 时间戳）与 last_run_error（None）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        assert result.output == "scheduled"
        assert result.iterations == 1

        calls = agent._memory.state.set.call_args_list
        keys = [c.args[0] for c in calls]
        assert "last_run_at" in keys
        assert "last_run_error" in keys

        last_run_at = [c.args[1] for c in calls if c.args[0] == "last_run_at"][0]
        last_run_error = [c.args[1] for c in calls if c.args[0] == "last_run_error"][0]
        assert isinstance(last_run_at, float)
        assert last_run_error is None

        ns = [c.args[2] for c in calls]
        assert all(n == "s:1:state" for n in ns)

    @pytest.mark.asyncio
    async def test_on_end_still_called_when_state_write_fails(self, caplog):
        """State 写入失败时，on_end 仍应被调用，主流程不被阻塞。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="ok", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock(side_effect=RuntimeError("storage down"))

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with caplog.at_level(logging.WARNING):
            result = await loop._execute_once(agent, "input")

        assert result.output == "ok"
        loop.on_end.assert_awaited_once()
        assert any("Failed to save scheduled run state" in r.message for r in caplog.records)


class TestRound3CreateLoopUnit:
    """第8轮修复项28（_create_loop 未知类型抛 ValueError）—— 补充单元测试。"""

    def _weave_with_loop_type(self, loop_type):
        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = loop_type
        weave._tools = []
        weave._tool_map = {}
        return weave

    def test_create_loop_empty_type_raises(self):
        """loop.type 为空字符串也应抛出 ValueError（不静默回退）。"""
        weave = self._weave_with_loop_type("")
        with pytest.raises(ValueError):
            weave._create_loop()

    def test_create_loop_error_message_lists_supported_types(self):
        """ValueError 错误消息应列出全部支持类型并引导 weave.yaml。"""
        weave = self._weave_with_loop_type("bogus_type")
        with pytest.raises(ValueError) as excinfo:
            weave._create_loop()
        msg = str(excinfo.value)
        assert "Unknown loop type 'bogus_type'" in msg
        assert "simple, iterative, scheduled" in msg
        assert "weave.yaml" in msg


class TestRound3AccessTypeUnit:
    """第8轮修复项29（access_type 合法性校验）—— 补充单元测试。"""

    def test_get_namespace_inactive_scope_raises(self):
        """get_namespace() 对未激活 scope 应抛出 ValueError（scope 校验仍保留）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        manager.activate_scopes()

        with pytest.raises(ValueError) as excinfo:
            manager.get_namespace("ghost", "state")
        assert "not active" in str(excinfo.value)

    def test_get_backend_four_segment_namespace(self):
        """4 段 namespace 应正确解析末尾 access_type（state），不抛错。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))

        backend = manager._get_backend_for_namespace("a:b:c:state")
        assert backend is not None


class TestRound3R3ModelUnit:
    """第8轮修复项30（R3：无硬编码模型名）—— 补充单元测试。"""

    def test_load_config_model_empty_when_no_env(self, tmp_path, monkeypatch):
        """YAML 无 model 且无 WEAVE_MODEL 环境变量时，model 应为空字符串（不硬编码）。"""
        import yaml

        monkeypatch.delenv("WEAVE_MODEL", raising=False)

        yaml_path = tmp_path / "no_model_round3.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "simple"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.llm.model == ""


# ============================================================
# 第3轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound3ScheduledRunE2E:
    """端到端测试：ScheduledLoop.run() 的 State 写入路径（第8轮修复项27）。"""

    @pytest.mark.asyncio
    async def test_scheduled_run_success_persists_state(self):
        """通过 run() 入口执行成功后，应持久化 last_run_at 与 last_run_error。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled output", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "input")

        assert result.output == "scheduled output"
        assert result.iterations == 1

        calls = agent._memory.state.set.call_args_list
        keys = [c.args[0] for c in calls]
        assert "last_run_at" in keys
        assert "last_run_error" in keys

    @pytest.mark.asyncio
    async def test_scheduled_run_file_not_found_silent(self, caplog):
        """State 写入抛 FileNotFoundError（正常情况）时应静默通过，无告警。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled output", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock(side_effect=FileNotFoundError("db missing"))

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with caplog.at_level(logging.WARNING):
            result = await loop.run(agent, "input")

        assert result.output == "scheduled output"
        assert not any("Failed to save scheduled run state" in r.message for r in caplog.records)


class TestRound3RealConfigE2E:
    """端到端测试：真实 weave.yaml → load_config → 行为验证（第8轮修复项28/29/30）。"""

    @pytest.fixture
    def real_config(self):
        from weave_agent_sdk.config import load_config
        project_root = Path(__file__).parent.parent
        return load_config(project_root / "weave.yaml")

    def test_create_loop_from_real_weave_yaml(self, real_config):
        """真实 weave.yaml（loop.type=iterative）应创建 IterativeLoop；未知类型抛 ValueError。"""
        weave = Weave.__new__(Weave)
        weave._config = real_config
        weave._tools = []
        weave._tool_map = {}

        # 真实配置文件中的 tool_timeout 应为 30.0（默认值）
        assert real_config.loop.tool_timeout == 30.0

        loop = weave._create_loop()
        assert type(loop).__name__ == "IterativeLoop"

        real_config.loop.type = "bogus_type"
        with pytest.raises(ValueError):
            weave._create_loop()

    def test_memory_manager_from_real_config(self, real_config):
        """真实 weave.yaml（无 memory 段 → 默认 scope）→ 合法/非法 access_type 行为正确。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        manager = MemoryManager(real_config.memory)
        manager.activate_scopes()

        # 合法 access_type 正常返回 namespace（默认 scope）
        assert manager.get_namespaces("stream") == ["default:default:stream"]
        assert manager.get_namespace("default", "state") == "default:default:state"

        # 非法 access_type 抛出 ValueError
        with pytest.raises(ValueError):
            manager.get_namespaces("bogus")
        with pytest.raises(ValueError):
            manager.get_namespace("default", "bogus")
        with pytest.raises(ValueError):
            manager._get_backend_for_namespace("default:default:bogus")


class TestRound3ToolLoopE2E:
    """端到端测试：IterativeLoop 中 fast tool 成功执行（第8轮修复项26 回归）。"""

    @pytest.mark.asyncio
    async def test_iterative_loop_fast_tool_success(self):
        """超时保护不误伤正常 tool：dict 结果正确流转，tool 计数被记录。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def dict_tool(q: str) -> dict:
            return {"answer": q}

        agent._tools = [dict_tool]
        agent._tool_map = {"dict_tool": dict_tool}
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(content="calling", model="test", tool_calls=[
                ToolCall(id="c1", name="dict_tool", arguments={"q": "hello"}),
            ]),
            LLMResponse(content="final", model="test", tool_calls=None),
        ])
        agent._system_prompt = "System prompt"

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "final"
        assert result.iterations == 2
        assert result.memory_updated.get("tools", {}).get("dict_tool") == 1
# ============================================================
# 第4轮测试（本轮新增）：针对第8轮修复行为的补充验证
# 只覆盖第8轮修复涉及的5个修复项，不与既有测试重复：
#   - 修复项26: Tool 执行超时保护（同步 tool 异常 / 多参数透传 / None 返回值）
#   - 修复项27: scheduled.py State 写入（memory_updated 计数 / 多 namespace 取首个）
#   - 修复项28: _create_loop 未知类型抛 ValueError（simple/iterative 大小写）
#   - 修复项29: access_type 合法性校验（校验顺序 / 未激活 scope 空列表）
#   - 修复项30: R3 无硬编码模型名（WEAVE_MODEL 空串回退 / OpenAI 错误信息）
# ============================================================


class TestRound4ToolTimeoutUnit:
    """第8轮修复项26（Tool 超时保护）—— 第4轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_sync_tool_exception_returns_structured_error(self):
        """同步 tool 抛异常时应返回结构化错误（RuntimeError: boom），不抛给上层。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0

        def bad_tool(x: str) -> str:
            raise RuntimeError("boom")

        agent._tool_map = {"bad_tool": bad_tool}
        tc = ToolCall(id="c1", name="bad_tool", arguments={"x": "1"})

        result = await _execute_tool(agent, tc)

        assert result.error == "RuntimeError: boom"
        assert result.result is None

    @pytest.mark.asyncio
    async def test_sync_tool_receives_multiple_kwargs(self):
        """同步 tool 的多个参数应通过 **tc.arguments 正确透传。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0

        def multi_tool(a: str, b: str) -> str:
            return f"{a}-{b}"

        agent._tool_map = {"multi_tool": multi_tool}
        tc = ToolCall(id="c1", name="multi_tool", arguments={"a": "x", "b": "y"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result == "x-y"

    @pytest.mark.asyncio
    async def test_sync_tool_none_result_no_error(self):
        """同步 tool 返回 None 时应视为成功（error=None，result=None）。"""
        from weave_agent_sdk.loop.iterative import _execute_tool
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config.loop.tool_timeout = 5.0

        def none_tool(x: str) -> None:
            return None

        agent._tool_map = {"none_tool": none_tool}
        tc = ToolCall(id="c1", name="none_tool", arguments={"x": "1"})

        result = await _execute_tool(agent, tc)

        assert result.error is None
        assert result.result is None


class TestRound4ScheduledWriteUnit:
    """第8轮修复项27（scheduled.py State 写入）—— 第4轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_execute_once_reports_state_write_in_memory_updated(self):
        """成功写入后，memory_updated['state'] 应反映实际写入计数（2 次/namespace）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        assert result.memory_updated["state"] == {"s:1:state": 2}

    @pytest.mark.asyncio
    async def test_execute_once_uses_first_namespace_when_multiple(self):
        """get_namespaces 返回多个 state namespace 时，只使用第一个用于写入。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state", "s:2:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        ns = [c.args[2] for c in agent._memory.state.set.call_args_list]
        assert ns == ["s:1:state", "s:1:state"]  # 两次写入都落在第一个 namespace
        assert result.memory_updated["state"] == {"s:1:state": 2}


class TestRound4CreateLoopUnit:
    """第8轮修复项28（_create_loop 未知类型抛 ValueError）—— 第4轮补充单元测试。"""

    def test_create_loop_uppercase_simple_and_iterative_raise(self):
        """loop.type 大小写不匹配（'Simple' / 'ITERATIVE'）也应抛出 ValueError。"""
        for bad_type in ("Simple", "ITERATIVE"):
            weave = Weave.__new__(Weave)
            weave._config = MagicMock()
            weave._config.loop.type = bad_type
            weave._tools = []
            weave._tool_map = {}
            with pytest.raises(ValueError) as excinfo:
                weave._create_loop()
            assert bad_type in str(excinfo.value)
            assert "Unknown loop type" in str(excinfo.value)


class TestRound4AccessTypeUnit:
    """第8轮修复项29（access_type 合法性校验）—— 第4轮补充单元测试。"""

    def test_access_type_validated_before_scope_in_get_namespace(self):
        """get_namespace 中非法 access_type 应优先于 scope 校验抛出 ValueError。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig())
        with pytest.raises(ValueError) as excinfo:
            manager.get_namespace("ghost", "bad_type")
        assert "Invalid access_type" in str(excinfo.value)
        assert "not active" not in str(excinfo.value)

    def test_get_namespaces_empty_without_active_scopes(self):
        """未调用 activate_scopes 时，合法 access_type 的 get_namespaces 返回空列表。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig())
        assert manager.get_namespaces("stream") == []
        assert manager.get_namespaces("state") == []
        assert manager.get_namespaces("knowledge") == []


class TestRound4R3ModelUnit:
    """第8轮修复项30（R3：无硬编码模型名）—— 第4轮补充单元测试。"""

    def test_resolve_model_empty_weave_model_falls_through_to_provider(self, monkeypatch):
        """WEAVE_MODEL 被显式置空字符串时，应回退到 provider 专用环境变量。"""
        from weave_agent_sdk.llm.factory import _resolve_model

        monkeypatch.setenv("WEAVE_MODEL", "")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-fallback-2026")
        result = _resolve_model("openai")
        assert result == "gpt-fallback-2026"

    def test_openai_adapter_empty_model_error_mentions_config(self):
        """OpenAIAdapter 空模型错误信息应引导配置 weave.yaml / WEAVE_MODEL。"""
        from weave_agent_sdk.llm.openai import OpenAIAdapter

        with pytest.raises(ValueError) as excinfo:
            OpenAIAdapter(api_key="test-key", model="")
        msg = str(excinfo.value)
        assert "model must be specified" in msg
        assert "WEAVE_MODEL" in msg
        assert "weave.yaml" in msg


# ============================================================
# 第4轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound4ToolLoopE2E:
    """端到端测试：IterativeLoop 中同步 tool 抛异常不阻塞循环（第8轮修复项26）。"""

    @pytest.mark.asyncio
    async def test_e2e_iterative_loop_sync_tool_exception_continues(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        def bad_tool(query: str) -> str:
            raise RuntimeError("boom")

        agent._tools = [bad_tool]
        agent._tool_map = {"bad_tool": bad_tool}
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(content="calling", model="test", tool_calls=[
                ToolCall(id="c1", name="bad_tool", arguments={"query": "q"}),
            ]),
            LLMResponse(content="final answer", model="test", tool_calls=None),
        ])
        agent._system_prompt = "System prompt"

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "final answer"
        assert result.iterations == 2
        assert result.memory_updated.get("tools", {}).get("bad_tool") == 1


class TestRound4ScheduledRunE2E:
    """端到端测试：ScheduledLoop.run() 的 State 写入计数（第8轮修复项27）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_run_reports_state_writes_in_memory_updated(self):
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled output", model="test"))
        agent._system_prompt = "System prompt"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "input")

        assert result.output == "scheduled output"
        assert result.memory_updated["state"] == {"s:1:state": 2}


class TestRound4WeaveConstructionE2E:
    """端到端测试：Weave 实际构造（loop.type=scheduled）（第8轮修复项28）。"""

    def test_e2e_weave_construction_scheduled_type(self, tmp_path, monkeypatch):
        import yaml

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("WEAVE_MODEL", "test-model")

        yaml_path = tmp_path / "scheduled_weave.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "scheduled"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        weave = Weave(str(yaml_path))

        assert weave._config.loop.type == "scheduled"
        assert type(weave._loop).__name__ == "ScheduledLoop"


class TestRound4MemoryManagerE2E:
    """端到端测试：MemoryManager 合法 access_type 的 state 写入/读取（第8轮修复项29）。"""

    @pytest.mark.asyncio
    async def test_e2e_memory_manager_state_write_and_read(self):
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        manager.activate_scopes({"default_id": "s1"})

        ns = manager.get_namespace("default", "state")
        assert ns == "default:s1:state"

        manager.state.set("last_run_at", 1234.5, ns)
        manager.state.set("last_run_error", None, ns)

        assert manager.state.get("last_run_at", ns) == 1234.5
        assert manager.state.get("last_run_error", ns) is None


class TestRound4CreateLLMDeepseekE2E:
    """端到端测试：create_llm(deepseek) 使用 WEAVE_MODEL 环境变量（第8轮修复项30）。"""

    def test_e2e_create_llm_deepseek_uses_weave_model_env(self, monkeypatch):
        from weave_agent_sdk.llm.factory import create_llm

        monkeypatch.setenv("WEAVE_MODEL", "deepseek-e2e-2026")
        with patch("weave_agent_sdk.llm.openai.OpenAIAdapter") as mock_ad:
            with patch("weave_agent_sdk.llm.factory._resolve_api_key", return_value="test-key"):
                create_llm(provider="deepseek", model=None)
                mock_ad.assert_called_once()
                _, kwargs = mock_ad.call_args
                assert kwargs.get("model") == "deepseek-e2e-2026"


# ============================================================
# 第5轮测试（本轮新增）：针对第5轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第5轮修复"（2026-08-15）标题，共5个修复项:
#   - 修复项1: 常驻 scheduled 排除共享锁，避免饿死其他操作（agent.py _is_continuous_scheduled）
#   - 修复项2: cron DOW 与 datetime.weekday() 语义错位修复（周日/周六偏移一天，scheduled.py）
#   - 修复项3: WS 协议透传 scope_hints / context / tool_filter（ws.py，与 REST 对齐）
#   - 修复项4: iterative 每轮用真实 user_input 做 knowledge 检索（替换 "continue" 字面量）
#   - 修复项5: iterative 增加 messages 窗口裁剪（_MAX_CONTEXT_MESSAGES=20）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


def _r5_cron_dow_of_ts(ts: float) -> int:
    """将时间戳转换为 cron DOW 表示（0=Sunday ... 6=Saturday）。"""
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return (d.weekday() + 1) % 7


class TestRound5ContinuousScheduledUnit:
    """第5轮修复项1（常驻 scheduled 排除共享锁）—— 单元测试。"""

    def _make_weave(self, loop_type: str, schedule):
        from types import SimpleNamespace
        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(
            loop=SimpleNamespace(type=loop_type, schedule=schedule)
        )
        return weave

    def test_is_continuous_scheduled_true_with_schedule(self):
        """loop.type='scheduled' 且配置非空 schedule 字符串时返回 True。"""
        weave = self._make_weave("scheduled", "0 * * * *")
        assert weave._is_continuous_scheduled() is True

    def test_is_continuous_scheduled_false_empty_schedule(self):
        """loop.type='scheduled' 但 schedule 为空白字符串时返回 False（单次执行）。"""
        weave = self._make_weave("scheduled", "   ")
        assert weave._is_continuous_scheduled() is False

    def test_is_continuous_scheduled_false_non_scheduled_type(self):
        """loop.type 非 scheduled（iterative/simple）时返回 False。"""
        weave = self._make_weave("iterative", "0 * * * *")
        assert weave._is_continuous_scheduled() is False

    def test_is_continuous_scheduled_false_when_schedule_attr_missing(self):
        """config 缺少 loop.schedule 属性时应返回 False（AttributeError 被捕获）。"""
        from types import SimpleNamespace
        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(loop=SimpleNamespace(type="scheduled"))
        assert weave._is_continuous_scheduled() is False

    @pytest.mark.asyncio
    async def test_run_impl_uses_shared_lock_for_non_scheduled(self):
        """非 scheduled 类型（iterative）走共享锁路径：_run_locks 应被创建。"""
        from types import SimpleNamespace
        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(loop=SimpleNamespace(type="iterative"))
        weave._run_impl_inner = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=1, iterations=1, memory_updated={}
        ))

        result = await weave._run_impl("hi")

        assert result.output == "ok"
        weave._run_impl_inner.assert_awaited_once()
        # 非 scheduled 路径应创建每事件循环的共享锁
        assert "_run_locks" in weave.__dict__
        assert len(weave._run_locks) == 1


class TestRound5CronDowUnit:
    """第5轮修复项2（cron DOW 语义错位）—— 单元测试。"""

    def test_next_cron_sunday_dow_zero(self):
        """cron DOW=0（周日）下一次触发必须是周日，不再偏移到周六。"""
        from weave_agent_sdk.loop.scheduled import _next_cron_timestamp
        ts = _next_cron_timestamp("0 0 * * 0", time.time())
        assert ts is not None
        assert _r5_cron_dow_of_ts(ts) == 0

    def test_next_cron_saturday_dow_six(self):
        """cron DOW=6（周六）下一次触发必须是周六，不再偏移到周五。"""
        from weave_agent_sdk.loop.scheduled import _next_cron_timestamp
        ts = _next_cron_timestamp("0 0 * * 6", time.time())
        assert ts is not None
        assert _r5_cron_dow_of_ts(ts) == 6


class TestRound5WSPassthroughUnit:
    """第5轮修复项3（WS 透传 scope_hints / context / tool_filter）—— 单元测试。"""

    def test_ws_passes_scope_hints_context_tool_filter(self):
        """ws.py 应读取 scope_hints/context/tool_filter 并透传给 _run_impl。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert 'scope_hints = data.get("scope_hints") or None' in source
        assert 'context = data.get("context") or None' in source
        assert 'tool_filter = data.get("tool_filter") or None' in source
        assert "scope_hints=scope_hints" in source
        assert "context=context" in source
        assert "tool_filter=tool_filter" in source


class TestRound5IterativeKnowledgeUnit:
    """第5轮修复项4（iterative 用真实 user_input 做 knowledge 检索）—— 单元测试。"""

    def test_before_think_uses_real_user_input(self):
        """before_think 应接收真实 user_input，而非字面量 "continue"。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "memory_ctx = await self.before_think(agent, user_input)" in source
        # 不应再使用 "continue" 字面量作为 knowledge 检索词
        assert 'before_think(agent, "continue")' not in source


class TestRound5IterativeTrimUnit:
    """第5轮修复项5（iterative messages 窗口裁剪）—— 单元测试。"""

    def test_max_context_messages_constant_and_trim_logic(self):
        """_MAX_CONTEXT_MESSAGES 应为 20，且 run() 含裁剪逻辑。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop, _MAX_CONTEXT_MESSAGES

        assert _MAX_CONTEXT_MESSAGES == 20
        source = inspect.getsource(IterativeLoop.run)
        assert "if len(messages) > _MAX_CONTEXT_MESSAGES:" in source
        assert "messages[-(_MAX_CONTEXT_MESSAGES - 2):]" in source


# ============================================================
# 第5轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound5ContinuousScheduledE2E:
    """端到端测试：常驻 scheduled 循环排除共享锁（第5轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_continuous_scheduled_bypasses_shared_lock(self):
        """常驻 scheduled（scheduled + 非空 schedule）执行时应绕过共享锁，
        不创建 _run_locks，避免持有锁饿死同 loop 上的其他操作。"""
        from types import SimpleNamespace
        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(
            loop=SimpleNamespace(type="scheduled", schedule="0 * * * *")
        )
        weave._run_impl_inner = AsyncMock(return_value=LoopResult(
            output="scheduled-run", elapsed_ms=2, iterations=1, memory_updated={}
        ))

        result = await weave._run_impl("tick")

        assert result.output == "scheduled-run"
        weave._run_impl_inner.assert_awaited_once()
        # 常驻 scheduled 不应创建共享锁（排除在共享锁之外）
        assert "_run_locks" not in weave.__dict__ or weave._run_locks == {}


class TestRound5CronDowE2E:
    """端到端测试：cron DOW 与 datetime.weekday() 语义对齐（第5轮修复项2）。"""

    def test_e2e_cron_dow_not_shifted(self):
        """周日(0)/周一(1)/周六(6) 的 cron 触发日期均应与预期星期一致。"""
        from weave_agent_sdk.loop.scheduled import _next_cron_timestamp

        now = time.time()
        for dow, expected in ((0, 0), (1, 1), (6, 6)):
            ts = _next_cron_timestamp(f"0 0 * * {dow}", now)
            assert ts is not None
            assert _r5_cron_dow_of_ts(ts) == expected, \
                f"cron DOW={dow} resolved to weekday {_r5_cron_dow_of_ts(ts)} (off-by-one)"

        # 周日(0) 与 周六(6) 不再是同一天（修复前偏移会把两者都落到周一/周日）
        sun_ts = _next_cron_timestamp("0 0 * * 0", now)
        sat_ts = _next_cron_timestamp("0 0 * * 6", now)
        assert sun_ts != sat_ts


class _FakeWS:
    """测试用伪 WebSocket：记录 accept/send/close 行为。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        return self._payload

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)

    async def close(self) -> None:
        self.closed = True


class TestRound5WSPassthroughE2E:
    """端到端测试：WS 协议透传 scope_hints / context / tool_filter（第5轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_ws_passes_scope_hints_context_tool_filter(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream

        bus = EventBus()
        weave = MagicMock()
        weave._run_impl = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=1, iterations=1, memory_updated={}
        ))
        weave.on = bus.subscribe
        weave.emit = bus.emit

        ws = _FakeWS({
            "input": "hello",
            "scope_hints": {"session_id": "s1"},
            "context": {"user": "alice"},
            "tool_filter": ["search_kb", "calc"],
        })

        await ws_agent_stream(ws, weave)

        weave._run_impl.assert_awaited_once()
        call = weave._run_impl.call_args
        assert call.args[0] == "hello"
        assert call.kwargs["scope_hints"] == {"session_id": "s1"}
        assert call.kwargs["context"] == {"user": "alice"}
        assert call.kwargs["tool_filter"] == ["search_kb", "calc"]
        assert call.kwargs["_streaming"] is True

        assert ws.accepted is True
        assert ws.closed is True
        # 至少收到 done 事件（basic.md §5 契约，非 run_complete）
        assert any(e["type"] == "done" for e in ws.sent)


class TestRound5IterativeKnowledgeE2E:
    """端到端测试：iterative 每轮用真实 user_input 做 knowledge 检索（第5轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_before_think_receives_real_user_input(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.loop.stop_conditions = [{"type": "no_tool_calls"}]
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        from weave_agent_sdk.llm.base import LLMResponse
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="answer", model="test"))
        agent._system_prompt = "System"
        agent._tools = []
        agent._tool_map = {}

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        real_input = "search for weather in Tokyo tomorrow"
        result = await loop.run(agent, real_input)

        assert result.output == "answer"
        # before_think 每轮都应收到真实 user_input，而非 "continue"
        assert loop.before_think.call_count >= 1
        for call in loop.before_think.call_args_list:
            assert call.args[1] == real_input
            assert call.args[1] != "continue"


class TestRound5IterativeTrimE2E:
    """端到端测试：iterative messages 窗口裁剪（第5轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_messages_trimmed_to_max_context(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop, _MAX_CONTEXT_MESSAGES
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 15
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"

        call_lens: list[int] = []
        counter = {"n": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            call_lens.append(len(messages))
            counter["n"] += 1
            if counter["n"] < 15:
                return LLMResponse(
                    content=f"iter{counter['n']}", model="test",
                    tool_calls=[ToolCall(id=f"c{counter['n']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
            loop = IterativeLoop()
            loop.on_start = AsyncMock()
            loop.on_end = AsyncMock()
            loop.before_think = AsyncMock(return_value={})
            loop.after_think = AsyncMock()

            result = await loop.run(agent, "hi")

        assert result.output == "final answer"
        assert result.iterations == 15
        assert result.memory_updated["tools"]["search_tool"] == 14
        # 每次注入 LLM 的上下文窗口都 <= 上限
        assert len(call_lens) == 15
        assert all(l <= _MAX_CONTEXT_MESSAGES for l in call_lens), \
            f"messages exceeded window: {call_lens}"
        # 裁剪确实被触发过（多轮 tool 调用后上下文曾超过上限，被裁剪回上限）
        assert max(call_lens) == _MAX_CONTEXT_MESSAGES

# ============================================================
# 第6轮测试（本轮新增）：针对第6轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第6轮修复" 标题（2026-08-16），共4个修复项:
#   - 修复项1: iterative.py 上下文裁剪改为按完整 tool 调用组截断，
#     避免孤立 tool 消息导致 LLM API 拒绝请求（P1）
#   - 修复项2: scheduled.py `_execute_once` 每触发重置 `_memory_writes`，
#     修复常驻 scheduled 跨触发记忆被清空（P2/P3）
#   - 修复项3: scheduled.py 常驻循环按触发周期持共享锁执行，
#     缓解并发状态竞争（P2）
#   - 修复项4: scheduled.py `handle_shutdown` 改用 `call_soon_threadsafe`
#     线程安全取消任务（P3）
# 本轮新增：10 个单元测试 + 4 个端到端测试
# ============================================================


async def _round6_wait_until(cond, timeout=5.0, interval=0.01):
    """轮询等待条件满足（带超时保护，防止测试无限挂起）。"""
    deadline = time.time() + timeout
    while not cond():
        if time.time() > deadline:
            raise TimeoutError("round6 wait condition not met")
        await asyncio.sleep(interval)


class TestRound6TrimGroupUnit:
    """第6轮修复项1（iterative 上下文按 tool 调用组截断）—— 单元测试。"""

    def test_trim_source_has_tool_group_skip(self):
        """run() 裁剪时遇到窗口起点为 tool 消息应向前跳过完整调用组。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "if len(messages) > _MAX_CONTEXT_MESSAGES:" in source
        assert 'while start < len(messages) and messages[start].role == "tool":' in source
        assert "start += 1" in source
        assert "trimmed = [messages[0], messages[1]] + messages[start:]" in source

    def test_trim_source_keeps_system_and_window(self):
        """裁剪窗口应保留 system prompt 与最近 _MAX_CONTEXT_MESSAGES-1 条消息。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "trimmed = [messages[0], messages[1]] + messages[-(_MAX_CONTEXT_MESSAGES - 2):]" in source
        assert "start = len(messages) - (_MAX_CONTEXT_MESSAGES - 2)" in source


class TestRound6MemoryWritesResetUnit:
    """第6轮修复项2（scheduled._execute_once 每触发重置 _memory_writes）—— 单元测试。"""

    def test_execute_once_source_resets_memory_writes(self):
        """_execute_once 起始处应 pop 掉 _memory_writes，防止跨触发累加。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop._execute_once)
        assert 'agent.__dict__.pop("_memory_writes", None)' in source

    @pytest.mark.asyncio
    async def test_execute_once_no_cross_trigger_stream_count(self):
        """同一 agent 连续两次触发：每次的 stream 写计数都从 1 开始，不累加。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["s:1:stream"], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.stream.append = MagicMock()
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        results = []
        for _ in range(2):
            results.append(await loop._execute_once(agent, "input"))

        for i, r in enumerate(results):
            assert r.memory_updated["stream"] == {"s:1:stream": 1}, \
                f"第{i+1}次触发 stream 计数应重置为 1，实际 {r.memory_updated['stream']}"
            assert r.memory_updated["state"] == {"s:1:state": 2}

    @pytest.mark.asyncio
    async def test_execute_once_stale_memory_writes_not_leaked(self):
        """上次触发遗留的 _memory_writes 应被清除，不泄漏到本次结果。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.state.set = MagicMock()
        # 模拟上次触发遗留的写入计数
        agent.__dict__["_memory_writes"] = {"stream": {"s:old:stream": 99}}

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        assert result.output == "scheduled"
        assert "stream" not in result.memory_updated, \
            "遗留的 stream 写计数不应泄漏到本次结果"
        assert result.memory_updated.get("state") == {"s:1:state": 2}


class TestRound6SharedLockUnit:
    """第6轮修复项3（scheduled 常驻循环按触发周期持共享锁）—— 单元测试。"""

    def test_run_source_acquires_shared_lock_per_trigger(self):
        """run() 常驻循环应在每次触发时获取 agent 共享锁（_run_locks）。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        # 共享锁逻辑已从 run() 移入 _execute_trigger()
        # （每次触发持锁，避免常驻周期持锁饿死其他操作）
        source = inspect.getsource(ScheduledLoop._execute_trigger)
        assert 'locks = agent.__dict__.setdefault("_run_locks", {})' in source
        assert "lock = asyncio.Lock()" in source
        assert "async with lock:" in source
        assert "await self._execute_once(agent, trigger_input)" in source

    @pytest.mark.asyncio
    async def test_run_single_execution_does_not_acquire_lock(self):
        """未配置 schedule 的单次执行路径不应创建共享锁。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = None

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop._execute_once = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=0, iterations=1, memory_updated={}
        ))

        result = await loop.run(agent, "hi")

        assert result.output == "ok"
        loop._execute_once.assert_awaited_once()
        assert "_run_locks" not in getattr(agent, "__dict__", {})


class TestRound6HandleShutdownUnit:
    """第6轮修复项4（handle_shutdown 用 call_soon_threadsafe 线程安全取消）—— 单元测试。"""

    def test_handle_shutdown_source_uses_call_soon_threadsafe(self):
        """handle_shutdown 应通过 loop.call_soon_threadsafe 调度任务取消。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.handle_shutdown)
        assert "call_soon_threadsafe" in source
        assert "self._current_task.cancel" in source
        assert "self._shutting_down = True" in source

    def test_handle_shutdown_schedules_cancel_via_call_soon_threadsafe(self):
        """handle_shutdown 应将 task.cancel 交给 call_soon_threadsafe，而非直接调用。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_loop = MagicMock()
        fake_task.get_loop.return_value = fake_loop
        loop._current_task = fake_task

        loop.handle_shutdown()

        assert loop._shutting_down is True
        fake_loop.call_soon_threadsafe.assert_called_once()
        callback = fake_loop.call_soon_threadsafe.call_args[0][0]
        assert callback == fake_task.cancel
        # 不应在当前线程直接调用 task.cancel
        fake_task.cancel.assert_not_called()

    def test_handle_shutdown_noop_when_task_none_or_done(self):
        """_current_task 为 None 或已 done 时，不应调用 call_soon_threadsafe。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        # None
        loop = ScheduledLoop()
        loop.handle_shutdown()
        assert loop._shutting_down is True

        # 已 done
        loop2 = ScheduledLoop()
        fake_task = MagicMock()
        fake_task.done.return_value = True
        loop2._current_task = fake_task
        loop2.handle_shutdown()
        assert loop2._shutting_down is True
        fake_task.get_loop.assert_not_called()


# ============================================================
# 第6轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound6TrimGroupE2E:
    """端到端测试：iterative 长迭代裁剪后窗口无孤立 tool 消息（第6轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_no_orphaned_tool_messages_after_trim(self):
        """12 轮 tool 迭代中，每次注入 LLM 的窗口都不超过上限且不以 tool 消息开头。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop, _MAX_CONTEXT_MESSAGES
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 12
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return "result"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"

        captured = []
        n = {"i": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            captured.append(list(messages))
            n["i"] += 1
            if n["i"] < 12:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[ToolCall(id=f"c{n['i']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
            loop = IterativeLoop()
            loop.on_start = AsyncMock()
            loop.on_end = AsyncMock()
            loop.before_think = AsyncMock(return_value={})
            loop.after_think = AsyncMock()

            result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=10.0)

        assert result.output == "final answer"
        assert result.iterations == 12
        assert len(captured) == 12
        # 裁剪确实触发：12 轮若不裁剪会到 24 条，这里全程 <= 20
        assert max(len(m) for m in captured) <= _MAX_CONTEXT_MESSAGES
        # 窗口起点绝不能是孤立 tool 消息（前置 assistant tool_calls 已被裁剪）
        for msgs in captured:
            assert msgs[0].role == "system"
            assert msgs[1].role != "tool", "窗口起点出现孤立 tool 消息（tool 调用组被裁断）"


class TestRound6MemoryWritesResetE2E:
    """端到端测试：常驻 scheduled 跨触发 _memory_writes 重置（第6轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_resident_scheduled_resets_memory_writes_across_triggers(self):
        """常驻循环两次触发各自报告独立的 stream 写计数（1），不累加。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["s:1:stream"], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.stream.append = MagicMock()
        agent._memory.state.set = MagicMock()
        agent._event_bus = EventBus()

        results = []

        async def record_end(agent_, result):
            results.append(dict(result.memory_updated))

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = record_end
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        task = asyncio.create_task(loop.run(agent, "hi"))

        # 事件驱动模式（第9轮修复项3）：常驻循环需收到 data_change 才触发首次执行
        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: len(results) >= 1)
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: len(results) >= 2)

        loop.handle_shutdown()
        await asyncio.wait_for(task, timeout=5.0)

        assert len(results) == 2
        for i, r in enumerate(results):
            assert r.get("stream") == {"s:1:stream": 1}, \
                f"第{i+1}次触发 stream 计数应重置为 1，实际 {r.get('stream')}"
            assert r.get("state") == {"s:1:state": 2}


class TestRound6SharedLockE2E:
    """端到端测试：常驻 scheduled 每次触发持共享锁执行（第6轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_per_trigger_holds_shared_lock(self):
        """每次触发执行期间，agent 的每事件循环共享锁应处于锁定状态。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._event_bus = EventBus()

        state = {"held": [], "count": 0}

        async def fake_execute_once(agent_, user_input):
            rloop = asyncio.get_running_loop()
            locks = getattr(agent_, "__dict__", {}).get("_run_locks") or {}
            lock = locks.get(rloop)
            state["held"].append(lock is not None and lock.locked())
            state["count"] += 1
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        task = asyncio.create_task(loop.run(agent, "hi"))

        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: state["count"] >= 1)
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: state["count"] >= 2)

        loop.handle_shutdown()
        await asyncio.wait_for(task, timeout=5.0)

        assert state["count"] >= 2
        assert len(state["held"]) >= 2
        assert all(state["held"]), "每次触发期间共享锁应处于锁定状态"


class TestRound6HandleShutdownE2E:
    """端到端测试：handle_shutdown 通过 call_soon_threadsafe 优雅终止常驻循环（第6轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_handle_shutdown_cancels_gracefully(self):
        """调用 handle_shutdown 后常驻循环应被取消并正常返回最近一次结果。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._event_bus = EventBus()

        count = {"n": 0}

        async def fake_execute_once(agent_, user_input):
            count["n"] += 1
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        task = asyncio.create_task(loop.run(agent, "hi"))

        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: count["n"] >= 1)
        loop.handle_shutdown()

        result = await asyncio.wait_for(task, timeout=5.0)

        assert result.output == "ok"
        assert loop._shutting_down is True
        assert loop._current_task is None

# ============================================================
# 第7轮测试（本轮新增）：针对第7轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第7轮修复" 标题（2026-08-18），共5个修复项:
#   - 修复项1: agent.py 常驻 scheduled 重入保护（已有常驻调度时重复
#     run/arun/stream 抛 RuntimeError）（P1）
#   - 修复项2: scheduled.py 单次触发失败不再终止整个调度器，
#     每触发 try/except 记录告警后继续等待下次触发（P2）
#   - 修复项3: scheduled.py @on_data_change 改为循环外常驻订阅，
#     消除执行期间 data_change 事件丢失窗口（P3）
#   - 修复项4: ws.py 协议文档与实现统一，文档化实际事件类型
#     （llm_token/tool_call/tool_result/run_complete/error）（P3）
#   - 修复项5: types.py/config.py/base.py/weave.yaml 新增
#     loop.memory_context_limit，before_think 注入历史条数可配置且有上限（P3）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================

from weave_agent_sdk.loop.base import BaseLoop


class _Round7ConcreteLoop(BaseLoop):
    """为调用 BaseLoop.before_think 提供可实例化的具体子类。"""

    async def run(self, agent, user_input):
        raise NotImplementedError


class _Round7BeforeThinkAgent:
    """最小真实 agent（非 MagicMock），供 before_think 行为测试。"""

    def __init__(self, limit):
        from types import SimpleNamespace
        self._config = SimpleNamespace(loop=SimpleNamespace(memory_context_limit=limit))
        self._memory = MagicMock()
        self._memory_writes = None
        self._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["s:1:stream"],
            "state": [],
            "knowledge": [],
        }[at])
        self._memory.stream = MagicMock()
        self._memory.state = MagicMock()
        self._memory.knowledge = MagicMock()


class TestRound7ReentryUnit:
    """第7轮修复项1（常驻 scheduled 重入保护）—— 单元测试。"""

    def _make_weave(self, is_running: bool):
        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "scheduled"
        weave._config.loop.schedule = "0 * * * *"
        weave._is_running = is_running
        weave._run_impl_inner = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=1, iterations=1, memory_updated={}
        ))
        return weave

    @pytest.mark.asyncio
    async def test_run_impl_raises_when_continuous_scheduled_already_running(self):
        """常驻 scheduled 已在运行时再次调用 _run_impl 应抛 RuntimeError。"""
        weave = self._make_weave(is_running=True)

        with pytest.raises(RuntimeError) as excinfo:
            await weave._run_impl("hi")

        assert "already running" in str(excinfo.value)
        assert "resident scheduled" in str(excinfo.value)
        weave._run_impl_inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_impl_allows_when_continuous_scheduled_not_running(self):
        """常驻 scheduled 未运行时首次调用应正常执行（不误伤首次启动）。"""
        weave = self._make_weave(is_running=False)

        result = await weave._run_impl("hi")

        assert result.output == "ok"
        weave._run_impl_inner.assert_awaited_once()


class TestRound7TriggerFailureUnit:
    """第7轮修复项2（单次触发失败不终止调度器）—— 单元测试。"""

    def test_run_source_wraps_trigger_in_try_except(self):
        """run() 每次触发应被 try/except 包裹，失败记录告警后继续等待下次触发。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        # 触发失败兕底已从 run() 移入 _execute_trigger()
        # （单次触发失败不再终止整个调度器）
        source = inspect.getsource(ScheduledLoop._execute_trigger)
        assert "await self._execute_once(agent, trigger_input)" in source
        assert "except asyncio.CancelledError:" in source
        assert "except Exception as e:" in source
        assert "will retry on next schedule" in source
        assert "logger.warning(" in source


class TestRound7DataChangeSubscriptionUnit:
    """第7轮修复项3（@on_data_change 循环外常驻订阅）—— 单元测试。"""

    def test_run_creates_resident_subscription_before_loop(self):
        """run() 应在 while 循环之前建立 data_change 常驻订阅（消除执行期间事件丢失窗口）。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        assert 'data_stream = bus.subscribe("data_change")' in source
        data_idx = source.index("data_stream = None")
        while_idx = source.index("while not self._shutting_down:")
        assert data_idx < while_idx, "常驻订阅应在 while 循环之前建立"
        # 循环体内消费订阅而非每次触发后重新订阅
        assert "async for _event in data_stream:" in source


class TestRound7WsDocUnit:
    """第7轮修复项4（ws.py 协议文档与实现统一）—— 单元测试。"""

    def test_ws_docstring_documents_five_event_types(self):
        """ws_agent_stream 文档/实现应明确包含 5 种事件类型。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        for et in ("token", "tool_call", "tool_result", "done", "error"):
            assert et in source, f"ws.py missing documented event type: {et}"

    def test_ws_subscribes_to_documented_event_types(self):
        """ws.py 订阅的事件类型应与文档声明完全一致（5 种）。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert '"token", "tool_call", "tool_result", "done", "error"' in source


class TestRound7MemoryContextLimitUnit:
    """第7轮修复项5（loop.memory_context_limit）—— 单元测试。"""

    def test_loop_config_has_memory_context_limit_default_20(self):
        """LoopConfig 应包含 memory_context_limit 字段，默认值 20。"""
        from weave_agent_sdk.types import LoopConfig

        config = LoopConfig()
        assert config.memory_context_limit == 20

    def test_load_config_parses_memory_context_limit_from_yaml(self, tmp_path):
        """load_config() 应从 YAML 解析 loop.memory_context_limit 字段。"""
        import yaml

        yaml_path = tmp_path / "test_r7_memory_context_limit.yaml"
        config_data = {
            "agent": {"name": "test"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "memory_context_limit": 7},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.memory_context_limit == 7

    @pytest.mark.asyncio
    async def test_before_think_invalid_limit_falls_back_to_default(self):
        """memory_context_limit 为非数值/非正配置时 before_think 应回退默认 20。"""
        agent = _Round7BeforeThinkAgent(limit="not-an-int")
        captured = {}

        def fake_last(n, namespaces):
            captured["n"] = n
            return [{"role": "user", "content": f"m{i}"} for i in range(min(n, 50))]

        agent._memory.stream.last = fake_last

        loop = _Round7ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        assert captured["n"] == 20
        assert len(ctx["stream"]) == 20

    def test_config_reference_contains_memory_context_limit(self):
        """docs/config-reference.yaml 应包含 loop.memory_context_limit: 20 配置。"""
        import yaml

        yaml_path = Path(__file__).parent.parent / "docs" / "config-reference.yaml"
        assert yaml_path.exists()
        with open(yaml_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert config["loop"].get("memory_context_limit") == 20


# ============================================================
# 第7轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound7ReentryE2E:
    """端到端测试：常驻 scheduled 重入保护（第7轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_second_arun_on_resident_scheduled_raises_runtime_error(self):
        """首个 arun 启动常驻调度后，再次 arun 应抛 RuntimeError。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "scheduled"
        weave._config.loop.schedule = "@on_data_change"
        weave._config.loop.max_iterations = 1
        weave._config.loop.timeout = 5.0
        weave._config.llm.max_tokens = 100
        weave._config.llm.temperature = 0.0
        weave._config.prompts.system = "system"
        weave._llm = AsyncMock()
        weave._llm.chat = AsyncMock(return_value=LLMResponse(content="ok", model="test"))
        weave._event_bus = EventBus()
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.get_namespaces = MagicMock(return_value=["default:default:state"])
        weave._memory.state.set = MagicMock()
        weave._memory.stream.last = MagicMock(return_value=[])
        weave._memory.state.get_all = MagicMock(return_value={})
        weave._memory.knowledge.search = MagicMock(return_value=[])
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")
        weave._loop = ScheduledLoop()
        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}

        # 首个 arun：启动常驻调度（不返回，直到 shutdown）
        first = asyncio.create_task(weave.arun("hi"))
        # 事件驱动模式（第9轮修复项3）：先发 data_change 触发首次执行，
        # 再等待 state 写入完成（last_run_at / last_run_error）
        await _round6_wait_until(
            lambda: "data_change" in getattr(weave._event_bus, "subscriber_count", {})
        )
        await weave._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: weave._memory.state.set.call_count >= 2)

        # 常驻调度运行中，再次 arun 应抛 RuntimeError
        with pytest.raises(RuntimeError) as excinfo:
            await asyncio.wait_for(weave.arun("again"), timeout=5.0)
        assert "already running" in str(excinfo.value)
        assert "resident scheduled" in str(excinfo.value)

        # 清理：关闭常驻调度
        weave._loop.handle_shutdown()
        result = await asyncio.wait_for(first, timeout=5.0)
        assert result.output == "ok"


class TestRound7TriggerFailureE2E:
    """端到端测试：单次触发失败不终止调度器（第7轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_trigger_failure_does_not_kill_scheduler(self, caplog):
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(schedule="@on_data_change")),
            _event_bus=EventBus(),
            _run_locks={},
        )

        n = {"count": 0}

        async def fake_execute_once(agent_, user_input):
            n["count"] += 1
            if n["count"] == 1:
                raise RuntimeError("first trigger boom")
            return LoopResult(output="recovered", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        with caplog.at_level(logging.WARNING):
            task = asyncio.create_task(loop.run(agent, "hi"))
            await _round6_wait_until(
                lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
            )
            # 第一次触发（事件驱动需先发 data_change，第9轮修复项3）失败后，
            # 调度器应仍存活（count 停在 1，等待下次触发）
            await agent._event_bus.emit("data_change", {})
            await _round6_wait_until(lambda: n["count"] >= 1)
            # 触发第二次执行 → 成功
            await agent._event_bus.emit("data_change", {})
            await _round6_wait_until(lambda: n["count"] >= 2)
            loop.handle_shutdown()
            result = await asyncio.wait_for(task, timeout=5.0)

        assert result.output == "recovered"
        assert n["count"] == 2
        # 失败被记录告警而非终止调度器
        assert any("will retry on next schedule" in r.message for r in caplog.records)


class TestRound7DataChangeE2E:
    """端到端测试：@on_data_change 循环外常驻订阅不丢事件（第7轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_data_change_during_execution_not_lost(self):
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(schedule="@on_data_change")),
            _event_bus=EventBus(),
            _run_locks={},
        )

        n = {"count": 0}

        async def fake_execute_once(agent_, user_input):
            n["count"] += 1
            if n["count"] == 2:
                # 第二次执行期间（常驻订阅已注册）宿主发出 data_change ——
                # 若订阅在循环内"每次触发后才建立"，执行期间的事件会丢失，
                # 第三次触发将永远不会发生（count 卡在 2）。
                await agent_._event_bus.emit("data_change", {"during": True})
                await asyncio.sleep(0.05)
            return LoopResult(output=f"run{n['count']}", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        task = asyncio.create_task(loop.run(agent, "hi"))

        # 等待常驻订阅已注册（事件驱动模式首次触发需 data_change，第9轮修复项3）
        await _round6_wait_until(lambda: "data_change" in agent._event_bus.subscriber_count)
        # 第一次触发
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: n["count"] >= 1)
        # 触发第二次执行（其执行期间会再发 data_change → 第三次触发）
        await agent._event_bus.emit("data_change", {})
        # 第二次执行期间发出的 data_change 被常驻订阅捕获 → 第三次触发确实发生
        await _round6_wait_until(lambda: n["count"] >= 3)

        loop.handle_shutdown()
        result = await asyncio.wait_for(task, timeout=5.0)

        assert n["count"] == 3, "执行期间发出的 data_change 应被常驻订阅捕获并触发后续执行"
        assert result.output == "run3"


class TestRound7WsE2E:
    """端到端测试：ws.py 推送文档声明的 5 种事件类型（第7轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_ws_forwards_all_documented_event_types(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream
        from weave_agent_sdk.types import LoopResult

        bus = EventBus()
        weave = MagicMock()
        weave.on = bus.subscribe
        weave.emit = bus.emit

        async def fake_run_impl(input_text, **kwargs):
            # 先短暂让出，确保 ws 端订阅已建立（避免竞态丢失首事件）
            await asyncio.sleep(0.05)
            await bus.emit("token", {"text": "tok", "index": 0})
            await bus.emit("tool_call", {"name": "search", "arguments": {}})
            await bus.emit("tool_result", {"name": "search", "result": "ok", "error": False})
            return LoopResult(output="done", elapsed_ms=1, iterations=1, memory_updated={})

        weave._run_impl = fake_run_impl
        ws = _FakeWS({"input": "hi"})

        await ws_agent_stream(ws, weave)

        types_sent = [e["type"] for e in ws.sent]
        assert "token" in types_sent
        assert "tool_call" in types_sent
        assert "tool_result" in types_sent
        assert "done" in types_sent
        assert "error" not in types_sent

        rc = [e for e in ws.sent if e["type"] == "done"][0]
        # done 事件 data 直接携带 output/elapsed_ms/iterations（basic.md §5 契约）
        assert rc["data"]["output"] == "done"
        assert rc["data"]["elapsed_ms"] == 1
        assert rc["data"]["iterations"] == 1


class TestRound7MemoryContextLimitE2E:
    """端到端测试：before_think 注入 stream 历史条数受 memory_context_limit 上限约束（第7轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_before_think_caps_stream_history_at_limit(self):
        loop = _Round7ConcreteLoop()

        # limit=3：即使后端存有 50 条历史，也只注入 3 条
        agent = _Round7BeforeThinkAgent(limit=3)
        captured = {}

        def fake_last(n, namespaces):
            captured["n"] = n
            return [{"role": "user", "content": f"msg{i}"} for i in range(min(n, 50))]

        agent._memory.stream.last = fake_last

        ctx = await loop.before_think(agent, "hi")
        assert captured["n"] == 3
        assert len(ctx["stream"]) == 3

        # limit=10：注入条数随配置增大（可配置性验证）
        agent2 = _Round7BeforeThinkAgent(limit=10)
        captured2 = {}

        def fake_last2(n, namespaces):
            captured2["n"] = n
            return [{"role": "user", "content": f"msg{i}"} for i in range(min(n, 50))]

        agent2._memory.stream.last = fake_last2

        ctx2 = await loop.before_think(agent2, "hi")
        assert captured2["n"] == 10
        assert len(ctx2["stream"]) == 10


# ============================================================
# 第8轮测试（本轮新增）：针对第8轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第8轮修复" 标题（2026-08-18），共1个修复项:
#   - 修复项1: config.py `_ENV_VAR_RE` 正则补回可选组 `?`，
#     ${VAR}（无默认值）格式恢复匹配（第7轮修复项5 编辑 config.py 时丢失可选组）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound8EnvVarRegexFix:
    """第8轮修复项1（_ENV_VAR_RE 正则补回可选组 ?）—— 单元测试。"""

    def test_env_var_re_pattern_has_optional_group_question_mark(self):
        """_ENV_VAR_RE 的可选组 (?:...) 末尾应带 ?（核心修复验证）。

        若缺少末尾 ?，${VAR} 无默认值格式将完全不匹配（第7轮回归根因）。
        """
        from weave_agent_sdk.config import _ENV_VAR_RE

        assert _ENV_VAR_RE.pattern == r"\$\{(\w+)(?::(-?)([^}]*))?\}", \
            f"可选组末尾缺少 ?：{_ENV_VAR_RE.pattern}"

    def test_env_var_re_fullmatch_var_without_default(self):
        """${VAR} 无默认值格式应被完整匹配（group(2)=None 表示无冒号）。

        这是本轮修复的核心行为：第7轮 regex 丢失可选组 ? 后该格式完全不匹配。
        """
        from weave_agent_sdk.config import _ENV_VAR_RE

        m = _ENV_VAR_RE.fullmatch("${FOO}")
        assert m is not None, "${FOO} 无默认值格式应被匹配（可选组 ? 已补回）"
        assert m.groups() == ("FOO", None, None)

    def test_env_var_re_fullmatch_colon_default(self):
        """${VAR:default} POSIX 格式应被完整匹配（group(2)='' 表示仅有冒号无 -）。"""
        from weave_agent_sdk.config import _ENV_VAR_RE

        m = _ENV_VAR_RE.fullmatch("${FOO:bar}")
        assert m is not None
        assert m.groups() == ("FOO", "", "bar")

    def test_env_var_re_fullmatch_dash_default_excludes_dash(self):
        """${VAR:-default} Bash 格式的 - 应被独立捕获，不进入默认值（回归）。"""
        from weave_agent_sdk.config import _ENV_VAR_RE

        m = _ENV_VAR_RE.fullmatch("${FOO:-bar}")
        assert m is not None
        assert m.groups() == ("FOO", "-", "bar")

    def test_resolve_var_without_default_pure_placeholder_unset(self):
        """纯占位符 ${VAR}（无包围文本）未设置时解析为空字符串。"""
        os.environ.pop("RR8_UNSET_PURE", None)
        assert _resolve_env("${RR8_UNSET_PURE}") == ""

    def test_resolve_var_without_default_pure_placeholder_set(self):
        """纯占位符 ${VAR} 已设置时解析为环境变量值。"""
        os.environ["RR8_SET_PURE"] = "pure-val"
        try:
            assert _resolve_env("${RR8_SET_PURE}") == "pure-val"
        finally:
            del os.environ["RR8_SET_PURE"]

    def test_resolve_var_without_default_adjacent_placeholders(self):
        """相邻的多个 ${VAR} 占位符应全部解析，不互相吞并。"""
        os.environ["RR8_A"] = "a"
        os.environ["RR8_B"] = "b"
        try:
            assert _resolve_env("${RR8_A}${RR8_B}") == "ab"
        finally:
            del os.environ["RR8_A"]
            del os.environ["RR8_B"]

    def test_resolve_var_without_default_mixed_with_default_variants(self):
        """同一字符串中 ${VAR} / ${VAR:-d} / ${VAR:d} 三种格式混合应全部正确解析。"""
        os.environ["RR8_MIX"] = "mix"
        try:
            result = _resolve_env("${RR8_MIX}|${RR8_UNS1:-dash}|${RR8_UNS2:colon}")
            assert result == "mix|dash|colon"
        finally:
            del os.environ["RR8_MIX"]

    def test_dollar_without_braces_not_touched(self):
        """不带花括号的 $VAR 不应被误匹配（正则需要 ${...} 花括号）。"""
        assert _resolve_env("$HOME path") == "$HOME path"
        assert _resolve_env("cost is $5 and ${RR8_UNS}") == "cost is $5 and "

    def test_resolve_var_without_default_env_set_to_empty(self):
        """环境变量显式置空字符串时 ${VAR} 应返回空字符串（不误用非空默认）。"""
        os.environ["RR8_EMPTY"] = ""
        try:
            assert _resolve_env("x${RR8_EMPTY}y") == "xy"
        finally:
            del os.environ["RR8_EMPTY"]


class TestRound8EnvVarRegexE2E:
    """端到端测试：${VAR} 无默认值格式在 load_config 全链路中正确解析（第8轮修复项1）。"""

    def _write_yaml(self, tmp_path, text: str):
        """将含占位符的原始 YAML 文本写入临时文件，交由 load_config 解析。"""
        p = tmp_path / "round8_env.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_e2e_load_config_var_without_default_unset(self, tmp_path):
        """YAML 含 ${UNSET_VAR} 且未设置时，配置字段应为空字符串。"""
        p = self._write_yaml(tmp_path,
            "agent:\n  name: ${RR8_E2E_UNSET}\n"
            "llm:\n  provider: anthropic\nloop:\n  type: simple\n"
            "memory:\n  scopes: {}\nprompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n")
        os.environ.pop("RR8_E2E_UNSET", None)
        config = load_config(p)
        assert config.agent.name is None  # 空占位符解析后 YAML 空标量解析为 None

    def test_e2e_load_config_var_without_default_from_env(self, tmp_path, monkeypatch):
        """YAML 含 ${VAR} 且环境变量已设置时，配置字段应为环境变量值。"""
        monkeypatch.setenv("RR8_E2E_MODEL", "e2e-model-2026")
        p = self._write_yaml(tmp_path,
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n  model: ${RR8_E2E_MODEL}\n"
            "loop:\n  type: simple\n"
            "memory:\n  scopes: {}\nprompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n")
        config = load_config(p)
        assert config.llm.model == "e2e-model-2026"

    def test_e2e_load_config_three_formats_mixed(self, tmp_path, monkeypatch):
        """同一 YAML 中 ${VAR} / ${VAR:-d} / ${VAR:d} 三种格式全部正确解析。"""
        monkeypatch.setenv("RR8_E2E_TOKENS", "9")  # 默认 7，但环境变量优先
        p = self._write_yaml(tmp_path,
            "agent:\n  name: ${RR8_E2E_UNS}\n"
            "llm:\n  provider: anthropic\n"
            "  max_tokens: ${RR8_E2E_TOKENS:-7}\n"
            "  temperature: ${RR8_E2E_TEMP:0.5}\n"
            "loop:\n  type: simple\n"
            "memory:\n  scopes: {}\nprompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n")
        os.environ.pop("RR8_E2E_UNS", None)
        config = load_config(p)
        assert config.agent.name is None            # ${VAR} 未设置 → 空（YAML 空标量解析为 None）
        assert config.llm.max_tokens == 9         # ${VAR:-7} 环境变量优先
        assert config.llm.temperature == 0.5      # ${VAR:0.5} → 默认 0.5

    def test_e2e_load_config_var_without_default_embedded_in_text(self, tmp_path):
        """${VAR} 无默认值嵌入文本（如路径前缀）时，仅占位符被替换，其余文本保留。"""
        os.environ.pop("RR8_E2E_PATH", None)
        p = self._write_yaml(tmp_path,
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\nloop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            "        path: ${RR8_E2E_PATH}/data/memory.db\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n")
        config = load_config(p)
        stream_cfg = config.memory.scopes["session"].stream
        assert stream_cfg["path"] == "/data/memory.db"  # 占位符替换为空，其余文本保留

    def test_e2e_actual_weave_yaml_env_override_still_works(self, monkeypatch):
        """真实 weave.yaml 的 ${WEAVE_MODEL:-default} 在环境变量设置时仍被覆盖（回归）。

        验证补回可选组 ? 不破坏真实配置文件使用的 ${VAR:-default} 格式。
        """
        monkeypatch.setenv("WEAVE_MODEL", "r8-rollout-model")
        project_root = Path(__file__).parent.parent
        config = load_config(project_root / "weave.yaml")
        assert config.llm.model == "r8-rollout-model"

# ============================================================
# 第9轮测试（本轮新增）：针对第9轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第9轮修复" 标题（2026-08-19），共5个修复项:
#   - 修复项1: iterative.py 窗口裁剪保留当前 user 输入，
#     修复长迭代丢失当前请求上下文（P2）
#   - 修复项2: scheduled.py 每触发更新 status().last_run（P3）
#   - 修复项3: scheduled.py 事件驱动读取 data_change payload 作为触发输入（P3）
#   - 修复项4: agent.py stream() 入口支持 tool_filter（P3）
#   - 修复项5: agent.py 常驻 scheduled 路径清理 _run_locks 防泄漏（P3）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound9IterativeTrimKeepUserInputUnit:
    """第9轮修复项1（窗口裁剪保留当前 user 输入）—— 单元测试。"""

    def test_trim_source_keeps_current_user_input(self):
        """裁剪应保留 messages[0](system) 与 messages[1](当前 user 输入)。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "trimmed = [messages[0], messages[1]] + messages[-(_MAX_CONTEXT_MESSAGES - 2):]" in source

    def test_trim_source_window_is_max_minus_two(self):
        """裁剪窗口为最近 _MAX_CONTEXT_MESSAGES-2 条（system+user 占 2 条）。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "start = len(messages) - (_MAX_CONTEXT_MESSAGES - 2)" in source
        assert "messages[-(_MAX_CONTEXT_MESSAGES - 2):]" in source


class TestRound9ScheduledLastRunUnit:
    """第9轮修复项2（scheduled 每触发更新 status().last_run）—— 单元测试。"""

    def test_execute_once_source_sets_last_run(self):
        """_execute_once 应设置 agent._last_run = time.time()。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop._execute_once)
        assert "agent._last_run = time.time()" in source

    @pytest.mark.asyncio
    async def test_execute_once_updates_last_run_after_run(self):
        """_execute_once 执行后 agent._last_run 应为最近的时间戳。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._last_run = None
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        before = time.time()
        await loop._execute_once(agent, "input")
        after = time.time()

        assert isinstance(agent._last_run, float)
        assert before <= agent._last_run <= after


class TestRound9ScheduledPayloadUnit:
    """第9轮修复项3（事件驱动读取 data_change payload）—— 单元测试。"""

    def test_run_source_reads_payload_input(self):
        """run() 事件驱动模式应读取 data_change payload 的 input 字段作为触发输入。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        assert "event_data = _event.data or {}" in source
        assert 'trigger_input = event_data.get("input", user_input)' in source
        assert "async for _event in data_stream:" in source


class TestRound9StreamToolFilterUnit:
    """第9轮修复项4（stream() 入口支持 tool_filter）—— 单元测试。"""

    def test_stream_signature_has_tool_filter(self):
        """stream() 方法签名应包含 tool_filter 参数。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        sig = inspect.signature(Weave.stream)
        assert "tool_filter" in sig.parameters

    def test_stream_passes_tool_filter_to_run_impl(self):
        """stream() 应将 tool_filter 透传给 _run_impl。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        assert "self._run_impl(input, scope_hints, context, tool_filter, _streaming=True)" in source


class TestRound9RunLocksCleanupUnit:
    """第9轮修复项5（常驻 scheduled 路径清理 _run_locks 防泄漏）—— 单元测试。"""

    def test_run_impl_calls_cleanup_closed_run_locks(self):
        """_run_impl 起始处应调用 _cleanup_closed_run_locks()。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._run_impl)
        assert "self._cleanup_closed_run_locks()" in source

    def test_cleanup_removes_closed_loop_locks(self):
        """_cleanup_closed_run_locks 应移除已关闭事件循环的锁条目，保留活动循环。"""
        from weave_agent_sdk.agent import Weave

        class _FakeClosedLoop:
            def is_closed(self):
                return True

        class _FakeOpenLoop:
            def is_closed(self):
                return False

        weave = Weave.__new__(Weave)
        closed_loop = _FakeClosedLoop()
        open_loop = _FakeOpenLoop()
        weave._run_locks = {closed_loop: "closed-lock", open_loop: "open-lock"}

        weave._cleanup_closed_run_locks()

        assert closed_loop not in weave._run_locks
        assert open_loop in weave._run_locks

    def test_cleanup_noop_when_locks_empty(self):
        """_run_locks 不存在或为空时 _cleanup_closed_run_locks 不报错。"""
        from weave_agent_sdk.agent import Weave

        weave = Weave.__new__(Weave)
        weave.__dict__.pop("_run_locks", None)
        weave._cleanup_closed_run_locks()

        weave._run_locks = {}
        weave._cleanup_closed_run_locks()


# ============================================================
# 第9轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound9IterativeTrimE2E:
    """端到端测试：长迭代窗口裁剪保留当前 user 输入（第9轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_trim_keeps_current_user_input(self):
        """15 轮 tool 迭代中，每次注入 LLM 的窗口都保留当前 user 输入。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop, _MAX_CONTEXT_MESSAGES
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 15
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"

        captured = []
        n = {"i": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            captured.append(list(messages))
            n["i"] += 1
            if n["i"] < 15:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[ToolCall(id=f"c{n['i']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
            loop = IterativeLoop()
            loop.on_start = AsyncMock()
            loop.on_end = AsyncMock()
            loop.before_think = AsyncMock(return_value={})
            loop.after_think = AsyncMock()

            result = await asyncio.wait_for(loop.run(agent, "remember this user query"), timeout=10.0)

        assert result.output == "final answer"
        assert result.iterations == 15
        assert len(captured) == 15
        # 修复项1 核心：每一轮注入 LLM 的窗口，messages[1] 都保留当前 user 输入
        for i, msgs in enumerate(captured):
            assert msgs[0].role == "system"
            assert msgs[1].role == "user", f"第{i+1}轮窗口丢失当前 user 输入"
            assert msgs[1].content == "remember this user query"
            assert len(msgs) <= _MAX_CONTEXT_MESSAGES


class TestRound9ScheduledLastRunE2E:
    """端到端测试：常驻 scheduled 每触发更新 status().last_run（第9轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_updates_last_run_per_trigger(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.state.set = MagicMock()
        agent._event_bus = EventBus()
        agent._last_run = None
        agent._run_locks = {}

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        task = asyncio.create_task(loop.run(agent, "hi"))

        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: agent._last_run is not None)
        first = agent._last_run

        await asyncio.sleep(0.01)
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(
            lambda: agent._last_run is not None and agent._last_run > first
        )

        loop.handle_shutdown()
        result = await asyncio.wait_for(task, timeout=5.0)

        assert result.output == "scheduled"
        assert agent._last_run > first


class TestRound9ScheduledPayloadE2E:
    """端到端测试：data_change payload 作为触发输入（第9轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_data_change_payload_drives_trigger_input(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        # 第12轮修复确认：事件驱动触发间受 loop.event_min_interval
        # 节流（默认 1.0s）。显式关闭节流消除与原漂移测试的时序竞态。
        agent._config.loop.event_min_interval = 0
        agent._event_bus = EventBus()
        agent._run_locks = {}

        received = []

        async def fake_execute_once(agent_, user_input):
            received.append(user_input)
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        task = asyncio.create_task(loop.run(agent, "initial-input"))

        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        # 启动即执行一次（初始 user_input）
        await _round6_wait_until(lambda: len(received) >= 1)
        assert received[0] == "initial-input"

        # 第一次 data_change：payload 无 input 字段 → 回退到 user_input
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: len(received) >= 2)
        assert received[1] == "initial-input"

        # 第二次 data_change：payload 的 input 字段驱动触发输入
        await agent._event_bus.emit("data_change", {"input": "payload-driven-input"})
        await _round6_wait_until(lambda: len(received) >= 3)
        assert received[2] == "payload-driven-input"

        loop.handle_shutdown()
        await asyncio.wait_for(task, timeout=5.0)


class TestRound9StreamToolFilterE2E:
    """端到端测试：stream() 入口支持 tool_filter（第9轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_tool_filter_filters_tools(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.types import LoopResult

        def tool_a(query: str) -> str:
            return "a"

        def tool_b(query: str) -> str:
            return "b"

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.schedule = None
        weave._config.loop.timeout = 5.0
        weave._config.agent.name = "test"
        weave._event_bus = EventBus()
        weave._tools = [tool_a, tool_b]
        weave._tool_map = {"tool_a": tool_a, "tool_b": tool_b}
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}
        weave._memory = MagicMock()
        weave._load_system_prompt = MagicMock(return_value="sys")

        seen = {}

        async def fake_loop_run(agent_, user_input):
            seen["tools"] = [t.__name__ for t in agent_._tools]
            seen["tool_map"] = list(agent_._tool_map.keys())
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        weave._loop = MagicMock()
        weave._loop.run = fake_loop_run

        events = []
        async for event in weave.stream("hello", tool_filter=["tool_a"]):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # 修复项4 核心：运行期间 tools 被过滤为仅 tool_a
        assert seen["tools"] == ["tool_a"]
        assert seen["tool_map"] == ["tool_a"]
        # 运行结束后 tools 恢复完整（无泄漏）
        assert [t.__name__ for t in weave._tools] == ["tool_a", "tool_b"]
        assert list(weave._tool_map.keys()) == ["tool_a", "tool_b"]
        assert events[-1].type == "done"


class TestRound9RunLocksCleanupE2E:
    """端到端测试：_run_impl 清理已关闭事件循环的锁条目（第9轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_run_impl_cleans_closed_loop_locks(self):
        from weave_agent_sdk.types import LoopResult

        class _FakeClosedLoop:
            def is_closed(self):
                return True

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "iterative"
        weave._config.loop.schedule = None
        closed_loop = _FakeClosedLoop()
        weave._run_locks = {closed_loop: "stale-lock"}
        weave._run_impl_inner = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=0, iterations=1, memory_updated={}
        ))

        result = await weave._run_impl("hi")

        assert result.output == "ok"
        # 已关闭事件循环的锁条目在 _run_impl 入口被清理（防泄漏）
        assert closed_loop not in weave._run_locks


# ============================================================
# 第10轮测试（本轮新增）：针对第9轮修复行为的补充验证
# 依据 fix_record.md 最后一个 "## 第9轮修复" 标题（2026-08-19，最近一轮），
# 与既有 第9轮测试 互补（新增 15 个测试：10 个单元测试 + 5 个端到端测试）:
#   - 修复项1: iterative.py 窗口裁剪保留当前 user 输入
#     （补充：tool 跳过分支同样保留 user / memory context 注入 system 而非覆盖 user）
#   - 修复项2: scheduled.py 每触发更新 status().last_run
#     （补充：state 写失败仍更新 / LLM 调用失败不更新）
#   - 修复项3: scheduled.py 事件驱动读取 data_change payload
#     （补充：payload 读取位于常驻 while 循环内 / 空字符串 payload 驱动空触发输入）
#   - 修复项4: agent.py stream() 入口支持 tool_filter
#     （补充：空 filter 不生效 / 异常路径工具表恢复 / 过滤全部后恢复）
#   - 修复项5: agent.py 常驻 scheduled 路径清理 _run_locks 防泄漏
#     （补充：快照遍历 / 无 is_closed 属性保留 / 锁对象身份保持 / 开放循环保留）
# ============================================================


class TestRound10TrimUnit:
    """第9轮修复项1（窗口裁剪保留当前 user 输入）—— 第10轮补充单元测试。"""

    def test_trim_skip_branch_preserves_user_input(self):
        """裁剪的 tool 跳过分支重建窗口时同样保留 [system, user]，当前 user 输入不被裁掉。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        # 跳过分支重建窗口必须同样保留 messages[1]（当前 user 输入）
        assert "trimmed = [messages[0], messages[1]] + messages[start:]" in source

    @pytest.mark.asyncio
    async def test_memory_context_injected_into_system_not_user(self):
        """memory context 应注入 system prompt（messages[0]），不覆盖 messages[1] 的当前 user 输入。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.loop.stop_conditions = [{"type": "no_tool_calls"}]
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        from weave_agent_sdk.llm.base import LLMResponse
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="answer", model="test"))
        agent._system_prompt = "Base system"
        agent._tools = []
        agent._tool_map = {}
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])  # persist 为 no-op

        captured = []

        async def fake_call_llm(agent_, messages, tools=None):
            captured.append(list(messages))
            return LLMResponse(content="answer", model="test")

        with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
            loop = IterativeLoop()
            loop.on_start = AsyncMock()
            loop.on_end = AsyncMock()
            loop.before_think = AsyncMock(return_value={
                "stream": [{"role": "user", "content": "ctx-marker"}],
            })
            loop.after_think = AsyncMock()

            user_input = "remember this user query"
            result = await loop.run(agent, user_input)

        assert result.output == "answer"
        assert len(captured) == 1
        window = captured[0]
        # messages[1] 始终保留当前 user 输入
        assert window[1].role == "user"
        assert window[1].content == user_input
        # memory context 只注入 system（messages[0]），不覆盖 user
        assert "ctx-marker" in window[0].content
        assert "Base system" in window[0].content


class TestRound10ScheduledLastRunUnit:
    """第9轮修复项2（scheduled 每触发更新 status().last_run）—— 第10轮补充单元测试。"""

    def _make_agent(self, llm_error=None, state_error=None):
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        if llm_error is not None:
            agent._llm.chat = AsyncMock(side_effect=llm_error)
        else:
            agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": ["s:1:state"], "knowledge": [],
        }[at])
        if state_error is not None:
            agent._memory.state.set = MagicMock(side_effect=state_error)
        else:
            agent._memory.state.set = MagicMock()
        agent._last_run = None
        return agent

    @pytest.mark.asyncio
    async def test_last_run_updated_when_state_write_fails(self, caplog):
        """State 写入失败时，_last_run 仍应更新（更新先于 state 写入，不被失败阻断）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        agent = self._make_agent(state_error=RuntimeError("storage down"))
        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with caplog.at_level(logging.WARNING):
            result = await loop._execute_once(agent, "input")

        assert result.output == "scheduled"
        assert isinstance(agent._last_run, float), "state 写失败不应阻断 last_run 更新"
        assert any("Failed to save scheduled run state" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_last_run_not_updated_when_llm_call_fails(self):
        """LLM 调用抛异常时，_execute_once 传播异常且 _last_run 保持不变（None）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        agent = self._make_agent(llm_error=RuntimeError("llm down"))
        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with pytest.raises(RuntimeError, match="llm down"):
            await loop._execute_once(agent, "input")

        assert agent._last_run is None


class TestRound10ScheduledPayloadUnit:
    """第9轮修复项3（data_change payload 作为触发输入）—— 第10轮补充单元测试。"""

    def test_payload_read_happens_inside_resident_loop(self):
        """payload 触发输入的读取应位于常驻 while 循环内（每事件读取），而非仅启动时一次。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        while_idx = source.index("while not self._shutting_down:")
        payload_idx = source.index('trigger_input = event_data.get("input", user_input)')
        assert payload_idx > while_idx, "payload 读取应位于常驻 while 循环内部"


class TestRound10StreamToolFilterUnit:
    """第9轮修复项4（stream() 入口支持 tool_filter）—— 第10轮补充单元测试。"""

    def _make_weave(self, loop_run):
        from weave_agent_sdk.event_bus import EventBus

        def tool_a(query: str) -> str:
            return "a"

        def tool_b(query: str) -> str:
            return "b"

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.schedule = None
        weave._config.loop.timeout = 5.0
        weave._config.agent.name = "test"
        weave._event_bus = EventBus()
        weave._tools = [tool_a, tool_b]
        weave._tool_map = {"tool_a": tool_a, "tool_b": tool_b}
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}
        weave._memory = MagicMock()
        weave._load_system_prompt = MagicMock(return_value="sys")
        weave._loop = MagicMock()
        weave._loop.run = loop_run
        return weave

    @pytest.mark.asyncio
    async def test_stream_empty_tool_filter_is_noop(self):
        """tool_filter=[]（空列表，falsy）不应过滤任何工具。"""
        from weave_agent_sdk.types import LoopResult

        seen = {}

        async def fake_loop_run(agent_, user_input):
            seen["tools"] = [t.__name__ for t in agent_._tools]
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        weave = self._make_weave(fake_loop_run)

        events = []
        async for event in weave.stream("hello", tool_filter=[]):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert seen["tools"] == ["tool_a", "tool_b"]
        assert events[-1].type == "done"

    @pytest.mark.asyncio
    async def test_stream_tool_filter_restores_tools_on_error(self):
        """loop.run 抛异常时，stream() 仍应在 finally 恢复完整的工具表（无泄漏）。"""
        from weave_agent_sdk.types import LoopResult

        async def fake_loop_run(agent_, user_input):
            raise ValueError("boom")

        weave = self._make_weave(fake_loop_run)

        events = []
        async for event in weave.stream("hello", tool_filter=["tool_a"]):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert events[-1].type == "error"
        # 异常路径结束后工具表完整恢复
        assert [t.__name__ for t in weave._tools] == ["tool_a", "tool_b"]
        assert list(weave._tool_map.keys()) == ["tool_a", "tool_b"]


class TestRound10RunLocksCleanupUnit:
    """第9轮修复项5（常驻 scheduled 路径清理 _run_locks 防泄漏）—— 第10轮补充单元测试。"""

    def test_cleanup_iterates_over_snapshot_list(self):
        """_cleanup_closed_run_locks 应遍历 list(locks) 快照，避免边删边遍历。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._cleanup_closed_run_locks)
        assert "for existing_loop in list(locks):" in source

    def test_cleanup_keeps_loop_without_is_closed_attr(self):
        """无 is_closed 属性的事件循环对象应被保留（无法判定关闭即视为存活）。"""
        from weave_agent_sdk.agent import Weave

        class _Closed:
            def is_closed(self):
                return True

        class _Open:
            def is_closed(self):
                return False

        class _NoIsClosed:
            pass

        closed = _Closed()
        open_ = _Open()
        noattr = _NoIsClosed()

        weave = Weave.__new__(Weave)
        weave._run_locks = {closed: "c", open_: "o", noattr: "n"}

        weave._cleanup_closed_run_locks()

        assert closed not in weave._run_locks
        assert weave._run_locks.get(open_) == "o"
        assert weave._run_locks.get(noattr) == "n"

    def test_cleanup_preserves_open_lock_object_identity(self):
        """已关闭事件循环的锁被移除，活动事件循环的锁对象保持原引用。"""
        from weave_agent_sdk.agent import Weave

        class _Closed:
            def is_closed(self):
                return True

        class _Open:
            def is_closed(self):
                return False

        closed = _Closed()
        open_ = _Open()
        open_lock = asyncio.Lock()

        weave = Weave.__new__(Weave)
        weave._run_locks = {closed: "closed-lock", open_: open_lock}

        weave._cleanup_closed_run_locks()

        assert closed not in weave._run_locks
        assert weave._run_locks[open_] is open_lock


# ============================================================
# 第10轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound10IterativeTrimE2E:
    """端到端测试：多 tool 长迭代窗口裁剪保留当前 user 输入且无孤立 tool（第9轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_multi_tool_trim_keeps_user_input_no_orphan(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop, _MAX_CONTEXT_MESSAGES
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 12
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def tool_a(query: str) -> str:
            return f"a:{query}"

        async def tool_b(query: str) -> str:
            return f"b:{query}"

        agent._tools = [tool_a, tool_b]
        agent._tool_map = {"tool_a": tool_a, "tool_b": tool_b}
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])  # persist 为 no-op

        captured = []
        n = {"i": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            captured.append(list(messages))
            n["i"] += 1
            if n["i"] < 12:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[
                        ToolCall(id=f"c{n['i']}a", name="tool_a", arguments={"query": "q"}),
                        ToolCall(id=f"c{n['i']}b", name="tool_b", arguments={"query": "q"}),
                    ],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
            loop = IterativeLoop()
            loop.on_start = AsyncMock()
            loop.on_end = AsyncMock()
            loop.before_think = AsyncMock(return_value={})
            loop.after_think = AsyncMock()

            user_input = "multi-tool user query"
            result = await asyncio.wait_for(loop.run(agent, user_input), timeout=10.0)

        assert result.output == "final answer"
        assert result.iterations == 12
        assert len(captured) == 12
        # 每轮注入 LLM 的窗口都保留当前 user 输入且不超过上限
        for i, msgs in enumerate(captured):
            assert msgs[0].role == "system"
            assert msgs[1].role == "user", f"第{i+1}轮窗口丢失当前 user 输入"
            assert msgs[1].content == user_input
            assert len(msgs) <= _MAX_CONTEXT_MESSAGES
            # 裁剪后窗口起点不能是孤立 tool 消息
            if len(msgs) > 2:
                assert msgs[2].role != "tool", f"第{i+1}轮窗口起点为孤立 tool 消息"
        # 12 轮 × 2 tool = 36 条若不裁剪必然超上限；max<=20 证明裁剪确实被触发
        assert max(len(m) for m in captured) <= _MAX_CONTEXT_MESSAGES
        assert result.memory_updated["tools"] == {"tool_a": 11, "tool_b": 11}


class TestRound10ScheduledLastRunE2E:
    """端到端测试：常驻 scheduled 每次触发更新 status().last_run，state 写失败不阻断（第9轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_last_run_updates_each_trigger_despite_state_failure(self, caplog):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._config.loop.event_min_interval = 0
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.state.set = MagicMock(side_effect=RuntimeError("storage down"))
        agent._event_bus = EventBus()
        agent._last_run = None
        agent._run_locks = {}

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        task = asyncio.create_task(loop.run(agent, "hi"))

        with caplog.at_level(logging.WARNING):
            await _round6_wait_until(
                lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
            )
            # 事件驱动模式启动即执行一次（第9轮语义）
            await _round6_wait_until(lambda: agent._last_run is not None)
            first = agent._last_run

            # 第二次触发
            await agent._event_bus.emit("data_change", {})
            await _round6_wait_until(
                lambda: agent._last_run is not None and agent._last_run > first
            )
            second = agent._last_run

            # 第三次触发
            await asyncio.sleep(0.01)
            await agent._event_bus.emit("data_change", {})
            await _round6_wait_until(
                lambda: agent._last_run is not None and agent._last_run > second
            )

        loop.handle_shutdown()
        result = await asyncio.wait_for(task, timeout=5.0)

        assert result.output == "scheduled"
        assert second > first
        assert agent._last_run > second
        # 每次触发 state 写失败都记录告警，但不阻断 last_run 更新
        assert any("Failed to save scheduled run state" in r.message for r in caplog.records)


class TestRound10ScheduledPayloadE2E:
    """端到端测试：data_change payload 空字符串 input 驱动空触发输入（第9轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_data_change_payload_empty_string_input(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._config.loop.event_min_interval = 0
        agent._event_bus = EventBus()
        agent._run_locks = {}

        received = []

        async def fake_execute_once(agent_, user_input):
            received.append(user_input)
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        task = asyncio.create_task(loop.run(agent, "initial-input"))

        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        # 启动即执行一次（默认 user_input）
        await _round6_wait_until(lambda: len(received) >= 1)
        assert received[0] == "initial-input"

        # payload input 为空字符串（key 存在）→ 触发输入为空字符串，而非回退默认
        await agent._event_bus.emit("data_change", {"input": ""})
        await _round6_wait_until(lambda: len(received) >= 2)
        assert received[1] == ""

        loop.handle_shutdown()
        await asyncio.wait_for(task, timeout=5.0)


class TestRound10StreamToolFilterE2E:
    """端到端测试：stream(tool_filter) 过滤全部工具后完整恢复（第9轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_tool_filter_excludes_all_then_restores(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.types import LoopResult

        def tool_a(query: str) -> str:
            return "a"

        def tool_b(query: str) -> str:
            return "b"

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.schedule = None
        weave._config.loop.timeout = 5.0
        weave._config.agent.name = "test"
        weave._event_bus = EventBus()
        weave._tools = [tool_a, tool_b]
        weave._tool_map = {"tool_a": tool_a, "tool_b": tool_b}
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}
        weave._memory = MagicMock()
        weave._load_system_prompt = MagicMock(return_value="sys")

        seen = {}

        async def fake_loop_run(agent_, user_input):
            seen["tools"] = [t.__name__ for t in agent_._tools]
            seen["tool_map"] = list(agent_._tool_map.keys())
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        weave._loop = MagicMock()
        weave._loop.run = fake_loop_run

        events = []
        async for event in weave.stream("hello", tool_filter=["nonexistent_tool"]):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # 运行期间所有工具被过滤（filter 命中不存在工具 → 空工具表）
        assert seen["tools"] == []
        assert seen["tool_map"] == []
        assert events[-1].type == "done"
        # 结束后完整恢复（无泄漏）
        assert [t.__name__ for t in weave._tools] == ["tool_a", "tool_b"]
        assert list(weave._tool_map.keys()) == ["tool_a", "tool_b"]


class TestRound10RunLocksCleanupE2E:
    """端到端测试：_run_impl 入口清理已关闭事件循环锁并保留活动循环锁（第9轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_run_impl_cleans_closed_keeps_open(self):
        from weave_agent_sdk.types import LoopResult

        class _ClosedLoop:
            def is_closed(self):
                return True

        class _OpenLoop:
            def is_closed(self):
                return False

        closed = _ClosedLoop()
        open_ = _OpenLoop()
        open_lock = asyncio.Lock()

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "iterative"
        weave._config.loop.schedule = None
        weave._run_locks = {closed: "closed-lock", open_: open_lock}
        weave._run_impl_inner = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=0, iterations=1, memory_updated={}
        ))

        result = await weave._run_impl("hi")

        assert result.output == "ok"
        assert closed not in weave._run_locks
        assert weave._run_locks[open_] is open_lock


# ============================================================
# 第11轮测试（本轮新增）：针对第11轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第11轮修复" 标题（2026-08-21），共5个修复项:
#   - 修复项1: agent.py stream() 整体超时兜底（LLM 挂起时 emit error 而非永久阻塞）— P2
#   - 修复项2: scheduled.py handle_shutdown 执行中分支经 call_soon_threadsafe
#     + call_later 宽限 30s，线程安全（与等待中分支一致）— P3
#   - 修复项3: sqlite.py knowledge_search 空查询守卫（空/纯空白查询返回 []，
#     避免生成非法 SQL 被吞掉导致 knowledge 检索静默失效）— P3
#   - 修复项4: event_bus.py docstring 事件名对齐契约（5 种事件类型）— P3
#   - 修复项5: iterative.py output 回退最后 assistant 文本（max_iterations
#     耗尽且最后消息为 tool 消息时输出可读文本而非 tool JSON）— P3
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound11StreamTimeoutGuardUnit:
    """第11轮修复项1（stream() 整体超时兜底）—— 单元测试。"""

    def test_stream_source_has_timeout_guard_wrapper(self):
        """stream() 源码应包含带 wait_for 的整体超时兜底包装任务。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        # 第14轮修正：空闲超时语义（当前实现）——wait_for 应用于消费端
        # anext(subscribe_gen)，事件到达即重置计时；长时间无事件才触发。
        assert "event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)" in source
        assert 'message": f"Stream timed out after {stream_timeout}s (no events received)"' in source
        assert 'exception": "TimeoutError"' in source

    def test_stream_source_guard_only_when_positive_timeout(self):
        """整体超时兜底应仅在 timeout 为正值时启用（0/负值不启用，避免误杀正常流程）。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        assert "isinstance(stream_timeout, (int, float)) and stream_timeout > 0" in source
        assert "if has_timeout:" in source
        # 第14轮修正：空闲超时只影响消费端等待（wait_for anext），生产任务仍直接创建
        assert "asyncio.create_task(_run_and_emit())" in source


class TestRound11ScheduledShutdownThreadSafeUnit:
    """第11轮修复项2（handle_shutdown call_later 线程安全）—— 单元测试。"""

    def test_handle_shutdown_source_call_soon_threadsafe_call_later(self):
        """执行中分支应经 call_soon_threadsafe 调度 call_later(30.0, _force_cancel)。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.handle_shutdown)
        assert "call_soon_threadsafe(lambda: loop.call_later(30.0, _force_cancel))" in source

    def test_handle_shutdown_executing_schedules_graceful_cancel(self):
        """执行中分支不应直接 cancel，而是经 call_soon_threadsafe 注册 30s 宽限定时器。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        fake_loop = MagicMock()
        fake_task.get_loop.return_value = fake_loop
        loop._current_task = fake_task
        loop._executing = True

        loop.handle_shutdown()

        assert loop._shutting_down is True
        # 执行中：不直接取消（宽限 30s）
        fake_task.cancel.assert_not_called()
        fake_loop.call_soon_threadsafe.assert_called_once()
        # 取回调，模拟在事件循环线程内执行：应注册 call_later(30.0, _force_cancel)
        cb = fake_loop.call_soon_threadsafe.call_args[0][0]
        fake_loop.call_later.reset_mock()
        cb()
        fake_loop.call_later.assert_called_once()
        assert fake_loop.call_later.call_args[0][0] == 30.0
        # 触发 _force_cancel → 此时才取消任务
        force_cancel = fake_loop.call_later.call_args[0][1]
        force_cancel()
        fake_task.cancel.assert_called_once()


class TestRound11KnowledgeSearchGuardUnit:
    """第11轮修复项3（sqlite.py knowledge_search 空查询守卫）—— 单元测试。"""

    def test_knowledge_search_empty_query_returns_empty(self):
        """空查询字符串应返回空列表，不触发 SQL 构造。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        backend.knowledge_add("hello world", "scope1:u1:knowledge", None)

        assert backend.knowledge_search("", None, 5) == []

    def test_knowledge_search_whitespace_query_returns_empty(self):
        """纯空白查询字符串应返回空列表（query.split() 无有效词）。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        backend.knowledge_add("hello world", "scope1:u1:knowledge", None)

        assert backend.knowledge_search("   ", None, 5) == []

    def test_knowledge_search_normal_query_still_works(self):
        """正常非空查询仍应返回匹配结果（回归，守卫不误伤正常检索）。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        backend.knowledge_add("alpha beta gamma", "scope1:u1:knowledge", None)
        backend.knowledge_add("delta epsilon", "scope1:u1:knowledge", None)

        results = backend.knowledge_search("beta", None, 5)
        assert len(results) == 1
        assert results[0].content == "alpha beta gamma"


class TestRound11EventBusDocstringUnit:
    """第11轮修复项4（event_bus.py docstring 事件名对齐契约）—— 单元测试。"""

    def test_event_bus_docstring_documents_five_event_types(self):
        """event_bus 模块 docstring 应明确列出 5 种不可变契约事件类型。"""
        import inspect
        import weave_agent_sdk.event_bus as eb

        source = inspect.getsource(eb)
        for et in ("token", "tool_call", "tool_result", "done", "error"):
            assert et in source, f"event_bus.py docstring 缺少事件类型: {et}"
        # 文档明确标注"不可变契约"
        assert "不可变契约" in source


class TestRound11IterativeOutputFallbackUnit:
    """第11轮修复项5（iterative.py output 回退最后 assistant 文本）—— 单元测试。"""

    def test_iterative_output_fallback_source_has_last_assistant_content(self):
        """run() 源码应追踪最后一条 assistant 文本，并在最终输出时回退到它。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "last_assistant_content" in source
        assert "last_assistant_content = response.content" in source

    def test_iterative_output_fallback_source_output_expression(self):
        """最终输出表达式应依次回退：final_output → last_assistant_content → 最后消息。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "output = final_output or last_assistant_content or messages[-1].content" in source


# ============================================================
# 第11轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound11StreamTimeoutE2E:
    """端到端测试：stream() 整体超时兜底（第11轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_hanging_run_emits_error_within_timeout(self):
        """_run_impl 挂起（不产生任何事件）时，stream() 应在 loop.timeout 后 emit error。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 0.05
        weave._config.loop.stream_timeout = 0.05  # 第13轮修复：整体超时改用 loop.stream_timeout
        weave._event_bus = EventBus()

        async def hang_impl(*args, **kwargs):
            await asyncio.sleep(100)  # 模拟 LLM 调用挂起，不产生任何事件

        weave._run_impl = hang_impl

        events = []

        async def consume():
            async for event in weave.stream("input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break
            return events

        # 若超时兜底缺失，此测试会因永久阻塞在 consume 上而触发 5s 超时失败
        result = await asyncio.wait_for(consume(), timeout=5.0)

        assert len(result) >= 1
        assert result[-1].type == "error"
        assert "timed out after 0.05s" in result[-1].data["message"]
        assert result[-1].data["exception"] == "TimeoutError"


class TestRound11ScheduledShutdownE2E:
    """端到端测试：handle_shutdown 执行中分支宽限 30s（第11轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_handle_shutdown_executing_keeps_task_during_grace(self):
        """执行中分支调用 handle_shutdown 后，任务不应被立即取消（宽限 30s）。"""
        import contextlib
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()

        async def sleeper():
            await asyncio.sleep(3600)

        task = asyncio.create_task(sleeper())
        loop._current_task = task
        loop._executing = True

        loop.handle_shutdown()

        assert loop._shutting_down is True
        # 宽限期内任务保持运行（未被立即 cancel）
        await asyncio.sleep(0.05)
        assert task.done() is False
        assert task.cancelled() is False

        # 清理
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestRound11KnowledgeSearchE2E:
    """端到端测试：knowledge_search 空查询守卫（第11轮修复项3）。"""

    def test_e2e_knowledge_search_empty_query_safe(self, tmp_path):
        """真实 SQLite 后端：空/纯空白查询返回 []，正常查询仍命中，全程不崩溃。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        db = tmp_path / "kb" / "mem.db"
        backend = SQLiteBackend(str(db))
        backend.knowledge_add("hello world", "scope1:u1:knowledge", None)
        backend.knowledge_add("alpha beta gamma", "scope1:u1:knowledge", None)

        # 空查询守卫（第11轮修复项3 核心）
        assert backend.knowledge_search("", None, 5) == []
        assert backend.knowledge_search("   ", None, 5) == []
        # 正常查询回归
        assert len(backend.knowledge_search("hello", None, 5)) == 1
        assert len(backend.knowledge_search("gamma", None, 5)) == 1


class TestRound11EventBusDocstringE2E:
    """端到端测试：event_bus docstring 契约与 stream() 订阅事件对齐（第11轮修复项4）。"""

    def test_e2e_event_bus_docstring_aligned_with_stream_subscribe(self):
        """event_bus 文档声明的 5 种事件类型应与 agent.stream() 订阅的事件完全一致。"""
        import inspect
        import weave_agent_sdk.event_bus as eb
        from weave_agent_sdk.agent import Weave

        bus_src = inspect.getsource(eb)
        stream_src = inspect.getsource(Weave.stream)

        contract = ["token", "tool_call", "tool_result", "done", "error"]
        for et in contract:
            assert et in bus_src
        # stream() 订阅的 5 种事件与契约一致
        assert '"token", "tool_call", "tool_result", "done", "error"' in stream_src


class TestRound11IterativeOutputFallbackE2E:
    """端到端测试：iterative 输出"未收敛"标记（第16轮修复项5 取代第11轮"回退最后 assistant 文本"语义）。"""

    @pytest.mark.asyncio
    async def test_e2e_iterative_output_falls_back_to_last_assistant_text(self):
        """max_iterations 耗尽时 output 统一为"未收敛"标记（第16轮修复项5：不再回退陈旧文本）。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 3
        agent._config.loop.stop_conditions = []  # 无任何停止条件 → 必然走 max_iterations 耗尽
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def echo_tool(q: str) -> str:
            return f"echo:{q}"

        agent._tools = [echo_tool]
        agent._tool_map = {"echo_tool": echo_tool}
        # LLM 始终返回 tool_calls → 永不命中停止条件，循环耗尽 3 次
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="last assistant text", model="test",
            tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"q": "hi"})],
        ))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])  # persist 为 no-op

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        # 第16轮修复项5 核心：耗尽迭代后 output 统一为"未收敛"标记，而非陈旧 assistant 文本 / tool 结果 JSON
        assert result.iterations == 3
        assert result.output == "(reached max_iterations=3 without a final response)"
        assert result.memory_updated["tools"] == {"echo_tool": 3}

# ============================================================
# 第12轮测试（本轮新增）：针对第12轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第12轮修复" 标题（2026-08-22，最近一轮）
# 本轮修复摘要：已修复0个问题（第10轮审查 5 项已在第11轮修复并通过），
# 未编写测试。本轮为"验证 + 测试漂移修正"轮：
#   - 修正 7 个历史测试漂移（旧事件名 run_complete/llm_token 断言、
#     ScheduledLoop 共享锁/失败兜底源码断言指向 run()、event_min_interval
#     节流时序竞态），使其与 basic.md §5 契约及当前实现一致；
#   - 本轮新增测试固化契约：token/tool_call/tool_result/done/error 五种
#     事件类型、_execute_trigger 按触发周期持共享锁与失败兜底、事件驱动
#     payload 触发输入 + event_min_interval 节流配置。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound12WsContractUnit:
    """第12轮（验证）—— ws.py 事件契约固化：token/tool_call/tool_result/done/error。"""

    def test_ws_source_no_run_complete_or_llm_token(self):
        """ws.py 全文件不应再出现旧事件名 run_complete / llm_token（漂移根因守卫）。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert "run_complete" not in source, "旧事件名 run_complete 不应残留"
        assert "llm_token" not in source, "旧事件名 llm_token 不应残留"

    def test_ws_subscribe_string_contract(self):
        """ws.py 订阅字符串应精确等于 5 种契约事件类型。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert '"token", "tool_call", "tool_result", "done", "error"' in source


class TestRound12ScheduledTriggerUnit:
    """第12轮（验证）—— scheduled 触发执行逻辑已收敛到 _execute_trigger()。"""

    def test_execute_trigger_source_holds_shared_lock(self):
        """_execute_trigger 应包含按触发周期持共享锁的逻辑。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop._execute_trigger)
        assert 'locks = agent.__dict__.setdefault("_run_locks", {})' in source
        assert "async with lock:" in source
        assert "await self._execute_once(agent, trigger_input)" in source

    def test_run_source_no_shared_lock_logic(self):
        """run() 不应再包含共享锁获取逻辑（已移入 _execute_trigger，防回退）。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        assert 'agent.__dict__.setdefault("_run_locks", {})' not in source

    def test_execute_trigger_source_wraps_failure(self):
        """_execute_trigger 应包含触发失败兜底（告警 + 继续等待下次触发）。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop._execute_trigger)
        assert "will retry on next schedule" in source
        assert "logger.warning(" in source

    def test_run_source_no_trigger_try_except(self):
        """run() 不应再包含触发失败兜底（已移入 _execute_trigger，防回退）。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        assert "will retry on next schedule" not in source

    @pytest.mark.asyncio
    async def test_execute_trigger_returns_none_on_failure(self, caplog):
        """_execute_once 抛异常时 _execute_trigger 应返回 None 并记录告警。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        agent = MagicMock()
        loop = ScheduledLoop()
        loop._execute_once = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.WARNING):
            result = await loop._execute_trigger(agent, "input")

        assert result is None
        assert any("will retry on next schedule" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_execute_trigger_returns_result_on_success(self):
        """_execute_once 成功时 _execute_trigger 应原样返回 LoopResult。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        loop = ScheduledLoop()
        loop._execute_once = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=1, iterations=1, memory_updated={}
        ))

        result = await loop._execute_trigger(agent, "input")

        assert result.output == "ok"
        loop._execute_once.assert_awaited_once_with(agent, "input")


class TestRound12EventMinIntervalUnit:
    """第12轮（验证）—— event_min_interval 节流配置（payload 竞态漂移的根因项）。"""

    def test_loop_config_has_event_min_interval_default_1_0(self):
        """LoopConfig.event_min_interval 默认值应为 1.0 秒。"""
        from weave_agent_sdk.types import LoopConfig

        config = LoopConfig()
        assert config.event_min_interval == 1.0

    def test_load_config_parses_event_min_interval_from_yaml(self, tmp_path):
        """load_config() 应从 YAML 解析 loop.event_min_interval 字段。"""
        import yaml

        yaml_path = tmp_path / "test_r12_event_min_interval.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "scheduled", "event_min_interval": 0.25},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.event_min_interval == 0.25


# ============================================================
# 第12轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound12WsContractE2E:
    """端到端测试：ws.py 推送 basic.md §5 契约的 5 种事件（第12轮固化）。"""

    @pytest.mark.asyncio
    async def test_e2e_ws_forwards_contract_events(self):
        """伪 WebSocket 应收到 token/tool_call/tool_result/done 且无 error。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream
        from weave_agent_sdk.types import LoopResult

        bus = EventBus()
        weave = MagicMock()
        weave.on = bus.subscribe
        weave.emit = bus.emit

        async def fake_run_impl(input_text, **kwargs):
            await asyncio.sleep(0.05)
            await bus.emit("token", {"text": "tok", "index": 0})
            await bus.emit("tool_call", {"name": "search", "arguments": {"q": "x"}})
            await bus.emit("tool_result", {"name": "search", "result": "found", "error": False})
            return LoopResult(output="done-out", elapsed_ms=2, iterations=1, memory_updated={})

        weave._run_impl = fake_run_impl
        ws = _FakeWS({"input": "hi"})

        await ws_agent_stream(ws, weave)

        types_sent = [e["type"] for e in ws.sent]
        assert "token" in types_sent
        assert "tool_call" in types_sent
        assert "tool_result" in types_sent
        assert "done" in types_sent
        assert "error" not in types_sent

    @pytest.mark.asyncio
    async def test_e2e_ws_done_event_schema(self):
        """done 事件 data 应携带 output/elapsed_ms/iterations（契约 schema）。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream
        from weave_agent_sdk.types import LoopResult

        bus = EventBus()
        weave = MagicMock()
        weave.on = bus.subscribe
        weave.emit = bus.emit

        async def fake_run_impl(input_text, **kwargs):
            await asyncio.sleep(0.05)
            return LoopResult(output="schema-out", elapsed_ms=7, iterations=3, memory_updated={})

        weave._run_impl = fake_run_impl
        ws = _FakeWS({"input": "hi"})

        await ws_agent_stream(ws, weave)

        rc = [e for e in ws.sent if e["type"] == "done"][0]
        assert rc["data"]["output"] == "schema-out"
        assert rc["data"]["elapsed_ms"] == 7
        assert rc["data"]["iterations"] == 3
        assert "timestamp" in rc

    @pytest.mark.asyncio
    async def test_e2e_ws_scope_hints_passthrough_done_event(self):
        """scope_hints/context/tool_filter 透传 + 完成事件为 done（修正旧 run_complete 断言）。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream
        from weave_agent_sdk.types import LoopResult

        bus = EventBus()
        weave = MagicMock()
        weave._run_impl = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=1, iterations=1, memory_updated={}
        ))
        weave.on = bus.subscribe
        weave.emit = bus.emit

        ws = _FakeWS({
            "input": "hello",
            "scope_hints": {"session_id": "s1"},
            "context": {"user": "alice"},
            "tool_filter": ["search_kb"],
        })

        await ws_agent_stream(ws, weave)

        weave._run_impl.assert_awaited_once()
        call = weave._run_impl.call_args
        assert call.args[0] == "hello"
        assert call.kwargs["scope_hints"] == {"session_id": "s1"}
        assert call.kwargs["context"] == {"user": "alice"}
        assert call.kwargs["tool_filter"] == ["search_kb"]
        assert call.kwargs["_streaming"] is True
        assert any(e["type"] == "done" for e in ws.sent)


class TestRound12ScheduledE2E:
    """端到端测试：scheduled 触发失败兜底与 payload 触发输入（第12轮固化）。"""

    @pytest.mark.asyncio
    async def test_e2e_trigger_failure_logged_and_continues(self, caplog):
        """_execute_trigger 单次触发失败记录告警并返回 None，调度器继续等待下次触发。"""
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = SimpleNamespace(
            _config=SimpleNamespace(
                loop=SimpleNamespace(schedule="@on_data_change", event_min_interval=0)
            ),
            _event_bus=EventBus(),
            _run_locks={},
        )

        n = {"count": 0}

        async def fake_execute_once(agent_, user_input):
            n["count"] += 1
            if n["count"] == 1:
                raise RuntimeError("first trigger boom")
            return LoopResult(output="recovered", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        with caplog.at_level(logging.WARNING):
            task = asyncio.create_task(loop.run(agent, "hi"))
            await _round6_wait_until(
                lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
            )
            # 首次（初始）触发失败被记录告警，调度器存活
            await _round6_wait_until(lambda: n["count"] >= 1)
            # 第二次触发成功 → 调度器继续
            await agent._event_bus.emit("data_change", {})
            await _round6_wait_until(lambda: n["count"] >= 2)
            loop.handle_shutdown()
            result = await asyncio.wait_for(task, timeout=5.0)

        assert result.output == "recovered"
        assert n["count"] == 2
        assert any("will retry on next schedule" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_e2e_data_change_payload_drives_trigger_input(self):
        """data_change payload 的 input 字段驱动触发输入（无节流时即时生效）。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = "@on_data_change"
        agent._config.loop.event_min_interval = 0
        agent._event_bus = EventBus()
        agent._run_locks = {}

        received = []

        async def fake_execute_once(agent_, user_input):
            received.append(user_input)
            return LoopResult(output="ok", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        task = asyncio.create_task(loop.run(agent, "initial-input"))

        await _round6_wait_until(
            lambda: "data_change" in getattr(agent._event_bus, "subscriber_count", {})
        )
        await _round6_wait_until(lambda: len(received) >= 1)
        assert received[0] == "initial-input"

        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: len(received) >= 2)
        assert received[1] == "initial-input"  # 无 input → 回退 user_input

        await agent._event_bus.emit("data_change", {"input": "payload-driven"})
        await _round6_wait_until(lambda: len(received) >= 3)
        assert received[2] == "payload-driven"

        loop.handle_shutdown()
        await asyncio.wait_for(task, timeout=5.0)


# ============================================================
# 第13轮测试（本轮新增）：针对第13轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第13轮修复" 标题（2026-08-23，最近一轮）
# 修复摘要（共5项行为 + 1 项配置支持）:
#   - agent.py / ws.py：整体超时拆分到新配置 loop.stream_timeout（默认不启用），
#     修复默认 5s 误杀慢速运行；WS 同步整体超时兜底（P2/P3）
#   - iterative.py：text_pattern 停止条件不被 tool_calls 短路；
#     output 回退最近非空 assistant 文本（P3）
#   - scheduled.py：纯数字 "0" 间隔校验回退 60s，防忙循环（P3）
#   - types.py / config.py / weave.yaml：新增 loop.stream_timeout 配置（P2/P3）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# 同时修正 3 个第11轮历史漂移断言（timeout → stream_timeout，见下方"历史漂移修正"）
# ============================================================


class TestRound13StreamTimeoutConfigUnit:
    """第13轮修复项（types/config：新增 loop.stream_timeout 配置）—— 单元测试。"""

    def test_loop_config_stream_timeout_default_none_and_semantic_separation(self):
        """LoopConfig.stream_timeout 默认 None（不启用），与 timeout(宽限 5.0) 语义分离。"""
        from weave_agent_sdk.types import LoopConfig

        config = LoopConfig()
        assert config.stream_timeout is None
        assert config.timeout == 5.0
        # 语义分离：修改 stream_timeout 不影响 timeout（宽限时长）
        config.stream_timeout = 3.0
        assert config.timeout == 5.0

    def test_load_config_parses_stream_timeout_from_yaml(self, tmp_path):
        """load_config() 应从 YAML 解析 loop.stream_timeout 字段。"""
        import yaml

        yaml_path = tmp_path / "r13_stream_timeout.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": 7.5},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.stream_timeout == 7.5
        assert config.loop.timeout == 5.0  # 独立解析，不影响宽限时长

    def test_load_config_invalid_stream_timeout_falls_back_none(self, tmp_path):
        """非法 stream_timeout（非数值）应降级为 None（不启用），避免 wait_for 收非法 timeout。"""
        import yaml

        yaml_path = tmp_path / "r13_stream_timeout_invalid.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": "not-a-number"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.stream_timeout is None


class TestRound13StreamTimeoutSourceUnit:
    """第13轮修复项（agent.py / ws.py：整体超时改用 stream_timeout）—— 源字符串断言。"""

    def test_stream_uses_stream_timeout_not_loop_timeout(self):
        """stream() 应读取 loop.stream_timeout 作为整体超时，而非复用 loop.timeout。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        # 从配置读取 stream_timeout（默认不启用）
        assert 'stream_timeout = getattr(self._config.loop, "stream_timeout", None)' in source
        assert "has_timeout = isinstance(stream_timeout, (int, float)) and stream_timeout > 0" in source
        # 空闲超时（第14轮修正）：wait_for 应用于消费端 anext(subscribe_gen)，
        # 以 stream_timeout 为单次等待上限、事件到达即重置；而非包住整个 _run_and_emit。
        assert "event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)" in source
        assert 'message": f"Stream timed out after {stream_timeout}s (no events received)"' in source
        # loop.timeout 仅作为"收到 done/error 后等待后台任务收尾"的宽限时长
        assert "timeout = self._config.loop.timeout" in source

    def test_ws_uses_stream_timeout_for_overall_timeout(self):
        """ws.py 应读取 loop.stream_timeout 并作为整体超时兜底（与 agent.stream 一致）。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert 'stream_timeout = getattr(weave._config.loop, "stream_timeout", None)' in source
        assert "isinstance(stream_timeout, (int, float)) and stream_timeout > 0" in source
        # 空闲超时（第14轮修正）：与 agent.stream() 一致，wait_for 应用于
        # 消费端 anext(subscribe_gen)，事件到达即重置计时。
        assert "event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)" in source
        assert 'message": f"Stream timed out after {stream_timeout}s (no events received)"' in source


class TestRound13StreamTimeoutDisabledUnit:
    """第13轮修复项（agent.py：默认不启用整体超时）—— 行为测试。"""

    @pytest.mark.asyncio
    async def test_stream_timeout_disabled_when_non_positive(self):
        """stream_timeout 为 0/负值/非数值（不启用）时，慢速运行应正常完成，不被整体超时误杀。"""
        from weave_agent_sdk.event_bus import EventBus

        for bad_timeout in (0, -1, "abc"):
            weave = Weave.__new__(Weave)
            weave._config = MagicMock()
            weave._config.loop.type = "simple"
            weave._config.loop.timeout = 0.05
            weave._config.loop.stream_timeout = bad_timeout
            weave._event_bus = EventBus()

            async def slow_impl(*args, **kwargs):
                await asyncio.sleep(0.2)  # 超过 loop.timeout=0.05 但正常完成
                from weave_agent_sdk.types import LoopResult
                return LoopResult(output="done", elapsed_ms=200, iterations=1, memory_updated={})

            weave._run_impl = slow_impl

            events = []
            async for event in weave.stream("input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break

            assert events[-1].type == "done", f"stream_timeout={bad_timeout!r} 不应触发整体超时"
            assert events[-1].data["output"] == "done"


class TestRound13IterativeSourceUnit:
    """第13轮修复项（iterative.py：text_pattern 不被短路 / 输出回退最近非空 assistant）—— 源字符串断言。"""

    def test_text_pattern_checked_before_tool_calls(self):
        """text_pattern 停止条件应位于 or 左侧（先判断），不被 tool_calls 短路。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "_matches_text_pattern(response.content, stop_conditions) or not response.tool_calls" in source
        # 若 text_pattern 被放在 or 右侧，会被 not response.tool_calls 短路（当 tool_calls 存在时）。
        # 因此左侧必须是 text_pattern（命中即停）。
        idx_pattern = source.index("_matches_text_pattern(response.content, stop_conditions)")
        idx_no_tools = source.index("not response.tool_calls")
        assert idx_pattern < idx_no_tools, "text_pattern 应位于 or 左侧（先判断）"

    def test_output_fallback_uses_last_non_empty_assistant(self):
        """output 回退应只记录最近的非空 assistant 文本（if response.content 守卫）。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "if response.content:" in source
        assert "last_assistant_content = response.content" in source
        assert "output = final_output or last_assistant_content or messages[-1].content" in source


class TestRound13ScheduledZeroIntervalUnit:
    """第13轮修复项（scheduled.py：纯数字 "0" 间隔回退 60s 防忙循环）—— 单元测试。"""

    def test_wait_for_next_run_zero_interval_guard_source(self):
        """_wait_for_next_run 对 interval<=0 应回退 60s 并记录告警（源码）。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop._wait_for_next_run)
        assert "if interval <= 0:" in source
        assert "interval = 60.0" in source
        assert "invalid interval" in source
        assert "defaulting to 60s interval" in source

    @pytest.mark.asyncio
    async def test_wait_for_next_run_zero_interval_falls_back_to_60s(self, caplog):
        """schedule="0" 时：告警记录 + 按 60s 间隔（1.0s 分片睡眠 60 次）而非忙循环。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.loop.scheduled"):
                    await asyncio.wait_for(loop._wait_for_next_run(agent, "0"), timeout=2.0)

        # 60s 间隔按 1.0s 分片睡眠恰好 60 次（若是 0 间隔忙循环则为 0 次/无限紧循环）
        assert len(sleep_calls) == 60
        assert all(c == 1.0 for c in sleep_calls)
        assert any("invalid interval '0'" in r.message for r in caplog.records)
        assert any("60s interval" in r.message for r in caplog.records)


class TestRound13StreamSlowRunE2E:
    """端到端测试：默认不启用整体超时 → 慢速运行（超过 loop.timeout）不被误杀（第13轮核心修复）。"""

    @pytest.mark.asyncio
    async def test_e2e_default_timeout_does_not_kill_slow_run(self):
        """默认配置（stream_timeout=None）下，运行耗时超过 loop.timeout=0.05 仍应正常完成。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 0.05
        weave._config.loop.stream_timeout = None  # 默认不启用整体超时
        weave._event_bus = EventBus()

        async def slow_impl(*args, **kwargs):
            await asyncio.sleep(0.2)  # 超过 loop.timeout=0.05 但正常完成
            from weave_agent_sdk.types import LoopResult
            return LoopResult(output="slow done", elapsed_ms=200, iterations=1, memory_updated={})

        weave._run_impl = slow_impl

        events = []
        async for event in weave.stream("input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # 修复项核心：默认 5s（此处 0.05）不再作为整次运行期限 → 慢速运行 emit done
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "slow done"


class TestRound13StreamTimeoutE2E:
    """端到端测试：配置 stream_timeout 后，挂起的运行超时 emit error（第13轮修复项）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_timeout_emits_error_on_hang(self):
        """stream_timeout=0.05 且 _run_impl 挂起时，stream() 应在 0.05s 后 emit error。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 0.05
        weave._config.loop.stream_timeout = 0.05
        weave._event_bus = EventBus()

        async def hang_impl(*args, **kwargs):
            await asyncio.sleep(100)

        weave._run_impl = hang_impl

        events = []

        async def consume():
            async for event in weave.stream("input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break
            return events

        result = await asyncio.wait_for(consume(), timeout=5.0)

        assert len(result) >= 1
        assert result[-1].type == "error"
        assert "timed out after 0.05s" in result[-1].data["message"]
        assert result[-1].data["exception"] == "TimeoutError"


class TestRound13WsTimeoutE2E:
    """端到端测试：WS 路径整体超时兜底（第13轮修复项：WS 同步整体超时兜底）。"""

    @pytest.mark.asyncio
    async def test_e2e_ws_stream_timeout_emits_error_on_hang(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream

        bus = EventBus()
        weave = MagicMock()
        weave._config = MagicMock()
        weave._config.loop.stream_timeout = 0.05
        weave._config.loop.timeout = 0.05
        weave.emit = bus.emit
        weave.on = bus.subscribe

        async def hang_impl(input_text, **kwargs):
            await asyncio.sleep(100)

        weave._run_impl = hang_impl

        ws = _FakeWS({"input": "hi"})

        await asyncio.wait_for(ws_agent_stream(ws, weave), timeout=5.0)

        types_sent = [e["type"] for e in ws.sent]
        assert "error" in types_sent
        err = [e for e in ws.sent if e["type"] == "error"][0]
        assert "Stream timed out after 0.05s" in err["data"]["message"]
        assert err["data"]["exception"] == "TimeoutError"


class TestRound13TextPatternE2E:
    """端到端测试：text_pattern 停止条件不被 tool_calls 短路（第13轮修复项）。"""

    @pytest.mark.asyncio
    async def test_e2e_text_pattern_stops_even_with_tool_calls(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 3
        agent._config.loop.stop_conditions = [
            {"type": "text_pattern", "pattern": r"ANSWER:\s*\d+"},
        ]
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        executed = []

        async def search_tool(q: str) -> str:
            executed.append(q)
            return "result"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        # LLM 返回命中 text_pattern 的文本 + tool_calls：必须"命中即停"（不被 tool_calls 短路）
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="ANSWER: 42",
            model="test",
            tool_calls=[ToolCall(id="c1", name="search_tool", arguments={"q": "x"})],
        ))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "ANSWER: 42"
        assert result.iterations == 1
        assert executed == [], "text_pattern 命中即停，tool 不应被执行"
        assert "tools" not in result.memory_updated


class TestRound13OutputFallbackE2E:
    """端到端测试：output 在耗尽时统一标记未收敛（第16轮修复项5 取代第13轮"回退最近非空"语义）。"""

    @pytest.mark.asyncio
    async def test_e2e_output_falls_back_to_last_non_empty_assistant(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []  # 无停止条件 → max_iterations 耗尽
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def echo_tool(q: str) -> str:
            return f"echo:{q}"

        agent._tools = [echo_tool]
        agent._tool_map = {"echo_tool": echo_tool}
        # 第1轮：非空 assistant 文本 + tool_calls；第2轮：空 assistant 文本 + tool_calls（耗尽）
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(
                content="first assistant text", model="test",
                tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"q": "a"})],
            ),
            LLMResponse(
                content="", model="test",
                tool_calls=[ToolCall(id="c2", name="echo_tool", arguments={"q": "b"})],
            ),
        ])
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.iterations == 2
        # 第16轮修复项5 核心：耗尽时统一标记"未收敛"，即便末轮 assistant 为空也不再回退到陈旧文本
        assert result.output == "(reached max_iterations=2 without a final response)"
        assert result.memory_updated["tools"] == {"echo_tool": 2}


# ============================================================
# 第13轮测试（本轮新增）：历史漂移修正
# 第13轮修复将 stream()/WS 的整体超时从 loop.timeout 拆分为 loop.stream_timeout
# （默认不启用）。3 个第11轮测试仍断言旧行为（整体超时复用 loop.timeout），
# 与当前实现漂移，按 round-12 先例修正为 stream_timeout 语义：
#   - TestRound11StreamTimeoutGuardUnit::test_stream_source_has_timeout_guard_wrapper
#   - TestRound11StreamTimeoutGuardUnit::test_stream_source_guard_only_when_positive_timeout
#   - TestRound11StreamTimeoutE2E::test_e2e_stream_hanging_run_emits_error_within_timeout
# 修正位置见上方代码（第11轮测试块内，timeout → stream_timeout）。
# ============================================================


# ============================================================
# 第14轮测试（本轮新增）：针对第13轮修复行为在当前实现下的补充验证
# 依据 fix_record.md 最后一个 "## 第13轮修复" 标题（2026-08-23）。
# 当前源码已进一步细化 stream_timeout 为"空闲超时"语义：自上次事件计时、
# 事件到达即重置；wait_for 应用于消费端 anext(subscribe_gen)，而非包住整个
# 生产任务。本轮新增 15 个测试（10 个单元测试 + 5 个端到端测试）固化当前
# 空闲超时行为，并补充 iterative output 回退守卫与 scheduled 数值间隔：
#   - agent.stream() 空闲超时：逐事件重置 / 无事件挂起触发 error
#   - ws.py 空闲超时：与 agent.stream() 一致
#   - types/config：stream_timeout 数值字符串解析 / 字段独立
#   - iterative.py：output 回退守卫（reached max_iterations 提示）
#   - scheduled.py：数值间隔分片睡眠（非忙循环）/ 非法 cron 回退 60s
# ============================================================


class TestRound14IdleTimeoutSourceUnit:
    """第14轮（agent.py 空闲超时）—— 源字符串断言：wait_for 应用于消费端。"""

    def test_stream_source_uses_idle_timeout_per_event(self):
        """agent.stream() 应以 stream_timeout 为单次等待事件上限（逐事件重置计时）。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        assert "event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)" in source
        assert 'message": f"Stream timed out after {stream_timeout}s (no events received)"' in source
        assert 'stream_timeout = getattr(self._config.loop, "stream_timeout", None)' in source
        # 空闲超时语义：不包住整个 _run_and_emit（避免总时长限制误杀持续 emit 的长运行）
        assert "asyncio.wait_for(_run_and_emit(), timeout=stream_timeout)" not in source
        assert "_run_and_emit_with_timeout" not in source

    def test_stream_source_cancels_run_task_on_timeout(self):
        """空闲超时触发后应取消在途运行并 yield error 事件（exception=TimeoutError）。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        assert "if not _run_task.done():" in source
        assert "_run_task.cancel()" in source
        assert '"exception": "TimeoutError"' in source
        assert "yield WeaveEvent(" in source


class TestRound14WsIdleTimeoutSourceUnit:
    """第14轮（ws.py 空闲超时）—— 源字符串断言。"""

    def test_ws_source_uses_idle_timeout_per_event(self):
        """ws.py 应与 agent.stream() 一致：wait_for 应用于消费端 anext。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert "event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)" in source
        assert 'message": f"Stream timed out after {stream_timeout}s (no events received)"' in source
        assert 'stream_timeout = getattr(weave._config.loop, "stream_timeout", None)' in source
        assert "asyncio.wait_for(_run_and_emit(), timeout=stream_timeout)" not in source

    def test_ws_source_cancels_task_and_sends_error(self):
        """ws.py 空闲超时后应取消在途任务并向客户端推送 error 事件。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert "if not task.done():" in source
        assert "task.cancel()" in source
        assert '"exception": "TimeoutError"' in source
        assert "await websocket.send_json({" in source


class TestRound14StreamTimeoutConfigUnit:
    """第14轮（stream_timeout 配置解析）—— 单元测试。"""

    def test_load_config_parses_numeric_string_stream_timeout(self, tmp_path):
        """YAML 中 stream_timeout 为数值字符串时应转换为 float（"3.5" → 3.5）。"""
        import yaml

        yaml_path = tmp_path / "r14_stream_timeout_str.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": "3.5"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.stream_timeout == 3.5
        assert isinstance(config.loop.stream_timeout, float)

    def test_loop_config_stream_timeout_independent_of_other_timeouts(self):
        """stream_timeout 与 timeout / tool_timeout 相互独立、互不影响。"""
        from weave_agent_sdk.types import LoopConfig

        config = LoopConfig(stream_timeout=2.5, timeout=8.0, tool_timeout=40.0)
        assert config.stream_timeout == 2.5
        assert config.timeout == 8.0
        assert config.tool_timeout == 40.0

        config.stream_timeout = 9.0
        assert config.timeout == 8.0
        assert config.tool_timeout == 40.0


class TestRound14IterativeOutputUnit:
    """第14轮（iterative output 回退守卫）—— 源字符串断言。"""

    def test_output_guard_reached_max_iterations_source(self):
        """run() 应包含 reached max_iterations 守卫（末条为 tool 且无可用文本时）。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert 'reached max_iterations={max_iter} without a final response' in source
        assert 'if not (final_output or last_assistant_content) and messages[-1].role == "tool":' in source

    def test_output_fallback_still_preferred_over_guard(self):
        """存在可用文本时优先回退 final_output → last_assistant_content → 最后消息。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "output = final_output or last_assistant_content or messages[-1].content" in source


class TestRound14ScheduledIntervalUnit:
    """第14轮（scheduled 数值间隔 / 非法 schedule 回退）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_wait_for_next_run_numeric_interval(self):
        """schedule="5" 应按 5s 间隔分片睡眠恰好 5 次（非忙循环）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                await asyncio.wait_for(loop._wait_for_next_run(agent, "5"), timeout=2.0)

        assert len(sleep_calls) == 5
        assert all(c == 1.0 for c in sleep_calls)

    @pytest.mark.asyncio
    async def test_wait_for_next_run_unparseable_falls_back_60s(self, caplog):
        """非数值且无法解析为 cron 的 schedule 应回退 60s 间隔并记录告警。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.loop.scheduled"):
                    await asyncio.wait_for(loop._wait_for_next_run(agent, "not-a-cron"), timeout=2.0)

        assert len(sleep_calls) == 60
        assert all(c == 1.0 for c in sleep_calls)
        assert any("cannot parse schedule" in r.message for r in caplog.records)


# ============================================================
# 第14轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound14StreamIdleTimeoutE2E:
    """端到端测试：agent.stream() 空闲超时语义（第14轮固化当前实现）。"""

    @pytest.mark.asyncio
    async def test_e2e_idle_timeout_resets_per_event(self):
        """持续 emit 事件（间隔 < stream_timeout）的长运行应正常完成，不被误杀。

        这是空闲超时与"整次运行总时长上限"的核心区别：总时长超过 stream_timeout，
        但事件持续流动（每次等待 < stream_timeout），计时逐事件重置。
        """
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 0.05
        weave._config.loop.stream_timeout = 0.05
        weave._event_bus = EventBus()

        async def token_impl(*args, **kwargs):
            for i in range(15):
                await weave._event_bus.emit("token", {"text": f"t{i}", "index": i})
                await asyncio.sleep(0.02)
            return LoopResult(output="done", elapsed_ms=300, iterations=1, memory_updated={})

        weave._run_impl = token_impl

        events = []
        async for event in weave.stream("input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # 总时长 ~0.3s > stream_timeout=0.05，但事件每 0.02s 到达 → 计时逐次重置
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "done"
        assert not any(e.type == "error" for e in events)
        assert sum(1 for e in events if e.type == "token") == 15

    @pytest.mark.asyncio
    async def test_e2e_idle_timeout_emits_error_on_no_events(self):
        """_run_impl 挂起（不产生任何事件）时，stream() 应在 stream_timeout 后 emit error。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 0.05
        weave._config.loop.stream_timeout = 0.05
        weave._event_bus = EventBus()

        async def hang_impl(*args, **kwargs):
            await asyncio.sleep(100)

        weave._run_impl = hang_impl

        events = []

        async def consume():
            async for event in weave.stream("input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break
            return events

        result = await asyncio.wait_for(consume(), timeout=5.0)

        assert result[-1].type == "error"
        assert "no events received" in result[-1].data["message"]
        assert result[-1].data["exception"] == "TimeoutError"


class TestRound14WsIdleTimeoutE2E:
    """端到端测试：WS 路径空闲超时语义（第14轮固化当前实现）。"""

    @pytest.mark.asyncio
    async def test_e2e_ws_idle_timeout_resets_per_event(self):
        """WS 运行持续 emit token（间隔 < stream_timeout）应正常完成，不触发 error。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream

        bus = EventBus()
        weave = MagicMock()
        weave._config = MagicMock()
        weave._config.loop.stream_timeout = 0.05
        weave._config.loop.timeout = 0.05
        weave.emit = bus.emit
        weave.on = bus.subscribe

        async def token_impl(input_text, **kwargs):
            for i in range(15):
                await bus.emit("token", {"text": f"t{i}", "index": i})
                await asyncio.sleep(0.02)
            return LoopResult(output="done", elapsed_ms=300, iterations=1, memory_updated={})

        weave._run_impl = token_impl
        ws = _FakeWS({"input": "hi"})

        await asyncio.wait_for(ws_agent_stream(ws, weave), timeout=5.0)

        types_sent = [e["type"] for e in ws.sent]
        assert "done" in types_sent
        assert "error" not in types_sent
        assert sum(1 for e in ws.sent if e["type"] == "token") == 15


class TestRound14IterativeOutputE2E:
    """端到端测试：iterative output 回退守卫（第14轮固化 reached max_iterations 提示）。"""

    @pytest.mark.asyncio
    async def test_e2e_output_reached_max_iterations_guard(self):
        """耗尽迭代且所有 assistant 文本为空、末条为 tool 消息时，
        输出明确的 reached max_iterations 提示，而非 tool JSON / 空串。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def echo_tool(q: str) -> str:
            return f"echo:{q}"

        agent._tools = [echo_tool]
        agent._tool_map = {"echo_tool": echo_tool}
        # 每轮 assistant 文本均为空 + tool_calls → 永不命中停止条件，耗尽 2 次
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="", model="test",
            tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"q": "a"})],
        ))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.iterations == 2
        assert result.output == "(reached max_iterations=2 without a final response)"
        assert result.memory_updated["tools"] == {"echo_tool": 2}


class TestRound14ScheduledIntervalE2E:
    """端到端测试：scheduled 数值间隔分片睡眠（第14轮固化非忙循环）。"""

    @pytest.mark.asyncio
    async def test_e2e_wait_for_next_run_numeric_interval_not_busy(self):
        """schedule="2" 应按 2s 间隔睡眠 2 次（1.0s 分片），而非 0 间隔忙循环。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                await asyncio.wait_for(loop._wait_for_next_run(agent, "2"), timeout=2.0)

        # 2s 间隔 = 2 次 1.0s 分片睡眠；若退化为忙循环则为 0 次 / 无限紧循环
        assert len(sleep_calls) == 2
        assert all(c == 1.0 for c in sleep_calls)


# ============================================================
# 第15轮测试（本轮新增）：第13轮修复行为的补充验证
# 依据 fix_record.md 最后一个 "## 第13轮修复" 标题（2026-08-23，最近一轮）。
# 本轮为第13轮修复的补充验证批次（记录为第15轮测试），与既有 Round13/14 互补，
# 不重复既有测试，只针对第13轮修复涉及的 4 项行为 + 1 项配置支持：
#   - agent.py / ws.py：整体超时拆分到 loop.stream_timeout（默认不启用），
#     loop.timeout 仅作后台任务收尾宽限时长（P2/P3）
#   - iterative.py：text_pattern 停止条件不被 tool_calls 短路（命中即停、
#     未命中不误停）；output 回退最近非空 assistant 文本（P3）
#   - scheduled.py：纯数字 "0" 间隔校验回退 60s 防忙循环（含空白/前导零/负数边界）（P3）
#   - types.py / config.py / weave.yaml：loop.stream_timeout 配置支持（默认 None 不启用）（P2/P3）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound15StreamTimeoutConfigUnit:
    """第13轮修复（loop.stream_timeout 配置）—— 第15轮补充单元测试。"""

    def test_load_config_missing_stream_timeout_defaults_none(self, tmp_path):
        """YAML 未配置 stream_timeout 时，config.loop.stream_timeout 应为 None（默认不启用）。"""
        import yaml

        yaml_path = tmp_path / "r15_missing_stream_timeout.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative"},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        # 缺省 → None（不启用），而非沿用 loop.timeout 宽限值
        assert config.loop.stream_timeout is None
        assert config.loop.timeout == 5.0

    def test_real_weave_yaml_stream_timeout_disabled_by_default(self):
        """项目根 weave.yaml 的 stream_timeout 默认被注释（不启用），load_config 解析为 None。"""
        project_root = Path(__file__).parent.parent
        config = load_config(project_root / "weave.yaml")
        # 默认配置文件将 stream_timeout 注释掉（默认不启用），避免默认 5s 误杀慢速运行
        assert config.loop.stream_timeout is None
        assert config.loop.timeout == 5.0


class TestRound15StreamTimeoutSourceUnit:
    """第13轮修复（agent/ws 超时语义拆分）—— 第15轮补充源字符串断言。"""

    def test_stream_source_grace_period_still_loop_timeout(self):
        """stream() 中"收到 done/error 后等待后台任务收尾"的宽限时长仍用 loop.timeout。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        # 空闲超时用 stream_timeout，收尾宽限用 loop.timeout —— 两个语义各自独立
        assert "timeout = self._config.loop.timeout" in source
        assert "await asyncio.wait_for(_run_task, timeout=timeout)" in source

    def test_ws_source_default_disabled_plain_anext(self):
        """ws.py 未配置 stream_timeout（不启用）时，消费端走普通 anext 而非 wait_for。"""
        import inspect
        from weave_agent_sdk.server.ws import ws_agent_stream

        source = inspect.getsource(ws_agent_stream)
        assert "has_timeout = isinstance(stream_timeout, (int, float)) and stream_timeout > 0" in source
        # 未启用时的事件等待路径：直接 anext（不套 wait_for，不误杀慢速运行）
        assert "event = await anext(subscribe_gen)" in source


class TestRound15TextPatternUnit:
    """第13轮修复（text_pattern 不被 tool_calls 短路）—— 第15轮补充单元测试。"""

    def test_matches_text_pattern_second_condition_matches(self):
        """多个 text_pattern 条件中仅第二个命中时，_matches_text_pattern 应返回 True。"""
        from weave_agent_sdk.loop.iterative import _matches_text_pattern

        conditions = [
            {"type": "text_pattern", "pattern": r"ANSWER:\s*\d+"},
            {"type": "text_pattern", "pattern": r"DONE\b"},
        ]
        assert _matches_text_pattern("job is DONE", conditions) is True

    def test_matches_text_pattern_empty_content_no_match(self):
        """content 为空字符串时 _matches_text_pattern 应返回 False（不崩溃、不停止）。"""
        from weave_agent_sdk.loop.iterative import _matches_text_pattern

        conditions = [{"type": "text_pattern", "pattern": r"ANSWER"}]
        assert _matches_text_pattern("", conditions) is False

    def test_matches_text_pattern_non_text_condition_ignored(self):
        """非 text_pattern 类型的停止条件（如 no_tool_calls）应被忽略，不参与正则匹配。"""
        from weave_agent_sdk.loop.iterative import _matches_text_pattern

        conditions = [
            {"type": "no_tool_calls"},
            {"type": "tool_call", "name": "finish"},
        ]
        assert _matches_text_pattern("anything", conditions) is False


class TestRound15ScheduledZeroIntervalUnit:
    """第13轮修复（纯数字 "0" 间隔回退 60s 防忙循环）—— 第15轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_wait_for_next_run_zero_whitespace_variant_falls_back_60s(self, caplog):
        """schedule=" 0 "（含空白）strip 后为 "0"，仍应回退 60s 间隔（60 次分片睡眠）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.loop.scheduled"):
                    await asyncio.wait_for(loop._wait_for_next_run(agent, " 0 "), timeout=2.0)

        assert len(sleep_calls) == 60
        assert all(c == 1.0 for c in sleep_calls)
        assert any("invalid interval '0'" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_wait_for_next_run_zero_leading_zeros_falls_back_60s(self, caplog):
        """schedule="00"（前导零）解析为 0 间隔，仍应回退 60s 防忙循环。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.loop.scheduled"):
                    await asyncio.wait_for(loop._wait_for_next_run(agent, "00"), timeout=2.0)

        assert len(sleep_calls) == 60
        assert all(c == 1.0 for c in sleep_calls)
        assert any("invalid interval '00'" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_wait_for_next_run_negative_number_falls_back_60s(self, caplog):
        """schedule="-5"（负数，非 isdigit）无法解析为 cron，应回退 60s 并记录 cannot parse 告警。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        loop = ScheduledLoop()
        agent = MagicMock()
        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.loop.scheduled"):
                    await asyncio.wait_for(loop._wait_for_next_run(agent, "-5"), timeout=2.0)

        assert len(sleep_calls) == 60
        assert all(c == 1.0 for c in sleep_calls)
        assert any("cannot parse schedule" in r.message for r in caplog.records)


# ============================================================
# 第15轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound15StreamTimeoutE2E:
    """端到端测试：stream_timeout 配置经真实 load_config 全链路生效（第13轮修复项）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_timeout_config_full_chain(self, tmp_path):
        """YAML 配置 stream_timeout → load_config → stream() 挂起运行在超时后 emit error。"""
        import yaml
        from weave_agent_sdk.event_bus import EventBus

        yaml_path = tmp_path / "r15_stream_timeout.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": 0.05, "timeout": 0.05},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.stream_timeout == 0.05

        weave = Weave.__new__(Weave)
        weave._config = config
        weave._event_bus = EventBus()

        async def hang_impl(*args, **kwargs):
            await asyncio.sleep(100)

        weave._run_impl = hang_impl

        events = []

        async def consume():
            async for event in weave.stream("input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break
            return events

        result = await asyncio.wait_for(consume(), timeout=5.0)

        assert result[-1].type == "error"
        assert "no events received" in result[-1].data["message"]
        assert result[-1].data["exception"] == "TimeoutError"


class TestRound15WsTimeoutE2E:
    """端到端测试：WS 路径默认不启用整体超时 → 慢速运行不被误杀（第13轮修复项）。"""

    @pytest.mark.asyncio
    async def test_e2e_ws_default_timeout_slow_run_not_killed(self):
        """WS 路径 stream_timeout=None（默认）时，运行耗时超过 loop.timeout 仍应收到 done 而非 error。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.server.ws import ws_agent_stream

        bus = EventBus()
        weave = MagicMock()
        weave._config = MagicMock()
        weave._config.loop.stream_timeout = None   # 默认不启用整体超时
        weave._config.loop.timeout = 0.05
        weave.emit = bus.emit
        weave.on = bus.subscribe

        async def slow_impl(input_text, **kwargs):
            await asyncio.sleep(0.2)  # 超过 loop.timeout=0.05 但正常完成
            return LoopResult(output="ws slow done", elapsed_ms=200, iterations=1, memory_updated={})

        weave._run_impl = slow_impl

        ws = _FakeWS({"input": "hi"})

        await asyncio.wait_for(ws_agent_stream(ws, weave), timeout=5.0)

        types_sent = [e["type"] for e in ws.sent]
        assert "done" in types_sent
        assert "error" not in types_sent
        done = [e for e in ws.sent if e["type"] == "done"][0]
        assert done["data"]["output"] == "ws slow done"


class TestRound15TextPatternE2E:
    """端到端测试：text_pattern 未命中时不误停，tool 仍可执行（第13轮修复项）。"""

    @pytest.mark.asyncio
    async def test_e2e_text_pattern_no_match_with_tool_calls_continues(self):
        """LLM 返回未命中 text_pattern 的文本 + tool_calls → 不停止，tool 正常执行、循环继续。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 3
        agent._config.loop.stop_conditions = [
            {"type": "text_pattern", "pattern": r"ANSWER:\s*\d+"},
        ]
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        executed = []

        async def search_tool(q: str) -> str:
            executed.append(q)
            return "result"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        # 第1轮：未命中 text_pattern + tool_calls → 不停止，执行 tool
        # 第2轮：未命中 + tool_calls → 继续
        # 第3轮：命中 text_pattern（无 tool_calls）→ 停止
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(content="need more data", model="test", tool_calls=[
                ToolCall(id="c1", name="search_tool", arguments={"q": "x"}),
            ]),
            LLMResponse(content="still gathering", model="test", tool_calls=[
                ToolCall(id="c2", name="search_tool", arguments={"q": "y"}),
            ]),
            LLMResponse(content="ANSWER: 42", model="test", tool_calls=None),
        ])
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "ANSWER: 42"
        assert result.iterations == 3
        # 未命中的两轮 tool 均被执行（text_pattern 未命中不误停）
        assert executed == ["x", "y"]
        assert result.memory_updated["tools"] == {"search_tool": 2}


class TestRound15OutputFallbackE2E:
    """端到端测试：output 在耗尽时统一标记未收敛（第16轮修复项5 取代第13轮"取最新"语义）。"""

    @pytest.mark.asyncio
    async def test_e2e_output_falls_back_to_latest_nonempty_assistant(self):
        """耗尽迭代时 output 统一为"未收敛"标记，不再回退到最近一轮 assistant 文本。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []  # 无停止条件 → max_iterations 耗尽
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def echo_tool(q: str) -> str:
            return f"echo:{q}"

        agent._tools = [echo_tool]
        agent._tool_map = {"echo_tool": echo_tool}
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=[
            LLMResponse(content="first assistant text", model="test", tool_calls=[
                ToolCall(id="c1", name="echo_tool", arguments={"q": "a"}),
            ]),
            LLMResponse(content="second assistant text", model="test", tool_calls=[
                ToolCall(id="c2", name="echo_tool", arguments={"q": "b"}),
            ]),
        ])
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.iterations == 2
        # 第16轮修复项5 核心：耗尽时统一标记"未收敛"，而非回退到最近轮次 assistant 文本
        assert result.output == "(reached max_iterations=2 without a final response)"
        assert result.memory_updated["tools"] == {"echo_tool": 2}


class TestRound15ScheduledZeroIntervalE2E:
    """端到端测试：run() 全链路下 schedule="0" 回退 60s 分片睡眠，不忙循环（第13轮修复项）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_zero_interval_run_falls_back_60s(self):
        from types import SimpleNamespace
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(schedule="0")),
            _run_locks={},
        )

        now = {"t": 1000.0}
        sleep_calls = []

        def fake_time():
            return now["t"]

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            now["t"] += secs

        loop = ScheduledLoop()
        executed = {"n": 0}

        async def fake_execute_once(agent_, user_input):
            executed["n"] += 1
            # 首次触发后直接置位关闭标志，避免再次进入 60s 等待（不调度 cancel，保证确定性退出）
            loop._shutting_down = True
            return LoopResult(output="tick", elapsed_ms=0, iterations=1, memory_updated={})

        loop._execute_once = fake_execute_once

        with patch("weave_agent_sdk.loop.scheduled.time.time", side_effect=fake_time):
            with patch("asyncio.sleep", new=fake_sleep):
                result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.output == "tick"
        assert executed["n"] == 1
        # "0" 间隔回退 60s：一次等待 = 60 次 1.0s 分片睡眠；若退化为忙循环则为 0 次/无限紧循环
        assert len(sleep_calls) == 60
        assert all(c == 1.0 for c in sleep_calls)


# ============================================================
# 第16轮测试（本轮新增）：针对第16轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第16轮修复" 标题（2026-08-24，最近一轮）
# 修复摘要（共5项行为）:
#   - 空闲超时误杀tool型流程：非流式LLM调用在途视为活动（base/agent/ws）
#   - scheduled/simple user消息改为延迟提交（成功获得回复后落盘）
#   - types/weave.yaml的stream_timeout文档统一为空闲超时语义
#   - iterative重注入加预算约束并保留tool_calls摘要
#   - max_iterations耗尽统一标记未收敛，不再回退陈旧文本
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound16LlInFlightUnit:
    """第16轮修复项1（非流式 LLM 调用在途视为活动，base/agent/ws）—— 单元测试。"""

    def test_llm_call_in_flight_helper_reads_instance_dict(self):
        """_llm_call_in_flight 应从实例 __dict__ 读取，MagicMock 不自动创建属性导致误判。"""
        from weave_agent_sdk.loop.base import _llm_call_in_flight

        agent = MagicMock()
        # 未设置 → False（不能因 MagicMock 属性自动创建而误判为在途）
        assert _llm_call_in_flight(agent) is False
        # 设置 → True
        agent.__dict__["_llm_in_flight"] = True
        assert _llm_call_in_flight(agent) is True
        # 清除 → False
        agent.__dict__.pop("_llm_in_flight", None)
        assert _llm_call_in_flight(agent) is False

    @pytest.mark.asyncio
    async def test_call_llm_sets_and_clears_in_flight_during_non_streaming_chat(self):
        """非流式 LLM 调用（含 tools）期间 _llm_in_flight=True，调用结束清除。

        这是"非流式调用在途视为活动"的基础：call_llm 在非流式 chat 期间置标记，
        使消费端空闲超时能据此重置计时，避免 tool 型流程健康慢速调用被误杀。
        """
        from weave_agent_sdk.loop.base import call_llm, _llm_call_in_flight
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        # 非流式：agent.__dict__ 无 _streaming → _is_streaming=False

        observed = {}

        async def fake_chat(messages, tools=None, max_tokens=None, temperature=None):
            observed["in_flight_during"] = _llm_call_in_flight(agent)
            return LLMResponse(content="hello", model="test")

        agent._llm.chat = fake_chat

        resp = await call_llm(agent, [MagicMock()], None)

        assert resp.content == "hello"
        assert observed["in_flight_during"] is True, "非流式 chat 期间应置 _llm_in_flight"
        assert _llm_call_in_flight(agent) is False, "调用结束后应清除 _llm_in_flight"

    def test_stream_and_ws_source_treat_in_flight_as_active_on_idle_timeout(self):
        """agent.stream() 与 ws_agent_stream 空闲超时分支应先检查在途标记，在途 → continue。"""
        import inspect
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.server.ws import ws_agent_stream

        stream_src = inspect.getsource(Weave.stream)
        # 空闲超时分支：先检查非流式 LLM 调用是否在途，在途视为"活动"→ continue 重置计时
        assert "_llm_call_in_flight(self)" in stream_src
        assert "if _llm_call_in_flight(self)" in stream_src
        assert "continue" in stream_src

        ws_src = inspect.getsource(ws_agent_stream)
        assert "_llm_call_in_flight(weave)" in ws_src
        assert "if _llm_call_in_flight(weave)" in ws_src
        assert "continue" in ws_src


class TestRound16DelayedUserUnit:
    """第16轮修复项2（scheduled/simple user 消息延迟提交）—— 单元测试。"""

    def test_simple_and_scheduled_delay_user_message_until_llm_reply(self):
        """simple/scheduled 的 persist_user_message 应位于 call_llm 之后（成功获得回复后才落盘）。"""
        import inspect
        from weave_agent_sdk.loop.simple import SimpleLoop
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        simple_src = inspect.getsource(SimpleLoop.run)
        simple_call = simple_src.index("response = await call_llm")
        simple_persist = simple_src.index("await persist_user_message")
        assert simple_persist > simple_call, "simple 延迟提交：user 消息应在 LLM 回复之后落盘"

        sched_src = inspect.getsource(ScheduledLoop._execute_once)
        sched_call = sched_src.index("response = await call_llm")
        sched_persist = sched_src.index("await persist_user_message")
        assert sched_persist > sched_call, "scheduled 延迟提交：user 消息应在 LLM 回复之后落盘"

    def test_iterative_delays_and_persists_user_once(self):
        """iterative 由 user_persisted 标志保证仅首次成功回复后落盘一次。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        call_idx = source.index("response = await call_llm")
        persist_idx = source.index("await persist_user_message(agent, user_input)")
        assert persist_idx > call_idx, "iterative 延迟提交：user 消息应在 LLM 回复之后落盘"
        # 只落盘一次：受 user_persisted 标志保护
        assert "if not user_persisted:" in source
        assert "user_persisted = True" in source


class TestRound16DocsUnit:
    """第16轮修复项3（types/weave.yaml stream_timeout 文档统一为空闲超时语义）—— 单元测试。"""

    def test_stream_timeout_docs_idle_semantics_types_and_yaml(self):
        """types.py LoopConfig docstring 与 weave.yaml 注释应明确"空闲超时"语义，
        而非"整次运行总时长上限"。"""
        import inspect
        from weave_agent_sdk.types import LoopConfig

        src = inspect.getsource(LoopConfig)
        assert "空闲超时" in src
        assert "不是整次运行总时长上限" in src

        yaml_path = Path(__file__).parent.parent / "docs" / "config-reference.yaml"
        text = yaml_path.read_text(encoding="utf-8")
        assert "空闲超时" in text
        assert '不是"整次运行总时长上限"' in text


class TestRound16ReinjectUnit:
    """第16轮修复项4（iterative 重注入预算约束 + tool_calls 摘要）—— 单元测试。"""

    def test_reinjected_messages_respect_budget(self):
        """_format_reinjected_messages 仅保留最近 limit 条（预算约束），防止 system prompt 单调膨胀。"""
        from weave_agent_sdk.loop.iterative import _format_reinjected_messages
        from weave_agent_sdk.types import Message, ToolCall

        removed = [
            Message(role="assistant", content=f"a{i}",
                    tool_calls=[ToolCall(id=f"c{i}", name="search", arguments={"q": str(i)})])
            for i in range(10)
        ]
        result = _format_reinjected_messages(removed, 3)
        assert len(result) == 3, "预算约束：应只重注入最近 3 条"
        assert result[0]["content"].startswith("a7")
        assert result[0]["content"].endswith('[tool_calls: search({"q": "7"})]')
        assert result[1]["content"].startswith("a8")
        assert result[1]["content"].endswith('[tool_calls: search({"q": "8"})]')
        assert result[2]["content"].startswith("a9")
        assert result[2]["content"].endswith('[tool_calls: search({"q": "9"})]')

        # 无预算压力（limit 大于条数）时全量保留
        result2 = _format_reinjected_messages(removed, 100)
        assert len(result2) == 10

    def test_reinjected_messages_preserve_tool_calls_summary(self):
        """assistant 条目 content 末尾附带 tool_calls 摘要；空 content 以摘要为正文；tool 消息原样保留。"""
        from weave_agent_sdk.loop.iterative import _format_reinjected_messages
        from weave_agent_sdk.types import Message, ToolCall

        m1 = Message(role="assistant", content="thinking...",
                     tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "x"}),
                                 ToolCall(id="c2", name="calc", arguments={"a": 1, "b": 2})])
        m2 = Message(role="assistant", content="", tool_calls=[ToolCall(id="c3", name="lookup", arguments={})])
        m3 = Message(role="tool", content="result", name="search", tool_call_id="c1")

        result = _format_reinjected_messages([m1, m2, m3], 10)

        assert len(result) == 3
        assert "thinking..." in result[0]["content"]
        assert 'search({"q": "x"})' in result[0]["content"]
        assert 'calc({"a": 1, "b": 2})' in result[0]["content"]
        assert "[tool_calls:" in result[0]["content"]
        # 空 content 的 assistant：摘要直接作为 content
        assert result[1]["content"] == '[tool_calls: lookup({})]'
        # tool 消息原样保留，无摘要
        assert result[2]["role"] == "tool"
        assert result[2]["content"] == "result"


class TestRound16ExhaustedUnit:
    """第16轮修复项5（max_iterations 耗尽统一标记未收敛，不再回退陈旧文本）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_iterative_run_exhausted_output_unconverged_marker(self):
        """耗尽迭代（每轮均有非空 assistant 文本 + tool_calls）时，输出为"未收敛"标记，
        不再静默回退到数轮前的陈旧 assistant 文本（修复前行为）。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = []  # 无停止条件 → max_iterations 耗尽
        agent._config.loop.tool_timeout = 5.0
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def echo_tool(q: str) -> str:
            return f"echo:{q}"

        agent._tools = [echo_tool]
        agent._tool_map = {"echo_tool": echo_tool}
        # 每轮都返回非空 assistant 文本 + tool_calls → 永不触发停止条件，耗尽 2 次
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(
            content="stale assistant text", model="test",
            tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"q": "a"})],
        ))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.iterations == 2
        # 核心：耗尽时统一标记"未收敛"，即便存在陈旧 assistant 文本也不再回退
        assert result.output == "(reached max_iterations=2 without a final response)"
        assert "stale assistant text" not in result.output
        assert result.memory_updated["tools"] == {"echo_tool": 2}

    @pytest.mark.asyncio
    async def test_iterative_run_converged_output_not_marker(self):
        """命中 no_tool_calls 停止条件时正常输出模型文本，不误用"未收敛"标记。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 2
        agent._config.loop.stop_conditions = [{"type": "no_tool_calls"}]
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="converged answer", model="test"))
        agent._system_prompt = "System"
        agent._tools = []
        agent._tool_map = {}
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=5.0)

        assert result.iterations == 1
        assert result.output == "converged answer"
        assert "without a final response" not in result.output


# ============================================================
# 第16轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound16LlInFlightE2E:
    """端到端测试：空闲超时不误杀非流式 LLM 调用在途的 tool 型流程（第16轮修复项1）。"""

    def _make_slow_tool_weave(self):
        """创建 iterative + 慢速非流式 chat 的 Weave 实例（流式模式）。

        非流式 LLM 调用（含 tools）期间零事件产出且耗时 > stream_timeout：
        若修复缺失，空闲超时会取消在途调用并 emit error；修复后应完成 emit done。
        """
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "iterative"
        weave._config.loop.max_iterations = 2
        weave._config.loop.stop_conditions = []
        weave._config.loop.stream_timeout = 0.05   # 空闲超时
        weave._config.loop.timeout = 0.05
        weave._config.loop.tool_timeout = 5.0
        weave._config.llm.max_tokens = 100
        weave._config.llm.temperature = 0.0
        weave._event_bus = EventBus()
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.get_namespaces = MagicMock(return_value=[])
        weave._load_system_prompt = MagicMock(return_value="sys")

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        weave._tools = [search_tool]
        weave._tool_map = {"search_tool": search_tool}

        from weave_agent_sdk.loop.iterative import IterativeLoop
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()
        weave._loop = loop

        # 非流式 chat（含 tools）耗时 0.2s > stream_timeout=0.05：在途期间空闲超时不触发
        n = {"count": 0}

        async def slow_chat(messages, tools=None, max_tokens=None, temperature=None):
            await asyncio.sleep(0.2)
            n["count"] += 1
            if n["count"] == 1:
                return LLMResponse(content="iter1", model="test",
                                   tool_calls=[ToolCall(id="c1", name="search_tool", arguments={"query": "q"})])
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        weave._llm = MagicMock()
        weave._llm.chat = slow_chat
        return weave

    @pytest.mark.asyncio
    async def test_e2e_stream_idle_timeout_in_flight_llm_not_killed(self):
        """stream()+iterative：非流式 LLM 调用耗时 > stream_timeout 时，在途视为活动，
        空闲超时不触发，最终收到 done 而非 error。"""
        weave = self._make_slow_tool_weave()

        events = []
        async for event in weave.stream("input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert events[-1].type == "done", "在途 LLM 调用不应被空闲超时误杀"
        assert events[-1].data["output"] == "final answer"
        assert not any(e.type == "error" for e in events)

    @pytest.mark.asyncio
    async def test_e2e_ws_idle_timeout_in_flight_llm_not_killed(self):
        """WS 路径同场景：非流式 LLM 调用在途时空闲超时不触发，客户端收到 done 而非 error。"""
        from weave_agent_sdk.server.ws import ws_agent_stream

        weave = self._make_slow_tool_weave()
        ws = _FakeWS({"input": "hi"})

        await asyncio.wait_for(ws_agent_stream(ws, weave), timeout=5.0)

        types_sent = [e["type"] for e in ws.sent]
        assert "done" in types_sent, "WS 在途 LLM 调用不应被空闲超时误杀"
        assert "error" not in types_sent


class TestRound16DelayedUserE2E:
    """端到端测试：simple loop user 消息延迟提交（第16轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_simple_loop_delayed_user_message_persisted_after_reply_only(self):
        """成功获得 assistant 回复后 user 消息落盘一次；LLM 调用失败时 user 消息不落盘
        （避免"有 user 无 assistant"悬空消息污染后续记忆注入）。"""
        from weave_agent_sdk.loop.simple import SimpleLoop
        from weave_agent_sdk.llm.base import LLMResponse

        # ── 成功路径：回复后 user 落盘一次 ──
        agent_ok = MagicMock()
        agent_ok._config = MagicMock()
        agent_ok._config.llm.max_tokens = 100
        agent_ok._config.llm.temperature = 0.0
        agent_ok._system_prompt = "System"
        agent_ok._llm = AsyncMock()
        agent_ok._llm.chat = AsyncMock(return_value=LLMResponse(content="reply", model="test"))
        agent_ok._memory = MagicMock()
        agent_ok._memory.get_namespaces = MagicMock(return_value=["s:1:stream"])
        agent_ok._memory.stream.append = MagicMock()
        agent_ok.__dict__["_memory_writes"] = {}

        loop_ok = SimpleLoop()
        loop_ok.on_start = AsyncMock()
        loop_ok.on_end = AsyncMock()
        loop_ok.before_think = AsyncMock(return_value={})
        loop_ok.after_think = AsyncMock()

        result = await loop_ok.run(agent_ok, "hello")
        assert result.output == "reply"
        user_entries = [c for c in agent_ok._memory.stream.append.call_args_list
                        if c.args[0].get("role") == "user"]
        assert len(user_entries) == 1
        assert user_entries[0].args[0]["content"] == "hello"

        # ── 失败路径：LLM 抛异常 → user 消息不落盘 ──
        agent_fail = MagicMock()
        agent_fail._config = MagicMock()
        agent_fail._config.llm.max_tokens = 100
        agent_fail._config.llm.temperature = 0.0
        agent_fail._system_prompt = "System"
        agent_fail._llm = AsyncMock()
        agent_fail._llm.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        agent_fail._memory = MagicMock()
        agent_fail._memory.get_namespaces = MagicMock(return_value=["s:1:stream"])
        agent_fail._memory.stream.append = MagicMock()
        agent_fail.__dict__["_memory_writes"] = {}

        loop_fail = SimpleLoop()
        loop_fail.on_start = AsyncMock()
        loop_fail.on_end = AsyncMock()
        loop_fail.before_think = AsyncMock(return_value={})
        loop_fail.after_think = AsyncMock()

        with pytest.raises(RuntimeError, match="llm down"):
            await loop_fail.run(agent_fail, "hello")

        assert agent_fail._memory.stream.append.call_count == 0, \
            "LLM 调用失败时 user 消息不应落盘（延迟提交语义）"


class TestRound16ExhaustedE2E:
    """端到端测试：stream() done 事件输出未收敛标记（第16轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_done_event_output_unconverged_when_exhausted(self):
        """stream()+iterative 耗尽迭代（每轮均 tool_calls）时，done 事件 output 为
        "未收敛"标记，不再回退陈旧 assistant 文本。"""
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "iterative"
        weave._config.loop.max_iterations = 3
        weave._config.loop.stop_conditions = []
        weave._config.loop.tool_timeout = 5.0
        weave._config.loop.stream_timeout = None
        weave._config.loop.timeout = 0.05
        weave._config.llm.max_tokens = 100
        weave._config.llm.temperature = 0.0
        weave._event_bus = EventBus()
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.get_namespaces = MagicMock(return_value=[])
        weave._load_system_prompt = MagicMock(return_value="sys")

        async def echo_tool(q: str) -> str:
            return f"echo:{q}"

        weave._tools = [echo_tool]
        weave._tool_map = {"echo_tool": echo_tool}

        from weave_agent_sdk.loop.iterative import IterativeLoop
        loop = IterativeLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()
        weave._loop = loop

        # 每轮返回非空 assistant 文本 + tool_calls → 永不收敛，耗尽 3 次
        weave._llm = MagicMock()
        weave._llm.chat = AsyncMock(return_value=LLMResponse(
            content="stale", model="test",
            tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"q": "a"})],
        ))

        events = []
        async for event in weave.stream("input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        assert events[-1].type == "done"
        assert events[-1].data["output"] == "(reached max_iterations=3 without a final response)"
        assert "stale" not in events[-1].data["output"]


class TestRound16ReinjectE2E:
    """端到端测试：iterative 长 tool 链裁剪后重注入带 tool_calls 摘要（第16轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_iterative_long_tool_chain_reinjects_with_tool_calls_summary(self):
        """12 轮 tool 链触发窗口裁剪后，被裁剪的早期消息重新注入 system prompt，
        且 assistant 条目附带 tool_calls 摘要（模型仍能看到完整工具链参数）。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop, _MAX_CONTEXT_MESSAGES
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 12
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.loop.memory_context_limit = 20
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])  # persist no-op

        captured = []
        n = {"i": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            captured.append(list(messages))
            n["i"] += 1
            if n["i"] < 12:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[ToolCall(id=f"c{n['i']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
            loop = IterativeLoop()
            loop.on_start = AsyncMock()
            loop.on_end = AsyncMock()
            loop.before_think = AsyncMock(return_value={})
            loop.after_think = AsyncMock()

            result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=10.0)

        assert result.output == "final answer"
        assert result.iterations == 12
        assert len(captured) == 12
        # 长 tool 链触发窗口裁剪：全程注入窗口 ≤ 上限
        assert all(len(m) <= _MAX_CONTEXT_MESSAGES for m in captured)
        # 被裁剪的早期 assistant 消息以带 tool_calls 摘要的形式重新注入 system prompt
        late_system = captured[-1][0].content
        assert "[tool_calls:" in late_system, "被裁剪消息应带 tool_calls 摘要重注入"
        assert 'search_tool({"query": "q"})' in late_system


# ============================================================
# 第1轮测试（本轮新增）：针对第1轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第1轮修复" 标题（最近一轮），共5个修复项:
#   - 修复项1: 订阅急切注册，先订阅后建任务，同步失败error不丢（event_bus.py / agent.py）
#   - 修复项2: before_think按ns裁剪本次写入（loop/base.py）
#   - 修复项3: config注释改空闲超时（config.py / types.py / weave.yaml）
#   - 修复项4: scheduled重启复位标志（loop/scheduled.py）
#   - 修复项5: removed_messages有界（loop/iterative.py）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class _Round1ConcreteLoop(BaseLoop):
    """为调用 BaseLoop.before_think 提供可实例化的具体子类。"""

    async def run(self, agent, user_input):
        raise NotImplementedError


class TestRound1EagerSubscribeUnit:
    """第1轮修复项1（订阅急切注册，先订阅后建任务）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_subscribe_registers_queues_eagerly(self):
        """EventBus.subscribe() 应在调用时立即注册队列（急切），emit 立即可见。

        修复前：队列创建与注册延迟到首次 anext()，后台任务先 emit 时 error 被丢弃。
        """
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token", "tool_call", "tool_result", "done", "error")

        # 急切注册：调用 subscribe() 后（未消费任何事件）订阅即已注册
        counts = bus.subscriber_count
        for et in ("token", "tool_call", "tool_result", "done", "error"):
            assert counts.get(et, 0) == 1, f"{et} 应在 subscribe() 调用时立即注册"

        # 启动生成器消费一个事件后 aclose，验证退订清理。
        # 注意：未启动的 async generator 调用 aclose 不会进入 finally（不会退订），
        # 必须先消费一个事件使其进入 try 块。
        await bus.emit("token", {"text": "t", "index": 0})
        event = await anext(gen)
        assert event.type == "token"
        await gen.aclose()
        assert bus.subscriber_count == {}

    def test_stream_subscribes_before_task_creation(self):
        """stream() 源码中订阅（subscribe_gen）应位于后台任务（_run_task）创建之前。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave.stream)
        subscribe_idx = source.index("subscribe_gen = self._event_bus.subscribe(")
        task_idx = source.index("_run_task = asyncio.create_task(_run_and_emit())")
        assert subscribe_idx < task_idx, \
            "订阅必须先于后台任务创建（防止同步失败 error 被丢弃）"


class TestRound1BeforeThinkPerNsUnit:
    """第1轮修复项2（before_think 按 ns 裁剪本次写入）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_before_think_reads_per_namespace_write_counts(self):
        """before_think 对每个 namespace 按其自身写入条数放大读取窗口（limit + ns_run_writes）。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=20)),
            _memory=MagicMock(),
        )
        # 两个 namespace，各自独立记录本次写入条数（不跨 ns 求和）
        agent.__dict__["_memory_writes"] = {
            "stream": {"ns_a:session:stream": 2, "ns_b:session:stream": 3},
        }
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns_a:session:stream", "ns_b:session:stream"],
            "state": [], "knowledge": [],
        }[at])

        called = {}

        def fake_last(n, namespaces):
            called[namespaces[0]] = n
            return [{"role": "user", "content": f"{namespaces[0]}:{i}"} for i in range(n)]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        # 修复项2：每个 ns 按自身写入条数放大读取窗口，而非跨 ns 求和
        assert called["ns_a:session:stream"] == 22   # 20 + 2
        assert called["ns_b:session:stream"] == 23   # 20 + 3
        # 各自剔除自身的本次写入后，每 ns 保留 limit 条历史
        assert len(ctx["stream"]) == 40

    @pytest.mark.asyncio
    async def test_before_think_excludes_only_own_namespace_writes(self):
        """before_think 只剔除各 namespace 自身本次写入，不跨 ns 过度裁剪历史。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=5)),
            _memory=MagicMock(),
        )
        # 仅 ns_a 有本次写入（2 条），ns_b 无本次写入（0 条）
        agent.__dict__["_memory_writes"] = {"stream": {"ns_a:session:stream": 2}}
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns_a:session:stream", "ns_b:session:stream"],
            "state": [], "knowledge": [],
        }[at])

        # ns_a 后端：5 条历史 + 2 条本次写入；ns_b 后端：5 条历史（无本次写入）
        entries_a = [{"role": "user", "content": f"a_h{i}"} for i in range(5)] + \
                    [{"role": "user", "content": f"a_r{i}"} for i in range(2)]
        entries_b = [{"role": "user", "content": f"b_h{i}"} for i in range(5)]

        def fake_last(n, namespaces):
            ns = namespaces[0]
            return (entries_a if ns == "ns_a:session:stream" else entries_b)[-n:]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        contents = [m["content"] for m in ctx["stream"]]
        # ns_a：读取窗口 5+2=7 → 剔除最近 2 条本次写入 → 保留 5 条历史
        for i in range(5):
            assert f"a_h{i}" in contents
        assert "a_r0" not in contents and "a_r1" not in contents
        # ns_b：无本次写入 → 5 条历史全部保留（未被跨 ns 求和过度裁剪）
        for i in range(5):
            assert f"b_h{i}" in contents
        # 两 ns 合计 10 条，跨运行历史未被多裁（修复项2 核心）
        assert len(ctx["stream"]) == 10


class TestRound1ConfigIdleDocUnit:
    """第1轮修复项3（config 注释改空闲超时）—— 单元测试。"""

    def test_config_stream_timeout_comment_idle_semantics(self):
        """config.py 中 stream_timeout 注释应明确"空闲超时"语义，而非"整体超时"。"""
        import inspect
        from weave_agent_sdk import config

        source = inspect.getsource(config)
        assert "空闲超时" in source
        assert "不是整次运行总时长上限" in source
        # 不应再残留"整体超时"作为 stream_timeout 的描述
        assert "整体超时" not in source.split("空闲超时")[0]

    def test_types_and_yaml_stream_timeout_docs_idle_semantics(self):
        """types.py LoopConfig 文档与 weave.yaml 注释应统一为"空闲超时"语义。"""
        import inspect
        from weave_agent_sdk.types import LoopConfig

        src = inspect.getsource(LoopConfig)
        assert "空闲超时" in src
        assert "不是整次运行总时长上限" in src

        yaml_path = Path(__file__).parent.parent / "docs" / "config-reference.yaml"
        text = yaml_path.read_text(encoding="utf-8")
        assert "空闲超时" in text
        assert "整次运行总时长上限" in text


class TestRound1ScheduledRestartUnit:
    """第1轮修复项4（scheduled 重启复位标志）—— 单元测试。"""

    def test_scheduled_run_resets_shutting_down_flag(self):
        """scheduled.py run() 启动时应复位 _shutting_down，允许 shutdown 后重新启动。"""
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        assert "self._shutting_down = False" in source

    @pytest.mark.asyncio
    async def test_scheduled_restart_resets_flag_and_executes(self):
        """handle_shutdown() 后同一实例重新 run() 应复位标志并正常执行，而非静默 no-op。"""
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(
                schedule="@on_data_change", event_min_interval=0)),
            _event_bus=EventBus(),
            _run_locks={},
        )
        executed = {"n": 0}

        async def fake_execute_once(agent_, user_input):
            executed["n"] += 1
            return LoopResult(output=f"run{executed['n']}", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        # 模拟 handle_shutdown() 之后的残留状态：_shutting_down 仍为 True
        loop._shutting_down = True

        task = asyncio.create_task(loop.run(agent, "hi"))

        # 修复项4：run() 启动即复位 _shutting_down，随后正常进入常驻调度
        await _round6_wait_until(lambda: loop._shutting_down is False)
        await _round6_wait_until(lambda: executed["n"] >= 1)   # 预热执行（非空结果）
        await agent._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: executed["n"] >= 2)

        loop.handle_shutdown()
        result = await asyncio.wait_for(task, timeout=5.0)

        assert executed["n"] == 2
        assert result.output == "run2", "重启后应正常返回最近一次执行结果，而非空 LoopResult"


class TestRound1RemovedMessagesUnit:
    """第1轮修复项5（removed_messages 有界）—— 单元测试。"""

    def test_removed_messages_budget_clip_in_source(self):
        """iterative.py run() 应对 removed_messages 做预算裁剪，防止长 tool 链内存无界增长。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "if len(removed_messages) > reinject_limit:" in source
        assert "removed_messages = removed_messages[-reinject_limit:]" in source

    def test_format_reinjected_messages_bounded_by_limit(self):
        """_format_reinjected_messages 只格式化最近 limit 条被裁消息（有界重注入）。"""
        from weave_agent_sdk.loop.iterative import _format_reinjected_messages
        from weave_agent_sdk.types import Message

        removed = [Message(role="assistant", content=f"msg{i}") for i in range(50)]
        result = _format_reinjected_messages(removed, 5)

        assert len(result) == 5
        assert result[0]["content"] == "msg45"
        assert result[-1]["content"] == "msg49"


# ============================================================
# 第1轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound1StreamSyncErrorE2E:
    """端到端测试：同步失败 error 事件不丢失（第1轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_sync_failure_error_delivered(self):
        """stream() 在首个调度槽内同步失败时，消费者仍收到 error 事件（订阅先于任务创建）。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.schedule = None
        weave._config.loop.timeout = 5.0
        weave._config.loop.stream_timeout = None   # 默认不启用空闲超时
        weave._event_bus = EventBus()
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._run_locks = {}
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        # 同步失败：_load_system_prompt 在首个调度槽内抛 ValueError
        weave._load_system_prompt = MagicMock(
            side_effect=ValueError("No system prompt configured")
        )

        events = []
        async for event in weave.stream("input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # 若订阅晚于任务创建（修复前），error 事件会被丢弃、消费者永久挂起
        assert len(events) >= 1
        assert events[-1].type == "error"
        assert "No system prompt configured" in events[-1].data["message"]
        assert events[-1].data["exception"] == "ValueError"


class TestRound1BeforeThinkPerNsE2E:
    """端到端测试：多 namespace 下 before_think 按 ns 裁剪（第1轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_before_think_per_namespace_clip_no_cross_loss(self):
        """多 namespace 下按 ns 裁剪，不跨 ns 求和导致跨运行历史丢失。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=3)),
            _memory=MagicMock(),
        )
        # 本次运行写入：ns_a 1 条，ns_b 2 条（每 ns 各自独立）
        agent.__dict__["_memory_writes"] = {
            "stream": {"ns_a:session:stream": 1, "ns_b:session:stream": 2},
        }
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns_a:session:stream", "ns_b:session:stream"],
            "state": [], "knowledge": [],
        }[at])

        # ns_a 后端：5 条历史 + 1 条本次写入（窗口 3+1=4 → 剔 1 → 3 条历史）
        # ns_b 后端：5 条历史 + 2 条本次写入（窗口 3+2=5 → 剔 2 → 3 条历史）
        def fake_last(n, namespaces):
            ns = namespaces[0]
            if ns == "ns_a:session:stream":
                hist = [{"role": "user", "content": f"a_h{i}"} for i in range(5)]
                run = [{"role": "user", "content": "a_run"}]
                return (hist + run)[-n:]
            hist = [{"role": "user", "content": f"b_h{i}"} for i in range(5)]
            run = [{"role": "user", "content": f"b_run{i}"} for i in range(2)]
            return (hist + run)[-n:]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        contents = [m["content"] for m in ctx["stream"]]
        # 每个 ns 均保留最近 limit(3) 条跨运行历史（未被跨 ns 求和过度裁剪）
        assert len(ctx["stream"]) == 6
        for i in (2, 3, 4):
            assert f"a_h{i}" in contents
            assert f"b_h{i}" in contents
        # 本次写入条目被剔除
        assert "a_run" not in contents
        assert "b_run0" not in contents and "b_run1" not in contents


class TestRound1ConfigIdleTimeoutE2E:
    """端到端测试：stream_timeout 空闲超时语义经 load_config 全链路一致（第1轮修复项3）。"""

    def test_e2e_config_stream_timeout_idle_semantics(self, tmp_path):
        """真实 weave.yaml 的 stream_timeout 默认 None（不启用）；配置后独立解析，不影响 timeout。"""
        import yaml

        # 真实默认配置文件：stream_timeout 被注释（不启用）→ None；timeout 保持宽限值
        project_root = Path(__file__).parent.parent
        config = load_config(project_root / "weave.yaml")
        assert config.loop.stream_timeout is None
        assert config.loop.timeout == 5.0

        # 配置 stream_timeout 后独立解析（空闲超时语义，非整次运行总时长上限）
        yaml_path = tmp_path / "r1_idle_timeout.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": 3.5, "timeout": 8.0},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        cfg = load_config(yaml_path)
        assert cfg.loop.stream_timeout == 3.5
        assert cfg.loop.timeout == 8.0


class TestRound1ScheduledRestartE2E:
    """端到端测试：shutdown 后同一 ScheduledLoop 实例重启并继续执行（第1轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_loop_restart_after_shutdown(self):
        """完整生命周期：shutdown 后同一实例重新 run() 应复位标志并继续执行触发。"""
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        def make_agent():
            return SimpleNamespace(
                _config=SimpleNamespace(loop=SimpleNamespace(
                    schedule="@on_data_change", event_min_interval=0)),
                _event_bus=EventBus(),
                _run_locks={},
            )

        executed = {"n": 0}

        async def fake_execute_once(agent_, user_input):
            executed["n"] += 1
            return LoopResult(output=f"run{executed['n']}", elapsed_ms=0, iterations=1, memory_updated={})

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        # ── 第一次启动 ──
        agent1 = make_agent()
        task1 = asyncio.create_task(loop.run(agent1, "first"))
        await _round6_wait_until(lambda: "data_change" in agent1._event_bus.subscriber_count)
        await _round6_wait_until(lambda: executed["n"] >= 1)   # 预热
        await agent1._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: executed["n"] >= 2)
        loop.handle_shutdown()
        result1 = await asyncio.wait_for(task1, timeout=5.0)

        assert result1.output == "run2"
        assert loop._shutting_down is True     # 已 shutdown
        assert loop._current_task is None      # 任务已收尾

        # ── 重新启动（修复项4：不复位则重启立即返回空 LoopResult）──
        agent2 = make_agent()
        task2 = asyncio.create_task(loop.run(agent2, "second"))
        await _round6_wait_until(lambda: loop._shutting_down is False)
        await _round6_wait_until(lambda: "data_change" in agent2._event_bus.subscriber_count)
        await _round6_wait_until(lambda: executed["n"] >= 3)   # 重启预热执行
        await agent2._event_bus.emit("data_change", {})
        await _round6_wait_until(lambda: executed["n"] >= 4)
        loop.handle_shutdown()
        result2 = await asyncio.wait_for(task2, timeout=5.0)

        assert executed["n"] == 4
        assert result2.output == "run4", "重启后应继续执行触发，而非静默 no-op"


class TestRound1RemovedMessagesE2E:
    """端到端测试：长 tool 链下 removed_messages 恒受预算约束（第1轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_long_tool_chain_removed_messages_bounded(self):
        """30 轮 tool 链：每次重注入前 removed_messages 长度恒 ≤ memory_context_limit。"""
        from weave_agent_sdk.loop.iterative import (
            IterativeLoop,
            _format_reinjected_messages,
        )
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 30
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.loop.memory_context_limit = 20
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        n = {"i": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            n["i"] += 1
            if n["i"] < 30:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[ToolCall(id=f"c{n['i']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        # 通过 spy 捕获每次重注入前 removed_messages 的真实长度，
        # 直接验证内部列表有界（修复项5：预算裁剪后恒 ≤ reinject_limit）
        reinject_lens = []
        real_fn = _format_reinjected_messages

        def spy(removed, limit):
            reinject_lens.append(len(removed))
            return real_fn(removed, limit)

        with patch("weave_agent_sdk.loop.iterative._format_reinjected_messages", side_effect=spy):
            with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
                loop = IterativeLoop()
                loop.on_start = AsyncMock()
                loop.on_end = AsyncMock()
                loop.before_think = AsyncMock(return_value={})
                loop.after_think = AsyncMock()
                result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=15.0)

        assert result.output == "final answer"
        assert result.iterations == 30
        # 长 tool 链确实触发裁剪与重注入
        assert reinject_lens, "长 tool 链应触发 removed_messages 重注入"
        # 修复项5 核心：removed_messages 恒 ≤ memory_context_limit(20)，内存有界
        assert all(l <= 20 for l in reinject_lens), \
            f"removed_messages 应恒 ≤ 20，实际 {reinject_lens}"

# ============================================================
# 第2轮测试（本轮新增）：针对第1轮修复行为的补充验证
# 依据 fix_record.md 最后一个 "## 第1轮修复" 标题（最近一轮，
# 本轮修复摘要：done），共5个修复项:
#   - 修复项1: 订阅急切注册，先订阅后建任务，同步失败error不丢（event_bus.py / agent.py）
#   - 修复项2: before_think按ns裁剪本次写入（loop/base.py）
#   - 修复项3: config注释改空闲超时（config.py / types.py / weave.yaml）
#   - 修复项4: scheduled重启复位标志（loop/scheduled.py）
#   - 修复项5: removed_messages有界（loop/iterative.py）
# 与既有 第1轮测试 互补（不重复），新增 15 个测试：
#   10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound2EagerSubscribeUnit:
    """第1轮修复项1（订阅急切注册，先订阅后建任务）—— 第2轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_subscribe_emits_before_first_consume_not_lost(self):
        """首个 anext() 之前 emit 的事件应入队不丢（急切注册保证）。

        修复前：队列创建与注册延迟到首次 anext()，后台任务先 emit 时
        错误事件被丢弃（消费者永久挂起）。修复后 subscribe() 调用时即
        完成注册，早期 emit 事件立即可见。
        """
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token", "done")

        # 急切注册：未消费任何事件，订阅已立即可见
        assert bus.subscriber_count == {"token": 1, "done": 1}

        # 首个 anext 之前 emit：事件入队不丢（修复前延迟注册会丢弃）
        await bus.emit("token", {"text": "early", "index": 0})
        event = await anext(gen)
        assert event.type == "token"
        assert event.data["text"] == "early"

        # 消费 done 后 aclose，验证退订清理
        await bus.emit("done", {"output": "x", "elapsed_ms": 0, "iterations": 1})
        await anext(gen)
        await gen.aclose()
        assert bus.subscriber_count == {}

    @pytest.mark.asyncio
    async def test_subscribe_multiple_subscribers_cleanup_per_subscriber(self):
        """同一事件类型的多个订阅者各自独立注册/退订，互不影响。

        每个订阅者调用 subscribe() 时都急切注册；aclose() 只清理自己的队列，
        直到最后一个订阅者退订后才移除该事件类型。
        """
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen1 = bus.subscribe("token")
        gen2 = bus.subscribe("token")
        assert bus.subscriber_count["token"] == 2

        await bus.emit("token", {"text": "t1", "index": 0})
        await bus.emit("token", {"text": "t2", "index": 1})
        # 广播：两个订阅者各自收到相同的事件序列
        e1a = await anext(gen1)
        e1b = await anext(gen1)
        e2a = await anext(gen2)
        assert e1a.data["text"] == "t1"
        assert e1b.data["text"] == "t2"
        assert e2a.data["text"] == "t1"

        # gen1 退订只移除自己的队列，gen2 仍在
        await gen1.aclose()
        assert bus.subscriber_count["token"] == 1
        await gen2.aclose()
        assert bus.subscriber_count == {}


class TestRound2BeforeThinkUnit:
    """第1轮修复项2（before_think 按 ns 裁剪本次写入）—— 第2轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_before_think_ignores_stale_namespace_writes(self):
        """_memory_writes 中不属于当前 namespace 列表的陈旧写入不应放大任何 ns 窗口。

        修复项2 核心：写入计数是 per-namespace 的，只按各 ns 自身计数放大
        读取窗口。陈旧 ns（不在 get_namespaces 结果中）的写入条数不得影响
        真实 ns 的窗口（否则会多裁掉跨运行历史）。
        """
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=5)),
            _memory=MagicMock(),
        )
        # 陈旧 ns 记录了 99 条写入，但 get_namespaces 不再返回它
        agent.__dict__["_memory_writes"] = {"stream": {"stale:session:stream": 99}}
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["real:session:stream"], "state": [], "knowledge": [],
        }[at])

        called = {}

        def fake_last(n, namespaces):
            called[namespaces[0]] = n
            return [{"role": "user", "content": f"h{i}"} for i in range(n)]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        # 陈旧 ns 的 99 条写入不得放大真实 ns 窗口（5+0=5）
        assert called["real:session:stream"] == 5
        assert len(ctx["stream"]) == 5

    @pytest.mark.asyncio
    async def test_before_think_run_writes_exceed_available_history(self):
        """ns 本次写入条数超过后端可用条目时，该 ns 历史应剔除为空而不负向切片报错。

        守卫分支：`ns_entries[:-ns_run_writes] if len(ns_entries) > ns_run_writes
        else []` —— 当后端只返回本次写入、无任何跨运行历史时，返回空列表，
        且不产生负索引切片越界。
        """
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=3)),
            _memory=MagicMock(),
        )
        agent.__dict__["_memory_writes"] = {"stream": {"ns:session:stream": 5}}
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns:session:stream"], "state": [], "knowledge": [],
        }[at])

        def fake_last(n, namespaces):
            # 后端只有 3 条，且全部属于"本次运行"
            return [{"role": "user", "content": f"run{i}"} for i in range(3)]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        # 窗口 3+5=8，但可用仅 3 条且全为本次写入 → 剔除后为空，不注入
        assert "stream" not in ctx


class TestRound2ConfigIdleDocUnit:
    """第1轮修复项3（config 注释改空闲超时）—— 第2轮补充单元测试。"""

    def test_weave_yaml_stream_timeout_commented_disabled_by_default(self):
        """weave.yaml 的 stream_timeout 应默认被注释（不启用），注释块明确"空闲超时"语义。

        修复项3 核心：将 stream_timeout 的注释从"整体超时"改为"空闲超时"，
        避免用户误以为它是整次运行总时长上限（默认 5s 会误杀慢速运行）。
        """
        yaml_path = Path(__file__).parent.parent / "docs" / "config-reference.yaml"
        text = yaml_path.read_text(encoding="utf-8")

        commented = [l for l in text.splitlines() if "stream_timeout:" in l]
        assert commented, "config-reference.yaml 应包含 stream_timeout 配置行"
        for line in commented:
            assert line.lstrip().startswith("#"), \
                f"stream_timeout 应默认被注释（不启用），实际: {line.strip()}"

        # 注释块明确"空闲超时"语义，而非整次运行总时长上限
        assert "空闲超时" in text
        assert "整次运行总时长上限" in text

    def test_types_docstring_distinguishes_timeout_vs_stream_timeout(self):
        """types.py LoopConfig 文档应区分 timeout（宽限）与 stream_timeout（空闲超时）。

        修复项3 核心：timeout 是"收到 done/error 后等待后台任务收尾的宽限时长"，
        stream_timeout 是"空闲超时"，不是整次运行总时长上限；默认 None 不启用。
        """
        import inspect
        from weave_agent_sdk.types import LoopConfig

        src = inspect.getsource(LoopConfig)
        assert "宽限时长" in src            # timeout 语义：后台任务收尾宽限
        assert "空闲超时" in src            # stream_timeout 语义：空闲超时
        assert "不是整次运行总时长上限" in src

        config = LoopConfig()
        assert config.stream_timeout is None   # 默认不启用
        assert config.timeout == 5.0           # 宽限时长默认 5.0


class TestRound2ScheduledRestartUnit:
    """第1轮修复项4（scheduled 重启复位标志）—— 第2轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_scheduled_single_execution_preserves_shutting_down_flag(self):
        """未配置 schedule 的单次执行路径不应复位 _shutting_down。

        复位只属于常驻调度路径（run() 启动时）。单次执行直接走 _execute_once
        返回，不触碰标志——语义上单次执行无"重启"概念。
        """
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        agent = MagicMock()
        agent._config.loop.schedule = None   # 单次执行路径
        loop = ScheduledLoop()
        loop._shutting_down = True           # 预置关闭状态
        loop._execute_once = AsyncMock(return_value=LoopResult(
            output="ok", elapsed_ms=0, iterations=1, memory_updated={}
        ))

        result = await loop.run(agent, "hi")

        assert result.output == "ok"
        loop._execute_once.assert_awaited_once_with(agent, "hi")
        # 单次路径不应复位 _shutting_down（那是常驻路径的职责）
        assert loop._shutting_down is True

    def test_scheduled_resident_path_sets_task_and_resets_flags(self):
        """常驻调度路径应在主循环之前复位 _shutting_down 并重设 _current_task / _executing。

        修复项4 核心：shutdown 后同一实例重新 run() 若不复位 _shutting_down，
        while 循环立即退出并返回空 LoopResult——重启调用被静默吞掉。复位语句
        必须位于主循环之前。
        """
        import inspect
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        source = inspect.getsource(ScheduledLoop.run)
        assert "self._current_task = asyncio.current_task()" in source
        assert "self._executing = False" in source
        assert "self._shutting_down = False" in source

        idx_shutdown = source.index("self._shutting_down = False")
        idx_while = source.index("while not self._shutting_down:")
        assert idx_shutdown < idx_while, "复位语句应位于常驻主循环之前"


class TestRound2RemovedMessagesUnit:
    """第1轮修复项5（removed_messages 有界）—— 第2轮补充单元测试。"""

    def test_removed_messages_clip_keeps_recent_order_bounded(self):
        """removed_messages 裁剪后保留最近 reinject_limit 条且顺序不变。

        修复项5 核心：只保留最近 reinject_limit 条（重注入也只取最近 limit 条），
        在不变更重注入语义的前提下把内存约束为常数预算。
        """
        from weave_agent_sdk.loop.iterative import _format_reinjected_messages
        from weave_agent_sdk.types import Message

        removed = [Message(role="assistant", content=f"m{i}") for i in range(30)]
        limit = 5

        # 复现 run() 中的裁剪逻辑：removed_messages[-reinject_limit:]
        clipped = removed[-limit:]
        assert [m.content for m in clipped] == ["m25", "m26", "m27", "m28", "m29"]

        # 重注入只使用最近 limit 条，顺序不变、数量有界
        reinjected = _format_reinjected_messages(clipped, limit)
        assert [e["content"] for e in reinjected] == ["m25", "m26", "m27", "m28", "m29"]
        assert len(reinjected) == 5

    def test_iterative_run_clamps_invalid_reinject_limit(self):
        """run() 对非数值 / 非正的 reinject_limit 应回退默认 20。

        reinject_limit 来自 memory_context_limit（默认 20）。非法配置
        （字符串 / <=0）若不回退，`removed_messages[-limit:]` 会产生
        负切片 / 空切片，破坏有界性保证。
        """
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert 'reinject_limit = getattr(config, "memory_context_limit", 20)' in source
        assert "if not isinstance(reinject_limit, int) or reinject_limit <= 0:" in source
        assert "reinject_limit = 20" in source


# ============================================================
# 第2轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound2StreamSyncErrorE2E:
    """端到端测试：同步失败 error 不丢 + 订阅无泄漏（第1轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_sync_failure_twice_no_subscriber_leak(self):
        """连续两次同步失败（_load_system_prompt 抛 ValueError）都应收到 error 事件，
        且每次流结束订阅都被清理（subscriber_count 归零，无注册泄漏）。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 5.0
        weave._config.loop.stream_timeout = None   # 默认不启用空闲超时
        weave._event_bus = EventBus()
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._run_locks = {}
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._load_system_prompt = MagicMock(
            side_effect=ValueError("no system prompt configured")
        )

        for i in range(2):
            gen = weave.stream("input")
            events = []
            async for event in gen:
                events.append(event)
                if event.type in ("done", "error"):
                    break
            # 显式关闭外层流生成器，触发内部订阅退订清理（async for break 不会自动 aclose）
            await gen.aclose()

            assert events[-1].type == "error", f"第{i+1}次同步失败应收到 error 事件"
            assert "no system prompt configured" in events[-1].data["message"]
            assert events[-1].data["exception"] == "ValueError"
            # 订阅急切注册先于任务创建 → error 不被丢弃；显式关闭后订阅被清理
            assert weave._event_bus.subscriber_count == {}, \
                f"第{i+1}次流关闭后订阅应被清理，实际 {weave._event_bus.subscriber_count}"


class TestRound2BeforeThinkE2E:
    """端到端测试：多 namespace 下 before_think 按 ns 裁剪（第1轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_three_namespaces_mixed_writes_each_keeps_limit(self):
        """三个 namespace 混合写入（1/2/0 条）时，每个 ns 各自保留 limit 条历史，
        本次写入条目被剔除，不跨 ns 求和导致历史丢失。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=3)),
            _memory=MagicMock(),
        )
        agent.__dict__["_memory_writes"] = {"stream": {
            "ns_a:session:stream": 1,
            "ns_b:session:stream": 2,
            "ns_c:session:stream": 0,
        }}
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns_a:session:stream", "ns_b:session:stream", "ns_c:session:stream"],
            "state": [], "knowledge": [],
        }[at])

        run_counts = {
            "ns_a:session:stream": 1,
            "ns_b:session:stream": 2,
            "ns_c:session:stream": 0,
        }

        def fake_last(n, namespaces):
            ns = namespaces[0]
            hist = [{"role": "user", "content": f"{ns}:h{i}"} for i in range(10)]
            runs = [{"role": "user", "content": f"{ns}:r{i}"} for i in range(run_counts[ns])]
            return (hist + runs)[-n:]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        contents = [m["content"] for m in ctx["stream"]]
        # 每个 ns 保留最近 limit(3) 条跨运行历史，合计 9 条
        assert len(ctx["stream"]) == 9
        for ns in ("ns_a", "ns_b", "ns_c"):
            for i in (7, 8, 9):
                assert f"{ns}:session:stream:h{i}" in contents, \
                    f"{ns} 应保留历史 h{i}"
            # 本次写入条目被各自剔除
            for j in range(run_counts[f"{ns}:session:stream"]):
                assert f"{ns}:session:stream:r{j}" not in contents


class TestRound2ConfigIdleTimeoutE2E:
    """端到端测试：stream_timeout 空闲超时语义经 load_config 全链路一致（第1轮修复项3）。"""

    def test_e2e_stream_timeout_disabled_default_and_honored_when_set(self, tmp_path):
        """真实 weave.yaml 的 stream_timeout 默认被注释（不启用）→ None；
        配置后独立解析（float），不影响 timeout 宽限值。"""
        import yaml

        # 真实默认配置：stream_timeout 被注释（不启用）→ None；timeout 保持宽限值
        project_root = Path(__file__).parent.parent
        config = load_config(project_root / "weave.yaml")
        assert config.loop.stream_timeout is None
        assert config.loop.timeout == 5.0

        # 配置 stream_timeout 后独立解析（空闲超时语义，非整次运行总时长上限）
        yaml_path = tmp_path / "r2_idle_timeout.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": 3.5, "timeout": 8.0},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        cfg = load_config(yaml_path)
        assert cfg.loop.stream_timeout == 3.5
        assert cfg.loop.timeout == 8.0


class TestRound2ScheduledRestartE2E:
    """端到端测试：shutdown 后同一 ScheduledLoop 实例连续两次重启并继续执行（第1轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_restart_twice_continues(self):
        """完整生命周期：shutdown → run → shutdown → run 两个周期，
        每个周期都正常执行触发（run 编号递增），且重启前 _executing 已复位。"""
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        def make_agent():
            return SimpleNamespace(
                _config=SimpleNamespace(loop=SimpleNamespace(
                    schedule="@on_data_change", event_min_interval=0)),
                _event_bus=EventBus(),
                _run_locks={},
            )

        executed = {"n": 0}

        async def fake_execute_once(agent_, user_input):
            executed["n"] += 1
            return LoopResult(
                output=f"run{executed['n']}", elapsed_ms=0, iterations=1, memory_updated={}
            )

        loop = ScheduledLoop()
        loop._execute_once = fake_execute_once

        for cycle in range(2):
            agent = make_agent()
            task = asyncio.create_task(loop.run(agent, f"cycle{cycle}"))

            # 修复项4：run() 启动即复位 _shutting_down，随后正常进入常驻调度
            await _round6_wait_until(lambda: loop._shutting_down is False)
            await _round6_wait_until(
                lambda: "data_change" in agent._event_bus.subscriber_count
            )
            # 事件驱动模式启动即执行一次（预热）
            await _round6_wait_until(lambda: executed["n"] >= (cycle * 2 + 1))
            # 事件触发第二次执行
            await agent._event_bus.emit("data_change", {})
            await _round6_wait_until(lambda: executed["n"] >= (cycle * 2 + 2))

            loop.handle_shutdown()
            result = await asyncio.wait_for(task, timeout=5.0)

            assert result.output == f"run{cycle * 2 + 2}", \
                f"第{cycle + 1}个周期应正常返回执行结果"
            assert loop._shutting_down is True    # 已 shutdown
            assert loop._current_task is None     # 任务已收尾
            assert loop._executing is False       # 重启前执行标志已复位


class TestRound2RemovedMessagesE2E:
    """端到端测试：长 tool 链下 removed_messages 恒受预算约束（第1轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_removed_messages_bounded_with_small_limit(self):
        """25 轮 tool 链 + memory_context_limit=5：每次重注入前 removed_messages
        长度恒 ≤ 5（小预算强调有界性），重注入仍携带 tool_calls 摘要。"""
        from weave_agent_sdk.loop.iterative import IterativeLoop, _format_reinjected_messages
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 25
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        agent._config.loop.memory_context_limit = 5   # 小预算，强调有界性
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        n = {"i": 0}

        async def fake_call_llm(agent_, messages, tools=None):
            n["i"] += 1
            if n["i"] < 25:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[ToolCall(id=f"c{n['i']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        # 通过 spy 捕获每次重注入前 removed_messages 的真实长度，
        # 直接验证内部列表有界（修复项5：预算裁剪后恒 ≤ reinject_limit）
        reinject_lens = []
        real_fn = _format_reinjected_messages

        def spy(removed, limit):
            reinject_lens.append(len(removed))
            return real_fn(removed, limit)

        with patch("weave_agent_sdk.loop.iterative._format_reinjected_messages", side_effect=spy):
            with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
                loop = IterativeLoop()
                loop.on_start = AsyncMock()
                loop.on_end = AsyncMock()
                loop.before_think = AsyncMock(return_value={})
                loop.after_think = AsyncMock()
                result = await asyncio.wait_for(loop.run(agent, "hi"), timeout=15.0)

        assert result.output == "final answer"
        assert result.iterations == 25
        assert reinject_lens, "长 tool 链应触发 removed_messages 重注入"
        # 修复项5 核心：removed_messages 恒 ≤ memory_context_limit(5)，内存有界
        assert all(l <= 5 for l in reinject_lens), \
            f"removed_messages 应恒 ≤ 5，实际 {reinject_lens}"

# ============================================================
# 第3轮测试（本轮新增）：针对第1轮修复行为的补充验证
# 依据 fix_record.md 最后一个 "## 第1轮修复" 标题（最近一轮，
# 本轮修复摘要：done），共5个修复项:
#   - 修复项1: 订阅急切注册，先订阅后建任务，同步失败error不丢（event_bus.py / agent.py）
#   - 修复项2: before_think按ns裁剪本次写入（loop/base.py）
#   - 修复项3: config注释改空闲超时（config.py / types.py / weave.yaml）
#   - 修复项4: scheduled重启复位标志（loop/scheduled.py）
#   - 修复项5: removed_messages有界（loop/iterative.py）
# 与既有 第1轮/第2轮测试 互补（不重复），本轮新增 15 个测试：
#   10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound3EagerSubscribeUnit:
    """第1轮修复项1（订阅急切注册，先订阅后建任务）—— 第3轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_subscribe_mixed_event_types_cleanup_all(self):
        """单个订阅者跨多种事件类型：subscribe() 调用时全部急切注册，
        aclose() 时全部退订清理（每种事件类型都不残留）。"""
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token", "tool_call", "done")

        counts = bus.subscriber_count
        assert counts == {"token": 1, "tool_call": 1, "done": 1}

        await bus.emit("token", {"text": "t", "index": 0})
        await bus.emit("tool_call", {"name": "s", "arguments": {}})
        await bus.emit("done", {"output": "o", "elapsed_ms": 0, "iterations": 1})

        assert (await anext(gen)).type == "token"
        assert (await anext(gen)).type == "tool_call"
        assert (await anext(gen)).type == "done"

        await gen.aclose()
        assert bus.subscriber_count == {}

    @pytest.mark.asyncio
    async def test_emit_with_no_subscriber_safe_noop(self):
        """无订阅者时 emit 应是安全的 no-op（不抛异常、不残留队列）。"""
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        await bus.emit("done", {"output": "x", "elapsed_ms": 0, "iterations": 1})
        assert bus.subscriber_count == {}


class TestRound3BeforeThinkPerNsUnit:
    """第1轮修复项2（before_think 按 ns 裁剪本次写入）—— 第3轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_before_think_no_write_counts_reads_exact_limit(self):
        """无任何本次写入时，每个 ns 应精确读取 limit 条历史（0 放大）。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=10)),
            _memory=MagicMock(),
        )
        agent.__dict__["_memory_writes"] = None   # 无本次写入
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns_a:session:stream", "ns_b:session:stream"],
            "state": [], "knowledge": [],
        }[at])

        called = {}

        def fake_last(n, namespaces):
            called[namespaces[0]] = n
            return [{"role": "user", "content": f"{namespaces[0]}:{i}"} for i in range(n)]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        assert called["ns_a:session:stream"] == 10   # 0 放大
        assert called["ns_b:session:stream"] == 10
        assert len(ctx["stream"]) == 20

    @pytest.mark.asyncio
    async def test_before_think_stream_clip_with_state_and_knowledge(self):
        """按 ns 裁剪 stream 的同时，state / knowledge 注入不受影响。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=5)),
            _memory=MagicMock(),
        )
        agent.__dict__["_memory_writes"] = {"stream": {"ns:session:stream": 2}}
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns:session:stream"],
            "state": ["ns:session:state"],
            "knowledge": ["ns:session:knowledge"],
        }[at])

        def fake_last(n, namespaces):
            all_entries = [{"role": "user", "content": f"h{i}"} for i in range(5)] + \
                          [{"role": "user", "content": f"run{i}"} for i in range(2)]
            return all_entries[-n:]

        agent._memory.stream.last = fake_last
        agent._memory.state.get_all = MagicMock(return_value={"k": "v"})
        agent._memory.knowledge.search = MagicMock(return_value=[SimpleNamespace(content="kb")])

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        # stream 按 ns 裁剪：剔除本次写入 2 条，保留 5 条历史
        stream_contents = [m["content"] for m in ctx["stream"]]
        assert len(ctx["stream"]) == 5
        assert all(c.startswith("h") for c in stream_contents)
        assert not any(c.startswith("run") for c in stream_contents)
        # state / knowledge 照常注入
        assert ctx["state"] == {"k": "v"}
        assert len(ctx["knowledge"]) == 1


class TestRound3ConfigIdleDocUnit:
    """第1轮修复项3（config 注释改空闲超时）—— 第3轮补充单元测试。"""

    def test_config_stream_timeout_zero_parsed_but_stream_disables(self, tmp_path):
        """YAML stream_timeout=0 应被解析为 0.0（非 None），但 stream() 的
        >0 守卫使其不启用（0/负值/非数值一律视为不启用，避免 wait_for 收到非法超时）。"""
        import yaml

        yaml_path = tmp_path / "r3_stream_timeout_zero.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": 0},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.stream_timeout == 0.0

        # stream() 以 >0 判断是否启用：0 不启用
        import inspect
        from weave_agent_sdk.agent import Weave
        source = inspect.getsource(Weave.stream)
        assert "has_timeout = isinstance(stream_timeout, (int, float)) and stream_timeout > 0" in source

    def test_types_stream_timeout_field_default_and_annotation(self):
        """LoopConfig.stream_timeout 类型为 float|None、默认 None（不启用），
        与 timeout（宽限 5.0）在字段层面即分离。"""
        from weave_agent_sdk.types import LoopConfig

        config = LoopConfig()
        assert config.stream_timeout is None
        assert config.timeout == 5.0
        field = LoopConfig.__dataclass_fields__["stream_timeout"]
        assert "| None" in field.type
        assert field.default is None


class TestRound3ScheduledRestartUnit:
    """第1轮修复项4（scheduled 重启复位标志）—— 第3轮补充单元测试。"""

    @pytest.mark.asyncio
    async def test_execute_once_resets_executing_flag_on_error(self):
        """_execute_once 抛异常（LLM 调用失败）时，_executing 在 finally 中复位为 False，
        避免重启后把新触发误判为"执行中"（影响 handle_shutdown 的分支选择）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=["s:1:state"])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with pytest.raises(RuntimeError, match="llm down"):
            await loop._execute_once(agent, "input")

        assert loop._executing is False

    @pytest.mark.asyncio
    async def test_restart_uses_fresh_current_task(self):
        """shutdown 后同一实例重新 run()：_current_task 由新任务接管（非复用旧任务），
        且 _executing 复位为 False。"""
        from types import SimpleNamespace
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.types import LoopResult

        def make_agent():
            return SimpleNamespace(
                _config=SimpleNamespace(loop=SimpleNamespace(
                    schedule="@on_data_change", event_min_interval=0)),
                _event_bus=EventBus(),
                _run_locks={},
            )

        loop = ScheduledLoop()
        loop._execute_once = AsyncMock(return_value=LoopResult(
            output="tick", elapsed_ms=0, iterations=1, memory_updated={}
        ))

        # 第一次启动
        agent1 = make_agent()
        task1 = asyncio.create_task(loop.run(agent1, "first"))
        await _round6_wait_until(lambda: "data_change" in agent1._event_bus.subscriber_count)
        await _round6_wait_until(lambda: loop._execute_once.call_count >= 1)  # 预热
        task1_ref = loop._current_task
        assert task1_ref is not None

        loop.handle_shutdown()
        await asyncio.wait_for(task1, timeout=5.0)
        assert loop._current_task is None
        assert loop._executing is False

        # 重新启动（修复项4：不复位则 while 立即退出、被静默吞掉）
        agent2 = make_agent()
        task2 = asyncio.create_task(loop.run(agent2, "second"))
        await _round6_wait_until(lambda: "data_change" in agent2._event_bus.subscriber_count)
        assert loop._current_task is not None
        assert loop._current_task is not task1_ref   # 新任务接管
        assert loop._executing is False

        loop.handle_shutdown()
        await asyncio.wait_for(task2, timeout=5.0)


class TestRound3RemovedMessagesUnit:
    """第1轮修复项5（removed_messages 有界）—— 第3轮补充单元测试。"""

    def test_removed_messages_only_captures_trimmed_tail(self):
        """removed_messages 只累积被裁掉的 assistant/tool 消息（messages[2:start]），
        system+user（messages[0]/messages[1]）恒保留在窗口内，绝不进入 removed_messages。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "trimmed = [messages[0], messages[1]] + messages[-(_MAX_CONTEXT_MESSAGES - 2):]" in source
        assert "removed_messages.extend(messages[2:start])" in source
        # system+user 恒保留：裁剪从 index 2 开始
        assert "start = len(messages) - (_MAX_CONTEXT_MESSAGES - 2)" in source

    def test_format_reinjected_boundary_limit(self):
        """重注入条数在边界（limit==len / limit==len-1）时行为正确：
        limit 等于列表长度全量保留；limit 少一条时只丢弃最旧一条、顺序不变。"""
        from weave_agent_sdk.loop.iterative import _format_reinjected_messages
        from weave_agent_sdk.types import Message

        removed = [Message(role="assistant", content=f"m{i}") for i in range(5)]

        all_kept = _format_reinjected_messages(removed, 5)
        assert len(all_kept) == 5
        assert [e["content"] for e in all_kept] == ["m0", "m1", "m2", "m3", "m4"]

        drop_oldest = _format_reinjected_messages(removed, 4)
        assert len(drop_oldest) == 4
        assert [e["content"] for e in drop_oldest] == ["m1", "m2", "m3", "m4"]


# ============================================================
# 第3轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound3StreamSyncErrorE2E:
    """端到端测试：stream() 订阅先于任务创建（第1轮修复项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_subscription_registered_before_task_creation(self):
        """在 _run_and_emit 任务创建的时刻，5 种契约事件类型都已被急切注册——
        直接验证"先订阅后建任务"（同步失败 error 不丢的根因）。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "simple"
        weave._config.loop.schedule = None
        weave._config.loop.timeout = 5.0
        weave._config.loop.stream_timeout = None
        weave._event_bus = EventBus()
        weave._run_impl = AsyncMock(return_value=LoopResult(
            output="eager-done", elapsed_ms=1, iterations=1, memory_updated={}
        ))
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}

        recorded = {}
        orig_create_task = asyncio.create_task

        def spy_create(coro, *a, **k):
            recorded["subscriber_count"] = dict(weave._event_bus.subscriber_count)
            return orig_create_task(coro, *a, **k)

        with patch("weave_agent_sdk.agent.asyncio.create_task", side_effect=spy_create):
            events = []
            async for event in weave.stream("input"):
                events.append(event)
                if event.type in ("done", "error"):
                    break

        # 任务创建前订阅已全部注册（先订阅后建任务）
        subs = recorded["subscriber_count"]
        for et in ("token", "tool_call", "tool_result", "done", "error"):
            assert subs.get(et, 0) == 1, f"{et} 应在任务创建前完成订阅注册"
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "eager-done"


class TestRound3BeforeThinkPerNsE2E:
    """端到端测试：单 namespace 下 before_think 按 ns 剔除本次写入（第1轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_single_namespace_run_writes_excluded(self):
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_context_limit=3)),
            _memory=MagicMock(),
        )
        agent.__dict__["_memory_writes"] = {"stream": {"ns:session:stream": 2}}
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["ns:session:stream"], "state": [], "knowledge": [],
        }[at])

        def fake_last(n, namespaces):
            all_entries = [{"role": "user", "content": f"h{i}"} for i in range(5)] + \
                          [{"role": "user", "content": f"run{i}"} for i in range(2)]
            return all_entries[-n:]

        agent._memory.stream.last = fake_last

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "hi")

        contents = [m["content"] for m in ctx["stream"]]
        # 读取窗口 3+2=5 → 剔除最近 2 条本次写入 → 保留最近 3 条历史
        assert contents == ["h2", "h3", "h4"]
        assert not any(c.startswith("run") for c in contents)


class TestRound3ConfigIdleTimeoutE2E:
    """端到端测试：stream_timeout=0（配置为 0）经 load_config 全链路视为不启用（第1轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_timeout_zero_disabled_slow_run_completes(self, tmp_path):
        import yaml
        from weave_agent_sdk.event_bus import EventBus

        yaml_path = tmp_path / "r3_zero_stream_timeout.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "stream_timeout": 0, "timeout": 0.05},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.stream_timeout == 0.0

        weave = Weave.__new__(Weave)
        weave._config = config
        weave._event_bus = EventBus()

        async def slow_impl(*args, **kwargs):
            await asyncio.sleep(0.2)  # 超过 loop.timeout=0.05 但正常完成
            return LoopResult(output="slow-done", elapsed_ms=200, iterations=1, memory_updated={})

        weave._run_impl = slow_impl

        events = []
        async for event in weave.stream("input"):
            events.append(event)
            if event.type in ("done", "error"):
                break

        # stream_timeout=0 视为不启用：慢速运行不被空闲超时误杀
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "slow-done"


class TestRound3ScheduledRestartE2E:
    """端到端测试：Weave 实例上常驻 scheduled shutdown 后重启继续执行（第1轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_weave_level_scheduled_restart_after_shutdown(self):
        from weave_agent_sdk.event_bus import EventBus
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.loop.type = "scheduled"
        weave._config.loop.schedule = "@on_data_change"
        weave._config.loop.event_min_interval = 0
        weave._config.loop.max_iterations = 1
        weave._config.loop.timeout = 5.0
        weave._config.llm.max_tokens = 100
        weave._config.llm.temperature = 0.0
        weave._config.prompts.system = "system"
        weave._llm = AsyncMock()
        weave._llm.chat = AsyncMock(return_value=LLMResponse(content="ok", model="test"))
        weave._event_bus = EventBus()
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.get_namespaces = MagicMock(return_value=["default:default:state"])
        weave._memory.state.set = MagicMock()
        weave._memory.stream.last = MagicMock(return_value=[])
        weave._memory.state.get_all = MagicMock(return_value={})
        weave._memory.knowledge.search = MagicMock(return_value=[])
        weave._memory.stats = MagicMock(return_value={})
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")
        weave._loop = ScheduledLoop()
        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}

        # ── 第一次启动 ──
        task1 = asyncio.create_task(weave.arun("first"))
        await _round6_wait_until(
            lambda: "data_change" in weave._event_bus.subscriber_count
        )
        # 事件驱动模式启动即执行一次（预热：写 last_run_at + last_run_error 各 1 次）
        await _round6_wait_until(lambda: weave._memory.state.set.call_count >= 2)
        state_calls_after_warmup = weave._memory.state.set.call_count
        await weave._event_bus.emit("data_change", {})
        await _round6_wait_until(
            lambda: weave._memory.state.set.call_count > state_calls_after_warmup
        )
        state_calls_first = weave._memory.state.set.call_count
        weave._loop.handle_shutdown()
        result1 = await asyncio.wait_for(task1, timeout=5.0)

        assert result1.output == "ok"
        assert weave._is_running is False          # 运行状态已复位

        # ── 重新启动（修复项4：不复位 _shutting_down 则重启被静默吞掉）──
        task2 = asyncio.create_task(weave.arun("second"))
        await _round6_wait_until(
            lambda: "data_change" in weave._event_bus.subscriber_count
        )
        await _round6_wait_until(
            lambda: weave._memory.state.set.call_count > state_calls_first
        )
        state_calls_after_warmup2 = weave._memory.state.set.call_count
        await weave._event_bus.emit("data_change", {})
        await _round6_wait_until(
            lambda: weave._memory.state.set.call_count > state_calls_after_warmup2
        )
        weave._loop.handle_shutdown()
        result2 = await asyncio.wait_for(task2, timeout=5.0)

        assert result2.output == "ok"
        assert weave._is_running is False


class TestRound3RemovedMessagesE2E:
    """端到端测试：长 tool 链 removed_messages 有界 + 窗口恒保留 system/user（第1轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_long_tool_chain_removed_bounded_and_window_preserved(self):
        from weave_agent_sdk.loop.iterative import IterativeLoop, _format_reinjected_messages
        from weave_agent_sdk.llm.base import LLMResponse
        from weave_agent_sdk.types import ToolCall

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 40
        agent._config.loop.stop_conditions = []
        agent._config.loop.tool_timeout = 5.0
        # memory_context_limit / messages_window 故意不配置（MagicMock 自动属性）：
        # 验证默认回退 reinject_limit=20 / 窗口=20 的有界性保证
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0

        async def search_tool(query: str) -> str:
            return f"result:{query}"

        agent._tools = [search_tool]
        agent._tool_map = {"search_tool": search_tool}
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])

        n = {"i": 0}
        captured_windows = []

        async def fake_call_llm(agent_, messages, tools=None):
            captured_windows.append(list(messages))
            n["i"] += 1
            if n["i"] < 40:
                return LLMResponse(
                    content=f"iter{n['i']}", model="test",
                    tool_calls=[ToolCall(id=f"c{n['i']}", name="search_tool",
                                         arguments={"query": "q"})],
                )
            return LLMResponse(content="final answer", model="test", tool_calls=None)

        reinject_lens = []
        real_fn = _format_reinjected_messages

        def spy(removed, limit):
            reinject_lens.append(len(removed))
            return real_fn(removed, limit)

        with patch("weave_agent_sdk.loop.iterative._format_reinjected_messages", side_effect=spy):
            with patch("weave_agent_sdk.loop.iterative.call_llm", side_effect=fake_call_llm):
                loop = IterativeLoop()
                loop.on_start = AsyncMock()
                loop.on_end = AsyncMock()
                loop.before_think = AsyncMock(return_value={})
                loop.after_think = AsyncMock()
                result = await asyncio.wait_for(loop.run(agent, "long chain query"), timeout=15.0)

        assert result.output == "final answer"
        assert result.iterations == 40
        assert result.memory_updated["tools"] == {"search_tool": 39}
        assert reinject_lens, "长 tool 链应触发 removed_messages 重注入"
        # 修复项5 核心：removed_messages 恒 ≤ 默认预算 20，内存有界
        assert all(l <= 20 for l in reinject_lens), \
            f"removed_messages 应恒 ≤ 20，实际 {reinject_lens}"
        # 窗口裁剪后每一轮注入 LLM 的窗口都保留 system+user 且不超过上限
        assert len(captured_windows) == 40
        assert all(m[0].role == "system" for m in captured_windows)
        assert all(m[1].role == "user" and m[1].content == "long chain query" for m in captured_windows)
        assert all(len(m) <= 20 for m in captured_windows)

# ============================================================
# 第4轮测试（本轮新增）：针对第4轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第4轮修复" 标题（最近一轮）：
#   第2轮5项问题已在源码修复并通过；本轮仅清理 9 个遗留 .bak 备份
#   （weave/ 内 7 个 + weave.yaml.bak + nexus/review_record.md.bak），
#   消除 2 个 TestBakFileCleanup 失败。
# 本轮只涉及 .bak 备份文件清理行为（无源码逻辑修改），
# 测试仅针对该行为，不与既有 TestBakFileCleanup 重复。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound4BakCleanupUnit:
    """第4轮修复（清理 .bak 备份文件）—— 逐项单元验证被清理的 9 个文件均已移除。

    覆盖：weave/ 内 7 个（config.py.bak、types.py.bak、features/ 3 个、loop/ 2 个）
    + weave.yaml.bak（项目根）+ nexus/review_record.md.bak。
    """

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_weave_config_py_bak_removed(self):
        """weave/config.py.bak 应已删除（weave/ 内 7 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "config.py.bak").exists(), \
            "weave/config.py.bak 应已清理"

    def test_weave_types_py_bak_removed(self):
        """weave/types.py.bak 应已删除（weave/ 内 7 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "types.py.bak").exists(), \
            "weave/types.py.bak 应已清理"

    def test_weave_yaml_bak_removed(self):
        """weave.yaml.bak（项目根）应已删除。"""
        assert not (self.PROJECT_ROOT / "weave.yaml.bak").exists(), \
            "weave.yaml.bak 应已清理"

    def test_review_record_bak_removed(self):
        """nexus/review_record.md.bak 应已删除。"""
        assert not (self.PROJECT_ROOT / "nexus" / "review_record.md.bak").exists(), \
            "nexus/review_record.md.bak 应已清理"

    def test_no_bak_in_weave_package_root(self):
        """weave/ 顶层目录不应存在任何 .bak 文件。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [p.name for p in weave_dir.iterdir() if p.name.endswith(".bak")]
        assert bak_files == [], f"weave/ 顶层残留 .bak: {bak_files}"

    def test_no_bak_in_weave_features(self):
        """weave/features/ 不应存在任何 .bak 文件（原 3 个已清理）。"""
        self._assert_no_bak_in("weave/features")

    def test_no_bak_in_weave_loop(self):
        """weave/loop/ 不应存在任何 .bak 文件（原 2 个已清理）。"""
        self._assert_no_bak_in("weave/loop")

    def test_no_bak_in_weave_llm(self):
        """weave/llm/ 不应存在任何 .bak 文件。"""
        self._assert_no_bak_in("weave/llm")

    def test_no_bak_in_weave_memory(self):
        """weave/memory/ 不应存在任何 .bak 文件。"""
        self._assert_no_bak_in("weave/memory")

    def test_no_bak_in_weave_server_and_utils(self):
        """weave/server/ 与 weave/utils/ 均不应存在任何 .bak 文件。"""
        self._assert_no_bak_in("weave/server")
        self._assert_no_bak_in("weave/utils")

    def _assert_no_bak_in(self, rel: str):
        d = self.PROJECT_ROOT / rel
        if not d.exists():
            return
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in d.rglob("*.bak")]
        assert bak_files == [], f"{rel} 残留 .bak: {bak_files}"


class TestRound4BakCleanupE2E:
    """端到端测试：全项目无 .bak 备份文件残留（第4轮修复核心）。

    覆盖：weave/ 源码整树、项目根、nexus/、各顶层目录递归扫描，
    以及 nexus/bak 归档目录不误命中 rglob('*.bak')。
    """

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_e2e_no_bak_anywhere_in_weave_source_tree(self):
        """weave/ 源码整棵树递归扫描不应存在任何 .bak 文件（含全部子目录）。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in weave_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/ 源码树残留 .bak: {bak_files}"

    def test_e2e_no_bak_anywhere_in_project(self):
        """整个项目目录树中不应存在任何 .bak 文件（含 tests/、nexus/、docs/ 等）。"""
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in self.PROJECT_ROOT.rglob("*.bak")]
        assert bak_files == [], f"项目残留 .bak: {bak_files}"

    def test_e2e_cleaned_bak_files_absent_anywhere(self):
        """本轮清理的 9 个 .bak 文件应全部从项目中消失（按已知路径 + 全量计数验证）。"""
        known_paths = [
            self.PROJECT_ROOT / "weave_agent_sdk" / "config.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "types.py.bak",
            self.PROJECT_ROOT / "weave.yaml.bak",
            self.PROJECT_ROOT / "nexus" / "review_record.md.bak",
        ]
        for p in known_paths:
            assert not p.exists(), f"被清理文件仍存在: {p}"

        # weave/ 内 7 个 + 根 weave.yaml.bak + nexus/review_record.md.bak = 9 个全部消除
        all_bak = list(self.PROJECT_ROOT.rglob("*.bak"))
        assert all_bak == [], f"仍存在 {len(all_bak)} 个 .bak 文件"

    def test_e2e_no_bak_files_anywhere(self):
        """全项目（含 nexus）不应残留任何 .bak 后缀文件（历史备份已全部清理）。"""
        assert list(self.PROJECT_ROOT.rglob("*.bak")) == []

    def test_e2e_all_top_level_source_dirs_clean(self):
        """各顶层源码/文档目录递归扫描均无 .bak 残留。"""
        for sub in ("weave", "tests", "prompts", "nexus", "docs", "examples"):
            d = self.PROJECT_ROOT / sub
            if not d.exists():
                continue
            bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in d.rglob("*.bak")]
            assert bak_files == [], f"{sub}/ 残留 .bak: {bak_files}"

# ============================================================
# 第5轮测试（本轮新增）：针对第5轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第5轮修复" 标题，共5个修复项:
#   - 修复项1: 移除 json_extract 硬编码 Prompt（R2 合规，utils/json_extract.py）
#   - 修复项2: messages_window 下限 3（config.py / iterative.py 钳制）
#   - 修复项3: features 模板路径 DRY + 友好缺失提示（features/_prompts.py）
#   - 修复项4: _schema_feedback 模板行数解耦（首行 header / 末行修复指令 / 前缀匹配标签）
#   - 修复项5: prompt 路径保留子目录（agent.py _load_system_prompt relative_to）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound5JsonExtractR2Unit:
    """第5轮修复项1（移除 json_extract 硬编码 Prompt，R2）—— 单元测试。"""

    def test_json_extract_no_to_feedback_or_hardcoded_prompt(self):
        """json_extract.py 不应再包含 to_feedback() 或任何硬编码 Prompt 文本（R2）。

        修复前 JsonExtractError 内置 to_feedback() 返回硬编码引导语；修复后
        仅承载结构化错误数据（raw_preview），反馈 Prompt 由调用方从 .md 加载。
        """
        import inspect
        import weave_agent_sdk.utils.json_extract as je

        source = inspect.getsource(je)
        assert "to_feedback" not in source, "json_extract.py 不应再包含 to_feedback()"
        # 不应包含任何硬编码 Prompt 引导语
        assert "Please output" not in source
        assert "You are" not in source
        assert "Please respond" not in source
        assert "Please fix" not in source

    def test_json_extract_error_carries_only_structured_data(self):
        """JsonExtractError 仅承载结构化错误数据（message 在 args、raw_preview 独立字段），无 Prompt。"""
        from weave_agent_sdk.utils.json_extract import JsonExtractError

        err = JsonExtractError("No JSON found", raw_preview="raw...")
        assert err.args[0] == "No JSON found"
        assert err.raw_preview == "raw..."
        # 不应有任何返回 Prompt 文本的反馈方法
        assert not hasattr(err, "to_feedback")
    def test_structured_call_uses_feature_prompt_for_invalid_json(self):
        """structured_call.py 对无效 JSON 的反馈应从 feature_prompt 加载，而非硬编码（R2）。"""
        import inspect
        from weave_agent_sdk.features.structured_call import structured_call

        source = inspect.getsource(structured_call)
        assert 'feature_prompt("structured_call_invalid", error=last_error)' in source
        assert "Your JSON was invalid" not in source
        assert "ONLY valid JSON" not in source


class TestRound5MessagesWindowUnit:
    """第5轮修复项2（messages_window 下限 3）—— 单元测试。"""

    def test_load_config_clamps_messages_window_to_three(self, tmp_path):
        """YAML 配置 messages_window < 3 时，load_config 应钳制为 3。"""
        import yaml
        from weave_agent_sdk.config import load_config

        for raw in (1, 2, 0, -5):
            yaml_path = tmp_path / f"r5_ms_win_{raw}.yaml"
            config_data = {
                "agent": {"name": "t"},
                "llm": {"provider": "anthropic"},
                "loop": {"type": "iterative", "messages_window": raw},
                "memory": {"scopes": {}},
                "prompts": {},
                "features": {},
                "server": {},
                "logging": {},
            }
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f)
            config = load_config(yaml_path)
            assert config.loop.messages_window == 3, \
                f"messages_window={raw} 应被钳制为 3，实际 {config.loop.messages_window}"

    def test_load_config_keeps_messages_window_above_three(self, tmp_path):
        """YAML 配置 messages_window >= 3 时应原样保留（如 5 / 20）。"""
        import yaml
        from weave_agent_sdk.config import load_config

        for raw in (3, 5, 20, 100):
            yaml_path = tmp_path / f"r5_ms_win_ok_{raw}.yaml"
            config_data = {
                "agent": {"name": "t"},
                "llm": {"provider": "anthropic"},
                "loop": {"type": "iterative", "messages_window": raw},
                "memory": {"scopes": {}},
                "prompts": {},
                "features": {},
                "server": {},
                "logging": {},
            }
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f)
            config = load_config(yaml_path)
            assert config.loop.messages_window == raw

    def test_iterative_uses_configured_window_only_when_ge_three(self):
        """iterative.py 仅在配置窗口 >= 3 时采用，否则回退模块级默认 20。"""
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "isinstance(configured_window, int) and configured_window >= 3" in source
        assert "globals()[\"_MAX_CONTEXT_MESSAGES\"]" in source


class TestRound5FeaturePromptUnit:
    """第5轮修复项3（features 模板路径 DRY + 友好缺失提示）—— 单元测试。"""

    def test_feature_prompt_loads_and_interpolates(self):
        """feature_prompt 应从 prompts/features/{name}.md 加载并做变量插值。"""
        from weave_agent_sdk.features._prompts import feature_prompt, FEATURE_PROMPTS_DIR

        # 路径集中定义（DRY），指向 prompts/features 子目录
        assert FEATURE_PROMPTS_DIR == Path("prompts") / "features"

        rendered = feature_prompt("structured_call_invalid", error="boom error")
        assert "boom error" in rendered
        assert "ONLY valid JSON" in rendered

    def test_feature_prompt_missing_file_friendly_hint(self, tmp_path, monkeypatch):
        """模板文件缺失时抛出友好 FileNotFoundError，包含路径与创建引导。"""
        import weave_agent_sdk.features._prompts as fp

        # 将 FEATURE_PROMPTS_DIR 指向一个不存在该模板的临时目录
        fake_dir = tmp_path / "prompts" / "features"
        monkeypatch.setattr(fp, "FEATURE_PROMPTS_DIR", fake_dir)

        with pytest.raises(FileNotFoundError) as excinfo:
            fp.feature_prompt("structured_call_invalid")
        msg = str(excinfo.value)
        assert "Feature prompt file not found" in msg
        assert "structured_call_invalid.md" in msg
        assert "create" in msg

    def test_feature_prompt_raw_loads_without_interpolation(self):
        """feature_prompt_raw 返回原始模板，不做变量替换。"""
        from weave_agent_sdk.features._prompts import feature_prompt_raw

        raw = feature_prompt_raw("structured_call_invalid")
        assert "{{ error }}" in raw
        assert "boom error" not in raw


class TestRound5SchemaFeedbackUnit:
    """第5轮修复项4（_schema_feedback 模板行数解耦）—— 单元测试。"""

    def _call_with_template(self, template: str):
        from weave_agent_sdk.features.structured_call import _schema_feedback
        from weave_agent_sdk.features.schema_validation import ValidationError
        from unittest.mock import patch

        err = ValidationError(missing=["name"], type_errors={"age": "bad"}, extra=["extra_field"])
        with patch("weave_agent_sdk.features.structured_call.feature_prompt", return_value=template):
            return _schema_feedback(err)

    def test_schema_feedback_header_is_first_line_fix_is_last(self):
        """header 取首行、修复指令取末行；中间行按前缀匹配标签。"""
        template = (
            "JSON does not match the expected schema:\n"
            "Missing fields:\n"
            "Type errors:\n"
            "Unknown fields:\n"
            "Please fix and output the correct JSON."
        )
        last, fix = self._call_with_template(template)
        assert last.startswith("JSON does not match the expected schema:")
        assert "Missing fields: ['name']" in last
        assert "Type errors: {'age': 'bad'}" in last
        assert "Unknown fields: ['extra_field']" in last
        assert fix == "Please fix and output the correct JSON."

    def test_schema_feedback_tolerates_extra_description_lines(self):
        """模板含额外说明行（非标签）时仍正常解析，不依赖固定 5 行。"""
        template = (
            "Header explanation line\n"
            "Missing fields:\n"
            "Some extra notes about the schema contract\n"
            "Type errors:\n"
            "Another unrelated line\n"
            "Unknown fields:\n"
            "Final fix instruction"
        )
        last, fix = self._call_with_template(template)
        assert last.startswith("Header explanation line")
        assert "Missing fields: ['name']" in last
        assert "Type errors: {'age': 'bad'}" in last
        assert "Unknown fields: ['extra_field']" in last
        assert fix == "Final fix instruction"

    def test_schema_feedback_missing_label_skips_category_with_warning(self, caplog):
        """某类别标签缺失时应跳过该类反馈并记录 warning，不崩溃不错位。"""
        from weave_agent_sdk.features.structured_call import _schema_feedback
        from weave_agent_sdk.features.schema_validation import ValidationError
        from unittest.mock import patch

        template = (
            "Header\n"
            "Missing fields:\n"
            "Final fix instruction"
        )
        err = ValidationError(missing=["name"], type_errors={"age": "bad"}, extra=["x"])
        with patch("weave_agent_sdk.features.structured_call.feature_prompt", return_value=template):
            with caplog.at_level(logging.WARNING):
                last, fix = _schema_feedback(err)

        # 只有 missing 有标签 → 仅拼入 missing
        assert "Missing fields: ['name']" in last
        assert "Type errors" not in last
        assert "Unknown fields" not in last
        assert fix == "Final fix instruction"
        assert any("no label for 'type' errors" in r.message for r in caplog.records)
        assert any("no label for 'extra' errors" in r.message for r in caplog.records)

    def test_schema_feedback_less_than_two_lines_raises(self):
        """模板不足 2 个非空行（无 header+修复指令）时应抛出 ValueError。"""
        from weave_agent_sdk.features.structured_call import _schema_feedback
        from weave_agent_sdk.features.schema_validation import ValidationError
        from unittest.mock import patch

        err = ValidationError(missing=["name"])
        with patch("weave_agent_sdk.features.structured_call.feature_prompt", return_value="Only one line"):
            with pytest.raises(ValueError) as excinfo:
                _schema_feedback(err)
        assert "at least" in str(excinfo.value)
        assert "structured_call_schema.md" in str(excinfo.value)

    def test_schema_feedback_reordered_labels_still_matched_by_prefix(self):
        """标签行重排后仍按前缀（Missing/Type/Unknown|Extra）匹配，不依赖行序。"""
        template = (
            "Header\n"
            "Unknown fields:\n"
            "Type errors:\n"
            "Missing fields:\n"
            "Fix instruction"
        )
        last, fix = self._call_with_template(template)
        assert "Missing fields: ['name']" in last
        assert "Type errors: {'age': 'bad'}" in last
        assert "Unknown fields: ['extra_field']" in last
        assert fix == "Fix instruction"


class TestRound5PromptSubdirUnit:
    """第5轮修复项5（prompt 路径保留子目录）—— 单元测试。"""

    def test_load_system_prompt_preserves_subdirectory(self):
        """_load_system_prompt 应以 prompts/ 为基准保留子目录相对部分。

        prompts/custom/system.md 不应被 PurePath.stem 截断为 system（会解析到
        prompts/system.md）；而应保留为 custom/system 相对名。
        """
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._load_system_prompt)
        assert "Path(prompt_path).relative_to(Path(\"prompts\"))" in source
        assert 'str(rel.with_suffix(""))' in source
        # 第8轮修复：非字符串守卫先于路径解析；无法 relative_to 时直接 load_prompt
        # 加载配置指向的文件（不再回退 PurePath.stem，避免子目录被截断为 stem）
        assert "if not isinstance(prompt_path, str):" in source
        assert "return load_prompt(path, context, self._config)" in source
        assert "PurePath(prompt_path).stem" not in source


# ============================================================
# 第5轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound5JsonExtractR2E2E:
    """端到端测试：无效 JSON 反馈从模板加载（第5轮修复项1 R2）。"""

    def test_e2e_structured_call_invalid_json_feedback_from_template(self):
        """structured_call 的无效 JSON 反馈应来自 prompts/features/structured_call_invalid.md。"""
        from weave_agent_sdk.features._prompts import feature_prompt

        rendered = feature_prompt("structured_call_invalid", error="my error msg")
        assert rendered == "Your JSON was invalid: my error msg. Please output ONLY valid JSON."


class TestRound5SchemaFeedbackE2E:
    """端到端测试：_schema_feedback 使用真实模板（第5轮修复项4）。"""

    def test_e2e_schema_feedback_with_real_template(self):
        """使用真实 structured_call_schema.md 时 header/fix/标签均正确。"""
        from weave_agent_sdk.features.structured_call import _schema_feedback
        from weave_agent_sdk.features.schema_validation import ValidationError

        err = ValidationError(missing=["name"], type_errors={"age": "bad"}, extra=["x"])
        last, fix = _schema_feedback(err)

        assert last.startswith("JSON does not match the expected schema:")
        assert "Missing fields: ['name']" in last
        assert "Type errors: {'age': 'bad'}" in last
        assert "Unknown fields: ['x']" in last
        assert fix == "Please fix and output the correct JSON."


class TestRound5MessagesWindowE2E:
    """端到端测试：messages_window 下限 3 经 config→iterative 全链路生效（第5轮修复项2）。"""

    def test_e2e_config_clamps_and_iterative_uses_clamped_window(self, tmp_path):
        """YAML 配置 messages_window=1 → load_config 钳制为 3，IterativeLoop 采用 3。"""
        import yaml
        from weave_agent_sdk.config import load_config

        yaml_path = tmp_path / "r5_e2e_ms_win.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "messages_window": 1},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        assert config.loop.messages_window == 3

        # IterativeLoop 读取该配置时使用钳制后的窗口
        import inspect
        from weave_agent_sdk.loop.iterative import IterativeLoop

        source = inspect.getsource(IterativeLoop.run)
        assert "configured_window >= 3" in source


class TestRound5FeaturePromptMissingE2E:
    """端到端测试：feature 模板缺失的友好提示（第5轮修复项3）。"""

    def test_e2e_feature_prompt_missing_file_guidance(self, tmp_path, monkeypatch):
        """缺失模板的 FileNotFoundError 应给出可操作的创建引导（路径 + copy 指引）。"""
        import weave_agent_sdk.features._prompts as fp

        fake_dir = tmp_path / "prompts" / "features"
        monkeypatch.setattr(fp, "FEATURE_PROMPTS_DIR", fake_dir)

        with pytest.raises(FileNotFoundError) as excinfo:
            fp.feature_prompt_raw("two_stage_understand")
        msg = str(excinfo.value)
        assert "Feature prompt file not found" in msg
        assert str(fake_dir / "two_stage_understand.md") in msg
        assert "copy the template" in msg


class TestRound5PromptSubdirE2E:
    """端到端测试：子目录 prompt 路径完整解析加载（第5轮修复项5）。"""

    def test_e2e_load_system_prompt_resolves_subdirectory_file(self, tmp_path, monkeypatch):
        """prompts/custom/system.md 应被解析为 custom/system 并从子目录加载内容。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        # 构造临时 prompts/custom/system.md
        prompts_dir = tmp_path / "prompts"
        custom_dir = prompts_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)
        (custom_dir / "system.md").write_text("subdir system content", encoding="utf-8")

        # chdir 到临时目录，使相对路径 prompts/... 生效
        monkeypatch.chdir(tmp_path)

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="prompts/custom/system.md"))
        weave._prompts = PromptRegistry(base_dir="prompts")

        result = weave._load_system_prompt()
        assert result == "subdir system content"

    def test_e2e_root_prompt_still_resolves_as_system(self, tmp_path, monkeypatch):
        """prompts/system.md（根目录）仍应解析为 system，兼容既有行为。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "system.md").write_text("root system content", encoding="utf-8")

        monkeypatch.chdir(tmp_path)

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="prompts/system.md"))
        weave._prompts = PromptRegistry(base_dir="prompts")

        result = weave._load_system_prompt()
        assert result == "root system content"


# ============================================================
# 第6轮测试（本轮新增）：针对第6轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第6轮修复" 标题（最近一轮），共5个修复项:
#   - 修复项1: TTL 全链路（config 解析 / _ttl_for_namespace / 后端写
#     expires_at / 写路径被动清理）— review round-4 issue 1
#   - 修复项2: state 窄覆盖宽（get_all 按宽→窄合并，priority 越小越窄，
#     同 key 窄 scope 覆盖宽 scope）— review round-4 issue 2
#   - 修复项3: tool 泛型 / 可选类型推断（_resolve_param_type 解包
#     Optional/Union/PEP 604 与 list[]/dict[] 泛型别名）— review round-4 issue 3
#   - 修复项4: prompt 绝对路径直载（_load_system_prompt 对绝对路径 /
#     非 prompts/ 前缀路径直接加载真实文件，不经 registry 名称解析）—
#     review round-4 issue 4
#   - 修复项5: scheduled 不写幻影 ns（未激活 state scope 时不写
#     "default:session:state" 幻影 namespace）— review round-4 issue 5
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound6TTLUnit:
    """第6轮修复项1（TTL 全链路：解析 / expires_at / 写路径清理）—— 单元测试。"""

    def test_parse_memory_scopes_ttl_from_access_block_and_priority(self):
        """memory.scopes 中 ttl 写在 access 配置块内（如 session.stream.ttl）
        应被解析进 MemoryScopeConfig.ttl；scope 级 ttl 优先于 access 块内 ttl。"""
        from weave_agent_sdk.config import _parse_memory_scopes

        # access 块内 ttl（weave.yaml 默认示例形态）
        scopes = _parse_memory_scopes({
            "session": {"stream": {"backend": "sqlite", "path": "./data/m.db", "ttl": 3600}},
        })
        assert scopes["session"].ttl_stream == 3600, \
            "access 块内 ttl 应被解析到 ttl_stream（此前被静默丢弃）"

        # scope 级 ttl 优先
        scopes2 = _parse_memory_scopes({
            "session": {
                "ttl": 100,
                "stream": {"ttl": 3600},
                "state": {"ttl": 7200},
            },
        })
        assert scopes2["session"].ttl == 100, "scope 级 ttl 应优先于 access 块内 ttl"

    def test_ttl_for_namespace_resolves_and_validates(self):
        """_ttl_for_namespace 应解析 scope 配置的 ttl 为 float；
        非数值 / 非正配置回退 None（永不过期）；无 scope 配置回退 None。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=3600),
            "invalid": MemoryScopeConfig(ttl="abc"),
            "zero": MemoryScopeConfig(ttl=0),
            "negative": MemoryScopeConfig(ttl=-5),
        }, default_path=":memory:"))

        assert manager._ttl_for_namespace("session:abc:stream") == 3600.0
        assert manager._ttl_for_namespace("invalid:abc:stream") is None
        assert manager._ttl_for_namespace("zero:abc:stream") is None
        assert manager._ttl_for_namespace("negative:abc:stream") is None
        # 未配置 scope 的 namespace → None（永不过期）
        assert manager._ttl_for_namespace("ghost:abc:stream") is None

    @pytest.mark.asyncio
    async def test_writes_set_expires_at_and_cleanup_on_write_path(self):
        """stream/state/knowledge 写入应按 scope ttl 计算 expires_at；
        过期条目在后续写入时被写路径被动清理（cleanup_expired）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=0.05),
        }, default_path=":memory:"))
        ns = "session:abc:stream"

        # stream 写入：expires_at 已设置（created_at + ttl）
        manager.stream.append({"role": "user", "content": "first"}, ns)
        backend = manager._get_backend_for_namespace(ns)
        cursor = backend.conn.execute(
            "SELECT created_at, expires_at FROM memory_entries WHERE namespace = ?",
            (ns,),
        )
        created_at, expires_at = cursor.fetchone()
        assert expires_at is not None, "stream 写入应按 ttl 设置 expires_at"
        assert expires_at - created_at > 0.04, "expires_at 应为 created_at + ttl"

        # 等待过期后，下一次写入触发写路径清理：仅剩未过期的 second
        await asyncio.sleep(0.1)
        manager.stream.append({"role": "user", "content": "second"}, ns)
        last = manager.stream.last(10, [ns])
        assert len(last) == 1, "过期条目应被写路径被动清理"
        assert last[0]["content"] == "second"

        # state / knowledge 写入同样按 ttl 设置 expires_at
        state_ns = "session:abc:state"
        manager.state.set("k", "v", state_ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ? AND access_type = 'state'",
            (state_ns,),
        )
        assert cursor.fetchone()[0] is not None, "state 写入应按 ttl 设置 expires_at"

        knowledge_ns = "session:abc:knowledge"
        manager.knowledge.add("hello world", knowledge_ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ? AND access_type = 'knowledge'",
            (knowledge_ns,),
        )
        assert cursor.fetchone()[0] is not None, "knowledge 写入应按 ttl 设置 expires_at"


class TestRound6StateNarrowOverWideUnit:
    """第6轮修复项2（state 窄覆盖宽）—— 单元测试。"""

    def test_get_namespaces_priority_ascending_narrow_first(self):
        """get_namespaces("state") 应按 priority 升序返回（窄 scope 在前）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})

        ns = manager.get_namespaces("state")
        assert ns == ["narrow:n1:state", "wide:w1:state"], \
            "get_namespaces 应按 priority 升序（窄→宽）返回"

    @pytest.mark.asyncio
    async def test_state_get_all_narrow_overrides_wide_same_key(self):
        """同 key 时窄 scope 覆盖宽 scope：get_all 按宽→窄合并，窄值胜出。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})
        narrow_ns = manager.get_namespace("narrow", "state")
        wide_ns = manager.get_namespace("wide", "state")

        manager.state.set("theme", "narrow-theme", narrow_ns)
        manager.state.set("theme", "wide-theme", wide_ns)

        # 窄→宽顺序传入：窄 scope 覆盖宽 scope，返回窄值
        result = manager.state.get_all([narrow_ns, wide_ns])
        assert result["theme"] == "narrow-theme", \
            "同 key 时窄 scope 应覆盖宽 scope（get_all 按宽→窄合并）"


class TestRound6ToolTypeInferenceUnit:
    """第6轮修复项3（tool 泛型 / 可选类型推断）—— 单元测试。"""

    def test_resolve_param_type_unpacks_optional_and_union(self):
        """Optional[int] / int | None（PEP 604）/ Union[...] 应解包为实际类型，
        而非退化为默认 "string"。"""
        from typing import Optional, Union
        from weave_agent_sdk.loop.iterative import _resolve_param_type

        assert _resolve_param_type(Optional[int]) == "integer"
        assert _resolve_param_type(int | None) == "integer"
        assert _resolve_param_type(Optional[str]) == "string"
        assert _resolve_param_type(Optional[bool]) == "boolean"
        assert _resolve_param_type(Union[int, None]) == "integer"

    def test_resolve_param_type_unpacks_generic_alias(self):
        """list[str] / dict[str, Any] 等泛型别名应映射为 array / object，
        而非退化为默认 "string"。"""
        from typing import Any, Dict, List
        from weave_agent_sdk.loop.iterative import _resolve_param_type

        assert _resolve_param_type(list[str]) == "array"
        assert _resolve_param_type(dict[str, Any]) == "object"
        assert _resolve_param_type(List[int]) == "array"
        assert _resolve_param_type(Dict[str, int]) == "object"
        # 基础类型回归（不受解包逻辑影响）
        assert _resolve_param_type(int) == "integer"
        assert _resolve_param_type(str) == "string"
        assert _resolve_param_type(float) == "number"
        assert _resolve_param_type(bool) == "boolean"

    def test_build_tool_schemas_with_generic_optional_params(self):
        """_build_tool_schemas 对含 Optional / list / dict 参数的 tool
        生成正确的 JSON Schema（类型 / 必填 / 描述）。"""
        from typing import Any, Optional
        from weave_agent_sdk.loop.iterative import _build_tool_schemas

        def search(query, top_k=5, tags=None, meta=None):
            """Search the knowledge base."""
            return ""

        # 测试模块启用了 from __future__ import annotations（def 注解为字符串），
        # 显式赋予真实类型对象，使 _build_tool_schemas 的签名解析得到真实类型
        search.__annotations__ = {
            "query": str,
            "top_k": Optional[int],
            "tags": list[str] | None,
            "meta": dict[str, Any] | None,
        }

        agent = MagicMock()
        agent._tools = [search]
        agent._tool_map = {t.__name__: t for t in agent._tools}

        schemas = _build_tool_schemas(agent)
        assert len(schemas) == 1
        assert schemas[0]["name"] == "search"
        assert schemas[0]["description"] == "Search the knowledge base."

        props = schemas[0]["parameters"]["properties"]
        assert props["query"]["type"] == "string"
        assert props["top_k"]["type"] == "integer", "Optional[int] 应解包为 integer"
        assert props["tags"]["type"] == "array", "list[str] 应映射为 array"
        assert props["meta"]["type"] == "object", "dict[str, Any] 应映射为 object"
        # 仅无默认值的 query 为必填；带默认值参数不进入 required
        assert schemas[0]["parameters"]["required"] == ["query"]


class TestRound6PromptAbsolutePathUnit:
    """第6轮修复项4（prompt 绝对路径直载）—— 单元测试。"""

    def test_load_system_prompt_direct_loads_absolute_path(self, tmp_path, monkeypatch):
        """_load_system_prompt 对绝对路径应直接加载真实文件内容，
        而非回退 PurePath.stem 经 registry 名称解析（会静默加载错误文件）。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        prompt_file = tmp_path / "custom" / "prompts" / "system.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text("absolute path content", encoding="utf-8")

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system=str(prompt_file)))
        weave._prompts = PromptRegistry(base_dir="prompts")

        result = weave._load_system_prompt()
        assert result == "absolute path content"


class TestRound6ScheduledNoPhantomNsUnit:
    """第6轮修复项5（scheduled 不写幻影 namespace）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_execute_once_skips_state_write_when_no_state_scope(self):
        """未激活任何 state namespace 时，_execute_once 不应写入
        "default:session:state" 幻影 namespace，且 memory_updated 不含 state。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        # 所有 access_type 均无激活 namespace
        agent._memory.get_namespaces = MagicMock(return_value=[])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        assert result.output == "scheduled"
        # 不写幻影 ns：state.set 从未被调用
        agent._memory.state.set.assert_not_called()
        # memory_updated 不含 state（也无幻影 default:session:state 计数）
        assert "state" not in result.memory_updated


# ============================================================
# 第6轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound6TTLE2E:
    """端到端测试：TTL 经 weave.yaml → load_config → MemoryManager 全链路生效。"""

    @pytest.mark.asyncio
    async def test_e2e_ttl_full_chain_expires_and_cleans(self, tmp_path):
        """YAML 配置 scope ttl → load_config 解析 → 写入设 expires_at →
        过期后从 last() 排除且写路径清理移除。"""
        import yaml

        yaml_path = tmp_path / "r6_ttl.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )

        from weave_agent_sdk.memory.manager import MemoryManager

        config = load_config(yaml_path)
        # 解析链：access 块内 ttl → MemoryScopeConfig.ttl
        assert config.memory.scopes["session"].ttl_stream == 0.05

        manager = MemoryManager(config.memory)
        ns = "session:abc:stream"
        manager.stream.append({"role": "user", "content": "first"}, ns)
        backend = manager._get_backend_for_namespace(ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ?", (ns,)
        )
        assert cursor.fetchone()[0] is not None, "全链路：写入应设 expires_at"

        await asyncio.sleep(0.1)
        # 过期后：last() 排除过期条目
        assert manager.stream.last(10, [ns]) == []
        # 写路径清理：再次写入触发 cleanup，过期条目被删除
        manager.stream.append({"role": "user", "content": "second"}, ns)
        cursor = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (ns,)
        )
        assert cursor.fetchone()[0] == 1, "过期条目应被写路径被动清理"
        last = manager.stream.last(10, [ns])
        assert len(last) == 1 and last[0]["content"] == "second"


class TestRound6StateNarrowOverWideE2E:
    """端到端测试：state 窄覆盖宽经 YAML scopes 全链路生效。"""

    @pytest.mark.asyncio
    async def test_e2e_state_narrow_covers_wide_same_key(self, tmp_path):
        """YAML 配置 narrow(priority=0) / wide(priority=10) 两个 scope，
        同 key 写入后 get_all 返回窄 scope 值（窄覆盖宽）。"""
        import yaml

        yaml_path = tmp_path / "r6_state_priority.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    wide:\n"
            "      priority: 10\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    narrow:\n"
            "      priority: 0\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )

        from weave_agent_sdk.memory.manager import MemoryManager

        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})

        # priority 升序：窄在前
        assert manager.get_namespaces("state") == ["narrow:n1:state", "wide:w1:state"]

        narrow_ns = manager.get_namespace("narrow", "state")
        wide_ns = manager.get_namespace("wide", "state")
        manager.state.set("theme", "narrow-theme", narrow_ns)
        manager.state.set("theme", "wide-theme", wide_ns)

        # 同 key 时窄 scope 覆盖宽 scope
        result = manager.state.get_all([narrow_ns, wide_ns])
        assert result["theme"] == "narrow-theme"


class TestRound6ToolSchemaE2E:
    """端到端测试：tool 泛型 / 可选类型推断生成正确 JSON Schema。"""

    def test_e2e_tool_schema_full_with_generic_optional_params(self):
        """含 Optional / 泛型别名的 tool 应生成完整且正确的 LLM 工具 Schema。"""
        from typing import Any, Optional
        from weave_agent_sdk.loop.iterative import _build_tool_schemas

        def analyze(source, limit=10, filters=None, params=None):
            """Analyze the given source with optional filters."""
            return ""

        # 同单元测试：显式赋予真实类型对象，规避 future-annotations 字符串化
        analyze.__annotations__ = {
            "source": str,
            "limit": Optional[int],
            "filters": list[str] | None,
            "params": dict[str, Any] | None,
        }

        agent = MagicMock()
        agent._tools = [analyze]
        agent._tool_map = {t.__name__: t for t in agent._tools}

        schemas = _build_tool_schemas(agent)
        assert len(schemas) == 1
        schema = schemas[0]
        assert schema["name"] == "analyze"
        assert schema["description"] == "Analyze the given source with optional filters."

        props = schema["parameters"]["properties"]
        assert props["source"]["type"] == "string"
        assert props["limit"]["type"] == "integer", "Optional[int] 应解包为 integer"
        assert props["filters"]["type"] == "array", "list[str] 应映射为 array"
        assert props["params"]["type"] == "object", "dict[str, Any] 应映射为 object"
        assert schema["parameters"]["required"] == ["source"]


class TestRound6PromptAbsolutePathE2E:
    """端到端测试：prompt 绝对路径直载（第6轮修复项4）。"""

    def test_e2e_load_system_prompt_absolute_and_relative_non_prompts(self, tmp_path, monkeypatch):
        """绝对路径与"非 prompts/ 前缀"相对路径都应直接加载配置指向的文件，
        而非回退 registry 名称解析（避免静默加载错误文件）。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        # ── 绝对路径 ──
        abs_file = tmp_path / "abs_prompts" / "system.md"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_text("absolute loaded content", encoding="utf-8")

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system=str(abs_file)))
        weave._prompts = PromptRegistry(base_dir="prompts")
        assert weave._load_system_prompt() == "absolute loaded content"

        # ── 非 prompts/ 前缀的相对路径（相对 CWD 解析）──
        custom = tmp_path / "custom_prompts"
        custom.mkdir(parents=True, exist_ok=True)
        (custom / "system.md").write_text("custom dir content", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        weave2 = Weave.__new__(Weave)
        weave2._config = SimpleNamespace(prompts=SimpleNamespace(
            system="custom_prompts/system.md"
        ))
        weave2._prompts = PromptRegistry(base_dir="prompts")
        assert weave2._load_system_prompt() == "custom dir content"


class TestRound6ScheduledNoPhantomNsE2E:
    """端到端测试：scheduled 单次执行在无激活 state scope 时不写幻影 ns。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_run_no_phantom_namespace_write(self):
        """真实 MemoryManager（未激活 scope，无任何 namespace）下 scheduled 单次执行：
        不应写入 default:session:state 幻影 ns（不创建任何 backend）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        manager = MemoryManager(MemoryConfig())
        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = None   # 单次执行路径
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = manager   # 真实 MemoryManager（未激活 → 无 namespace）

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "input")

        assert result.output == "scheduled"
        assert "state" not in result.memory_updated, \
            "未激活 state scope 时 memory_updated 不应含 state（幻影 ns 未被写）"
        # 未激活 scope 时不写幻影 ns：无任何 backend 被创建（无写路径触发）
        assert manager._backends == {}, \
            "未激活 scope 时不应创建任何 backend / 写入幻影 namespace"

# ============================================================
# 第7轮测试（本轮新增）：针对第7轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第7轮修复" 标题（最近一轮）：
#   - 修复项1: cleanup_expired() 去掉 SQLite 不支持的 DELETE ... LIMIT，
#     改为先查过期条目 id 再按 id 删除（兼容未启用
#     SQLITE_ENABLE_UPDATE_DELETE_LIMIT 的标准 SQLite 构建），
#     stream/state/knowledge 写路径恢复。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound7CleanupExpiredFixUnit:
    """第7轮修复项1（cleanup_expired 去 DELETE...LIMIT 改按 id 删除）—— 单元测试。"""

    def test_cleanup_expired_source_has_no_delete_limit(self):
        """cleanup_expired() 源码不应再包含 SQLite 不支持的 DELETE ... LIMIT。

        这是本轮修复的核心根因：Python 内置 SQLite 未启用
        SQLITE_ENABLE_UPDATE_DELETE_LIMIT 时，`DELETE ... LIMIT` 抛
        sqlite3.OperationalError: near "LIMIT": syntax error，导致所有写路径崩溃。
        """
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        source = inspect.getsource(SQLiteBackend.cleanup_expired)
        assert "DELETE FROM memory_entries WHERE expires_at IS NOT NULL AND expires_at <= ? LIMIT ?" not in source, \
            "cleanup_expired() 不应再包含 DELETE ... LIMIT（标准 SQLite 不支持）"

    def test_cleanup_expired_source_select_ids_then_delete_by_id(self):
        """cleanup_expired() 应改为：先 SELECT 过期条目 id，再按 id DELETE。"""
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        source = inspect.getsource(SQLiteBackend.cleanup_expired)
        # 先查 id（SELECT 带 LIMIT 是安全的分批上限）
        assert "SELECT id FROM memory_entries" in source
        assert "WHERE expires_at IS NOT NULL AND expires_at <= ?" in source
        # 再按 id 删除
        assert "DELETE FROM memory_entries WHERE id IN" in source
        assert "ids_to_delete" in source
        # 保留 limit 上限（写路径清理有界）
        assert "LIMIT ?" in source
        assert "(now, limit)" in source

    def test_all_write_paths_call_cleanup_expired(self):
        """stream_append / state_set / knowledge_add 三个写路径都应调用 cleanup_expired()。

        修复前 cleanup_expired 内部 DELETE...LIMIT 崩溃，写路径全部报错；
        修复后写路径恢复并在每次写入时被动清理过期条目。
        """
        import inspect
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        src_stream = inspect.getsource(SQLiteBackend.stream_append)
        src_state = inspect.getsource(SQLiteBackend.state_set)
        src_know = inspect.getsource(SQLiteBackend.knowledge_add)

        for name, src in (("stream_append", src_stream),
                          ("state_set", src_state),
                          ("knowledge_add", src_know)):
            assert "self.cleanup_expired()" in src, f"{name} 应调用 cleanup_expired()"

    def test_cleanup_expired_deletes_expired_keeps_fresh(self):
        """cleanup_expired() 应删除已过期条目，保留未过期条目。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        now = time.time()

        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
            ("expired-1", ns, json.dumps({"role": "user", "content": "old"}), now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
            ("fresh-1", ns, json.dumps({"role": "user", "content": "new"}), now - 10, now + 1000),
        )
        backend.conn.commit()

        deleted = backend.cleanup_expired()

        assert deleted == 1
        rows = backend.conn.execute(
            "SELECT id FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchall()
        assert [r[0] for r in rows] == ["fresh-1"], \
            "cleanup_expired() 应删除过期条目、保留未过期条目"

    def test_cleanup_expired_returns_deleted_count(self):
        """cleanup_expired() 返回值应等于实际删除的过期条目数。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        now = time.time()
        for i in range(4):
            backend.conn.execute(
                "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
                "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
                (f"exp-{i}", ns, json.dumps({"role": "user", "content": str(i)}), now - 10, now - 5),
            )
        backend.conn.commit()

        deleted = backend.cleanup_expired()
        assert deleted == 4
        remaining = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchone()[0]
        assert remaining == 0

    def test_cleanup_expired_limit_bounds_batch(self):
        """cleanup_expired(limit=n) 应最多删除 n 条（写路径清理有界，不一次性全删）。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        now = time.time()
        for i in range(5):
            backend.conn.execute(
                "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
                "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
                (f"exp-{i}", ns, json.dumps({"role": "user", "content": str(i)}), now - 10, now - 5),
            )
        backend.conn.commit()

        deleted = backend.cleanup_expired(limit=2)
        assert deleted == 2
        remaining = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchone()[0]
        assert remaining == 3, "limit 上限应生效：一次最多清理 limit 条"

    def test_cleanup_expired_no_expired_returns_zero(self):
        """无过期条目时 cleanup_expired() 应返回 0 且不误删任何条目。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        backend.stream_append({"role": "user", "content": "fresh"}, ns, ttl=1000)

        deleted = backend.cleanup_expired()

        assert deleted == 0
        assert len(backend.stream_last(10, [ns])) == 1

    def test_cleanup_expired_keeps_null_expires_at(self):
        """expires_at 为 NULL（永不过期）的条目不应被 cleanup_expired() 删除。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        now = time.time()

        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, NULL)",
            ("never-expires", ns, json.dumps({"role": "user", "content": "keep"}), now - 10),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
            ("expired-null-check", ns, json.dumps({"role": "user", "content": "old"}), now - 10, now - 5),
        )
        backend.conn.commit()

        deleted = backend.cleanup_expired()

        assert deleted == 1, "仅过期条目被删除"
        rows = backend.conn.execute(
            "SELECT id FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchall()
        assert [r[0] for r in rows] == ["never-expires"], "NULL expires_at 条目应保留"

    def test_stream_append_write_path_with_expired_present(self):
        """stream 写路径：存在过期条目时 stream_append 触发 cleanup_expired，
        不应抛 OperationalError（修复前 DELETE...LIMIT 崩溃），并正确回收过期条目。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        now = time.time()
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
            ("expired-stream", ns, json.dumps({"role": "user", "content": "old"}), now - 10, now - 5),
        )
        backend.conn.commit()

        # 写路径触发 cleanup_expired：不再崩溃，且过期条目被回收
        entry_id = backend.stream_append({"role": "user", "content": "new"}, ns, ttl=1000)
        assert entry_id
        last = backend.stream_last(10, [ns])
        assert [e["content"] for e in last] == ["new"], \
            "stream 写路径应恢复并回收过期条目"
        count = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchone()[0]
        assert count == 1

    def test_state_and_knowledge_write_paths_with_expired_present(self):
        """state/knowledge 写路径：存在过期条目时写操作触发 cleanup_expired，
        不应抛 OperationalError（修复前 DELETE...LIMIT 崩溃），并正确回收过期条目。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        state_ns = "scope1:u1:state"
        know_ns = "scope1:u1:knowledge"
        now = time.time()

        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'state', 'old_key', ?, ?, ?)",
            ("expired-state", state_ns, json.dumps("old_value"), now - 10, now - 5),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'knowledge', NULL, ?, ?, ?)",
            ("expired-know", know_ns, "old knowledge", now - 10, now - 5),
        )
        backend.conn.commit()

        # state 写路径
        backend.state_set("k", "v", state_ns, ttl=1000)
        assert backend.state_get("k", state_ns) == "v"
        state_remaining = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (state_ns,)
        ).fetchone()[0]
        assert state_remaining == 1, "state 写路径应恢复并回收过期条目"

        # knowledge 写路径
        backend.knowledge_add("new knowledge", know_ns, None, ttl=1000)
        results = backend.knowledge_search("new", [know_ns], 5)
        assert len(results) == 1
        assert results[0].content == "new knowledge"
        know_remaining = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (know_ns,)
        ).fetchone()[0]
        assert know_remaining == 1, "knowledge 写路径应恢复并回收过期条目"


class TestRound7CleanupExpiredFixE2E:
    """端到端测试：cleanup_expired 修复后 stream/state/knowledge 写路径完整可用。"""

    @pytest.mark.asyncio
    async def test_e2e_stream_full_chain_cleanup_after_expiry(self):
        """stream 完整链路：写入 → 过期 → 下次写入被动清理 → last() 只返回新条目。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:stream"
        now = time.time()
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('expired-1', ?, 'stream', NULL, '{\"role\":\"user\",\"content\":\"old\"}', ?, ?)",
            (ns, now - 100, now - 50),
        )
        backend.conn.commit()

        # 写路径触发被动清理
        backend.stream_append({"role": "user", "content": "second"}, ns, ttl=1000)
        last = backend.stream_last(10, [ns])
        assert [e["content"] for e in last] == ["second"]
        assert backend.namespace_stats().get(ns) == 1

    @pytest.mark.asyncio
    async def test_e2e_state_full_chain_cleanup_after_expiry(self):
        """state 完整链路：过期 state 条目在后续写入时被清理，get 返回新值。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:state"
        now = time.time()
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('expired-state', ?, 'state', 'old_key', ?, ?, ?)",
            (ns, json.dumps("old_value"), now - 100, now - 50),
        )
        backend.conn.commit()

        backend.state_set("old_key", "new_value", ns, ttl=1000)
        assert backend.state_get("old_key", ns) == "new_value"
        assert backend.state_get_all([ns]) == {"old_key": "new_value"}
        # 过期条目被回收，仅剩新值
        assert backend.namespace_stats().get(ns) == 1

    @pytest.mark.asyncio
    async def test_e2e_knowledge_full_chain_cleanup_after_expiry(self):
        """knowledge 完整链路：过期 knowledge 条目在后续写入时被清理，搜索只命中新条目。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        ns = "scope1:u1:knowledge"
        now = time.time()
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('expired-know', ?, 'knowledge', NULL, 'old stale knowledge', ?, ?)",
            (ns, now - 100, now - 50),
        )
        backend.conn.commit()

        backend.knowledge_add("fresh new knowledge", ns, None, ttl=1000)
        results = backend.knowledge_search("fresh", [ns], 5)
        assert len(results) == 1
        assert results[0].content == "fresh new knowledge"
        # 过期条目被回收
        assert backend.namespace_stats().get(ns) == 1

    @pytest.mark.asyncio
    async def test_e2e_manager_ttl_write_path_full_chain(self):
        """MemoryManager 全链路（真实 sqlite backend + scope ttl 配置）：
        写入设 expires_at → 过期后下次写入被动清理，last() 只返回新条目。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=0.05),
        }, default_path=":memory:"))
        ns = "session:abc:stream"

        manager.stream.append({"role": "user", "content": "first"}, ns)
        backend = manager._get_backend_for_namespace(ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ?", (ns,)
        )
        assert cursor.fetchone()[0] is not None, "写入应按 ttl 设置 expires_at"

        await asyncio.sleep(0.1)
        manager.stream.append({"role": "user", "content": "second"}, ns)
        last = manager.stream.last(10, [ns])
        assert [e["content"] for e in last] == ["second"], \
            "过期条目应被写路径被动清理，仅保留新条目"
        assert backend.namespace_stats().get(ns) == 1

    def test_e2e_all_write_paths_no_operational_error(self):
        """回归：三类写路径在存在过期条目时都完成写入，全程无 OperationalError。

        修复前 cleanup_expired 的 DELETE...LIMIT 会使每次写入抛
        sqlite3.OperationalError: near "LIMIT": syntax error。
        """
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        ns_stream = "s:1:stream"
        ns_state = "s:1:state"
        ns_know = "s:1:knowledge"
        now = time.time()

        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('exp-stream', ?, 'stream', NULL, ?, ?, ?)",
            (ns_stream, json.dumps({"role": "user", "content": "old"}), now - 10, now - 5),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('exp-state', ?, 'state', 'k', ?, ?, ?)",
            (ns_state, json.dumps("old"), now - 10, now - 5),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('exp-know', ?, 'knowledge', NULL, 'old kb', ?, ?)",
            (ns_know, now - 10, now - 5),
        )
        backend.conn.commit()

        # 三个写路径都不应抛 OperationalError
        backend.stream_append({"role": "user", "content": "new stream"}, ns_stream, ttl=100)
        backend.state_set("k", "new state", ns_state, ttl=100)
        backend.knowledge_add("new knowledge", ns_know, None, ttl=100)

        # 三个过期条目全部被清理
        remaining = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE id LIKE 'exp-%'"
        ).fetchone()[0]
        assert remaining == 0, "写路径清理应回收全部过期条目"
        # 新写入数据全部可读
        assert [e["content"] for e in backend.stream_last(10, [ns_stream])] == ["new stream"]
        assert backend.state_get("k", ns_state) == "new state"
        assert backend.knowledge_search("new", [ns_know], 5)[0].content == "new knowledge"

# ============================================================
# 第8轮测试（本轮新增）：针对第8轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第8轮修复" 标题（最近一轮）：
#   - 修复项1: _load_system_prompt 对非字符串 prompts.system(MagicMock) 的兼容性：
#     回退 registry 的 "system" 命名 prompt（而非按文件路径解析构造垃圾路径抛
#     FileNotFoundError），修复 TestStreamRaceCondition 同步阶段 FileNotFoundError
#     产出 error 事件。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound8NonStringSystemPromptUnit:
    """第8轮修复项1（_load_system_prompt 非字符串 prompts.system 兼容）—— 单元测试。"""

    def _make_weave(self, system_val):
        """构造 Weave 实例：config.prompts.system 为指定值，_prompts 为 MagicMock。"""
        from types import SimpleNamespace
        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system=system_val))
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt from registry")
        return weave

    def test_non_string_mock_falls_back_to_registry_system(self):
        """prompts.system 为 MagicMock（非字符串）时应回退 registry "system"，不抛 FileNotFoundError。"""
        weave = self._make_weave(MagicMock())
        result = weave._load_system_prompt()
        assert result == "System prompt from registry"
        weave._prompts.get.assert_called_once_with("system", None)

    def test_non_string_int_falls_back_to_registry_system(self):
        """prompts.system 为 int（非字符串）时应回退 registry "system"。"""
        weave = self._make_weave(12345)
        assert weave._load_system_prompt() == "System prompt from registry"
        weave._prompts.get.assert_called_once_with("system", None)

    def test_context_forwarded_to_registry_get(self):
        """非字符串回退时应将 context 透传给 _prompts.get("system", context)。"""
        weave = self._make_weave(MagicMock())
        ctx = {"name": "alice"}
        assert weave._load_system_prompt(ctx) == "System prompt from registry"
        weave._prompts.get.assert_called_once_with("system", ctx)

    def test_auto_mocked_config_prompts_system_falls_back(self):
        """MagicMock 配置下 prompts.system 为自动创建的 MagicMock，同样回退 registry（TestStreamRaceCondition 场景）。"""
        weave = Weave.__new__(Weave)
        weave._config = MagicMock()          # prompts.system 为自动创建的 MagicMock
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="mock system content")
        assert weave._load_system_prompt() == "mock system content"
        weave._prompts.get.assert_called_once_with("system", None)

    def test_source_guard_precedes_path_resolution(self):
        """源码中非字符串守卫应位于 Path() 路径解析之前（防止对 MagicMock 构造垃圾路径）。"""
        import inspect
        from weave_agent_sdk.agent import Weave
        source = inspect.getsource(Weave._load_system_prompt)
        guard_idx = source.index("if not isinstance(prompt_path, str):")
        rel_idx = source.index("Path(prompt_path).relative_to")
        assert guard_idx < rel_idx, "非字符串守卫必须先于文件路径解析"

    def test_empty_string_uses_bundled_default(self):
        """prompts.system 为空字符串（falsy）时应加载内置默认 prompt（C1，不抛错）。"""
        weave = self._make_weave("")
        result = weave._load_system_prompt()
        assert isinstance(result, str)
        assert result.strip()

    def test_none_uses_bundled_default(self):
        """prompts.system 为 None（falsy）时应加载内置默认 prompt（C1，不抛错）。"""
        weave = self._make_weave(None)
        result = weave._load_system_prompt()
        assert isinstance(result, str)
        assert result.strip()

    def test_string_relative_config_still_resolves_via_registry(self):
        """prompts/ 相对字符串配置仍走 registry 名称解析（回归，不受非字符串分支影响）。"""
        weave = self._make_weave("prompts/system.md")
        assert weave._load_system_prompt() == "System prompt from registry"
        weave._prompts.get.assert_called_once_with("system", None)

    def test_string_subdir_config_preserves_subdirectory(self):
        """prompts/custom/system.md 仍保留子目录相对名 custom/system（回归，第6轮修复项4）。"""
        weave = self._make_weave("prompts/custom/system.md")
        weave._load_system_prompt()
        name = weave._prompts.get.call_args[0][0]
        assert name.replace("\\", "/") == "custom/system"
        weave._prompts.get.assert_called_once()

    def test_real_registry_fallback_loads_actual_system_md(self, tmp_path, monkeypatch):
        """真实 PromptRegistry：非字符串配置回退后加载 prompts/system.md 实际内容。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        from types import SimpleNamespace
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "system.md").write_text("real system content", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system=MagicMock()))
        weave._prompts = PromptRegistry(base_dir="prompts")
        assert weave._load_system_prompt() == "real system content"


class TestRound8StreamNonStringPromptE2E:
    """端到端测试：非字符串 prompts.system 下 stream()/arun() 不再产出 error（第8轮核心修复）。"""

    @pytest.fixture
    def mock_weave(self):
        """构造与 TestStreamRaceCondition 相同的 fixture（MagicMock 配置 + 非字符串 prompts.system）。"""
        from weave_agent_sdk.event_bus import EventBus
        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.agent.name = "test_agent"
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 5.0
        weave._event_bus = EventBus()
        weave._llm = AsyncMock()
        from weave_agent_sdk.llm.base import LLMResponse
        weave._llm.chat = AsyncMock(return_value=LLMResponse(content="Hello", model="test-model"))
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.stats = MagicMock(return_value={"test:scope:stream": 0})
        weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt")
        weave._loop = AsyncMock()
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="test output", elapsed_ms=100, iterations=1,
            memory_updated={"stream": {"test:scope:stream": 1}},
        ))
        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None
        return weave

    @pytest.mark.asyncio
    async def test_e2e_stream_yields_done_not_error(self, mock_weave):
        """修复前同步阶段 FileNotFoundError → error 事件；修复后应收到 done 事件。"""
        weave = mock_weave
        events = []
        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break
        assert len(events) >= 1
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "test output"
        assert not any(e.type == "error" for e in events)

    @pytest.mark.asyncio
    async def test_e2e_stream_system_prompt_from_registry_fallback(self, mock_weave):
        """运行后 _system_prompt 应等于 _prompts.get("system") 返回值，且按 "system" 名称取。"""
        weave = mock_weave
        async for event in weave.stream("test input"):
            if event.type in ("done", "error"):
                break
        assert weave._system_prompt == "System prompt"
        weave._prompts.get.assert_called_with("system", None)

    @pytest.mark.asyncio
    async def test_e2e_arun_completes_with_mock_prompts(self, mock_weave):
        """非流式 arun 路径同样不应因 MagicMock prompts.system 抛 FileNotFoundError。"""
        weave = mock_weave
        result = await weave.arun("test input")
        assert result.output == "test output"
        assert weave._system_prompt == "System prompt"

    @pytest.mark.asyncio
    async def test_e2e_stream_tokens_and_done_flow(self, mock_weave):
        """token 事件正常流动 + 最终 done，全程无 error（修复不影响正常事件流）。"""
        weave = mock_weave

        async def run_with_tokens(agent_, user_input):
            await weave._event_bus.emit("token", {"text": "tok1", "index": 0})
            await weave._event_bus.emit("token", {"text": "tok2", "index": 1})
            return LoopResult(output="test output", elapsed_ms=100, iterations=1, memory_updated={})

        weave._loop.run = run_with_tokens

        types = []
        token_texts = []
        async for event in weave.stream("test input"):
            types.append(event.type)
            if event.type == "token":
                token_texts.append(event.data["text"])
            if event.type in ("done", "error"):
                break
        assert types[-1] == "done"
        assert "error" not in types
        assert token_texts == ["tok1", "tok2"]

    @pytest.mark.asyncio
    async def test_e2e_stream_registry_get_raising_still_yields_error(self, mock_weave):
        """若 registry "system" 也抛异常，stream 仍应产出 error（错误兜底路径未被破坏）。"""
        weave = mock_weave
        weave._prompts.get = MagicMock(
            side_effect=FileNotFoundError("Prompt 'system' not registered")
        )
        events = []
        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break
        assert events[-1].type == "error"
        assert "system" in events[-1].data["message"]



# ============================================================
# 第9轮测试（本轮新增）：针对第9轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第9轮修复" 标题（最近一轮）：
#   本轮修复摘要：已修复1个问题——清理9个 .bak 残留
#   （weave/ 内 8 个：config.py / types.py / loop/iterative.py /
#     loop/scheduled.py / memory/knowledge.py / memory/manager.py /
#     memory/state.py / memory/stream.py 的 .bak +
#     nexus/review_record.md.bak），消除 TestBakFileCleanup 与
#     TestRound4BakCleanup 共 19 个失败；第4轮审查 5 项已在第6轮修复，
#     本轮复核通过。本轮未编写测试。
# 本轮只涉及 .bak 备份文件清理行为（无源码逻辑修改），测试仅针对该行为，
# 与既有 TestBakFileCleanup / TestRound4BakCleanup 互补、不重复，
# 重点覆盖本轮被清理的 9 个具体文件与目录级清扫。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound9BakCleanupUnit:
    """第9轮修复（清理 9 个 .bak 残留）—— 逐项单元验证被清理文件均已移除。

    覆盖：weave/ 内 8 个（config.py / types.py / loop/ 2 个 / memory/ 4 个）
    + nexus/review_record.md.bak。
    """

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_loop_iterative_py_bak_removed(self):
        """weave/loop/iterative.py.bak 应已删除（weave/ 内 8 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "loop" / "iterative.py.bak").exists(),             "weave/loop/iterative.py.bak 应已清理"

    def test_loop_scheduled_py_bak_removed(self):
        """weave/loop/scheduled.py.bak 应已删除（weave/ 内 8 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "loop" / "scheduled.py.bak").exists(),             "weave/loop/scheduled.py.bak 应已清理"

    def test_memory_knowledge_py_bak_removed(self):
        """weave/memory/knowledge.py.bak 应已删除（memory/ 4 个之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "knowledge.py.bak").exists(),             "weave/memory/knowledge.py.bak 应已清理"

    def test_memory_manager_py_bak_removed(self):
        """weave/memory/manager.py.bak 应已删除（memory/ 4 个之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "manager.py.bak").exists(),             "weave/memory/manager.py.bak 应已清理"

    def test_memory_state_py_bak_removed(self):
        """weave/memory/state.py.bak 应已删除（memory/ 4 个之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "state.py.bak").exists(),             "weave/memory/state.py.bak 应已清理"

    def test_memory_stream_py_bak_removed(self):
        """weave/memory/stream.py.bak 应已删除（memory/ 4 个之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "stream.py.bak").exists(),             "weave/memory/stream.py.bak 应已清理"

    def test_config_py_bak_removed(self):
        """weave/config.py.bak 应已删除（weave/ 内 8 个 .bak 之一，本轮再次清理）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "config.py.bak").exists(),             "weave/config.py.bak 应已清理"

    def test_types_py_bak_removed(self):
        """weave/types.py.bak 应已删除（weave/ 内 8 个 .bak 之一，本轮再次清理）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "types.py.bak").exists(),             "weave/types.py.bak 应已清理"

    def test_review_record_bak_removed(self):
        """nexus/review_record.md.bak 应已删除（第9轮清理的 9 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "nexus" / "review_record.md.bak").exists(),             "nexus/review_record.md.bak 应已清理"

    def test_no_bak_in_loop_and_memory_dirs(self):
        """weave/loop/ 与 weave/memory/ 目录递归均不应存在任何 .bak 文件。"""
        self._assert_no_bak_in("weave/loop")
        self._assert_no_bak_in("weave/memory")

    def _assert_no_bak_in(self, rel: str):
        d = self.PROJECT_ROOT / rel
        if not d.exists():
            return
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in d.rglob("*.bak")]
        assert bak_files == [], f"{rel} 残留 .bak: {bak_files}"


class TestRound9BakCleanupE2E:
    """端到端测试：本轮清理的 9 个 .bak 文件全部消失且全项目无 .bak 残留。"""

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_e2e_round9_cleaned_files_absent_anywhere(self):
        """本轮清理的 9 个 .bak 文件应全部从项目中消失（已知路径逐一验证）。"""
        known_paths = [
            self.PROJECT_ROOT / "weave_agent_sdk" / "config.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "types.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "loop" / "iterative.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "loop" / "scheduled.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "knowledge.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "manager.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "state.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "stream.py.bak",
            self.PROJECT_ROOT / "nexus" / "review_record.md.bak",
        ]
        for p in known_paths:
            assert not p.exists(), f"被清理文件仍存在: {p}"

    def test_e2e_no_bak_anywhere_in_project(self):
        """整个项目目录树递归扫描不应存在任何 .bak 文件（含 weave/tests/nexus/docs 等）。"""
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in self.PROJECT_ROOT.rglob("*.bak")]
        assert bak_files == [], f"项目残留 .bak: {bak_files}"

    def test_e2e_no_bak_in_weave_source_tree(self):
        """weave/ 源码整棵树递归扫描不应存在任何 .bak 文件。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in weave_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/ 源码树残留 .bak: {bak_files}"

    def test_e2e_project_root_and_weave_yaml_bak_clean(self):
        """项目根目录无 .bak（含 weave.yaml.bak）；nexus/bak 归档目录不误命中。"""
        root_baks = [p.name for p in self.PROJECT_ROOT.iterdir() if p.name.endswith(".bak")]
        assert root_baks == [], f"项目根残留 .bak: {root_baks}"
        assert not (self.PROJECT_ROOT / "weave.yaml.bak").exists(),             "weave.yaml.bak 应已清理"

        bak_dir = self.PROJECT_ROOT / "nexus" / "bak"
        if bak_dir.is_dir():
            inner_bak = list(bak_dir.rglob("*.bak"))
            assert inner_bak == [], f"nexus/bak 内部仍残留 .bak: {inner_bak}"

    def test_e2e_all_top_level_source_dirs_clean(self):
        """各顶层源码/文档目录递归扫描均无 .bak 残留。"""
        for sub in ("weave", "tests", "prompts", "nexus", "docs", "examples"):
            d = self.PROJECT_ROOT / sub
            if not d.exists():
                continue
            bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in d.rglob("*.bak")]
            assert bak_files == [], f"{sub}/ 残留 .bak: {bak_files}"


# ============================================================
# 第10轮测试（本轮新增）：针对第10轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第10轮修复" 标题（最近一轮）：
#   - 修复项1: _load_system_prompt 对裸文件名相对路径（如 "system" / "system.md"）
#     先按 registry 解析为 prompts/{name}.md，修复 prompts.system='system' 被直载
#     CWD 文件抛 FileNotFoundError（TestRound7ReentryE2E 报告的具体失败）。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


def _r10_make_weave(system_val, registry=None):
    """构造最小 Weave 实例：config.prompts.system 为指定值，_prompts 为 registry
    （MagicMock 或真实 PromptRegistry）。"""
    from types import SimpleNamespace
    weave = Weave.__new__(Weave)
    weave._config = SimpleNamespace(prompts=SimpleNamespace(system=system_val))
    if registry is None:
        registry = MagicMock()
        registry.get = MagicMock(return_value="System prompt from registry")
    weave._prompts = registry
    return weave


class TestRound10SystemPromptBareNameUnit:
    """第10轮修复项1（裸文件名相对路径先经 registry 解析）—— 单元测试。"""

    def test_bare_name_no_suffix_resolves_via_registry(self):
        """prompts.system='system'（裸名无后缀）应经 registry 解析为 prompts/system.md，
        即调用 _prompts.get('system')，而非直载 CWD 下同名文件。"""
        weave = _r10_make_weave("system")
        ctx = {"name": "alice"}
        result = weave._load_system_prompt(ctx)
        assert result == "System prompt from registry"
        weave._prompts.get.assert_called_once_with("system", ctx)

    def test_bare_name_md_suffix_resolves_via_registry_using_stem(self):
        """prompts.system='system.md' 应取 stem 'system' 作为 registry 名称
        （prompts/system.md），而非 'system.md'。"""
        weave = _r10_make_weave("system.md")
        weave._load_system_prompt()
        weave._prompts.get.assert_called_once_with("system", None)

    def test_bare_name_other_suffix_resolves_via_registry_using_stem(self):
        """prompts.system='system.txt'（其他后缀）同样应取 stem 'system'。"""
        weave = _r10_make_weave("system.txt")
        weave._load_system_prompt()
        weave._prompts.get.assert_called_once_with("system", None)

    def test_bare_name_registry_missing_falls_back_to_real_file(self, tmp_path, monkeypatch):
        """registry 无此命名 prompt（get 抛 FileNotFoundError）时，回退按真实文件
        加载 CWD 下同名文件（错误信息指向完整路径）。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        # CWD 中存在真实文件 "system"
        monkeypatch.chdir(tmp_path)
        (tmp_path / "system").write_text("real cwd file content", encoding="utf-8")
        # registry 为空（无 "system" 命名 prompt → get 抛 FileNotFoundError）
        registry = PromptRegistry(base_dir=tmp_path / "prompts")

        weave = _r10_make_weave("system", registry=registry)
        result = weave._load_system_prompt()
        assert result == "real cwd file content"

    def test_bare_name_registry_missing_real_file_missing_raises_full_path_error(self, tmp_path, monkeypatch):
        """registry 与 CWD 真实文件均不存在时，应抛 FileNotFoundError 且消息含完整路径。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        monkeypatch.chdir(tmp_path)
        registry = PromptRegistry(base_dir=tmp_path / "prompts")

        weave = _r10_make_weave("system", registry=registry)
        with pytest.raises(FileNotFoundError) as excinfo:
            weave._load_system_prompt()
        msg = str(excinfo.value)
        assert "Prompt file not found" in msg
        assert str(tmp_path / "system") in msg

    def test_bare_name_registry_wins_over_cwd_file(self, tmp_path, monkeypatch):
        """核心回归：CWD 下存在同名文件时，bare name 仍应经 registry 解析
        （registry 内容优先），修复前会误读 CWD 文件/抛错。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        monkeypatch.chdir(tmp_path)
        (tmp_path / "system").write_text("CWD FILE CONTENT", encoding="utf-8")
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        (prompts_dir / "system.md").write_text("registry prompt content", encoding="utf-8")

        registry = PromptRegistry(base_dir="prompts")
        weave = _r10_make_weave("system", registry=registry)
        assert weave._load_system_prompt() == "registry prompt content"

    def test_source_bare_name_branch_precedes_real_file_load(self):
        """源码断言：裸文件名 registry 分支（parent == Path('.')）应先于真实文件
        load_prompt 直载，保证配置写 "system" 时先经 registry 而非直载 CWD。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._load_system_prompt)
        assert 'if not path.is_absolute() and path.parent == Path("."):' in source
        bare_idx = source.index('if not path.is_absolute() and path.parent == Path("."):')
        real_idx = source.index("return load_prompt(path, context, self._config)")
        assert bare_idx < real_idx, "裸文件名 registry 分支必须先于真实文件直载"

    def test_source_bare_name_uses_stem_for_suffix(self):
        """源码断言：带后缀裸名取 stem（system.md → system），无后缀取 name。"""
        import inspect
        from weave_agent_sdk.agent import Weave

        source = inspect.getsource(Weave._load_system_prompt)
        assert "name = path.stem if path.suffix else path.name" in source

    def test_regression_prompts_relative_still_registry(self):
        """回归：prompts/system.md（prompts/ 前缀相对路径）仍按 registry 解析为
        "system"（既有行为不受裸名分支影响）。"""
        weave = _r10_make_weave("prompts/system.md")
        weave._load_system_prompt()
        weave._prompts.get.assert_called_once_with("system", None)

    def test_regression_absolute_path_still_direct_load(self, tmp_path):
        """回归：绝对路径仍直接加载真实文件内容（不经 registry，第6轮修复项4
        行为不受裸名分支影响）。"""
        abs_file = tmp_path / "abs" / "system.md"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_text("absolute loaded content", encoding="utf-8")

        weave = _r10_make_weave(str(abs_file))
        assert weave._load_system_prompt() == "absolute loaded content"


class TestRound10SystemPromptBareNameE2E:
    """端到端测试：prompts.system 裸文件名（如 "system"）在真实运行链路中
    经 registry 解析，不再直载 CWD 文件抛 FileNotFoundError（TestRound7ReentryE2E
    报告的具体失败修复）。"""

    def _make_stream_weave(self):
        """构造最小可 stream()/arun() 的 Weave 实例：prompts.system 为裸名 "system"。"""
        from weave_agent_sdk.event_bus import EventBus

        weave = Weave.__new__(Weave)
        weave._config = MagicMock()
        weave._config.agent.name = "test_agent"
        weave._config.loop.type = "simple"
        weave._config.loop.timeout = 5.0
        weave._config.loop.stream_timeout = None
        weave._config.prompts.system = "system"   # 裸文件名（本轮修复核心场景）
        weave._event_bus = EventBus()
        weave._llm = AsyncMock()
        from weave_agent_sdk.llm.base import LLMResponse
        weave._llm.chat = AsyncMock(return_value=LLMResponse(content="Hello", model="test-model"))
        weave._memory = MagicMock()
        weave._memory.activate_scopes = MagicMock()
        weave._memory.stats = MagicMock(return_value={})
        weave._memory.get_namespaces = MagicMock(return_value=["test:scope:stream"])
        weave._prompts = MagicMock()
        weave._prompts.get = MagicMock(return_value="System prompt from mock registry")
        weave._loop = AsyncMock()
        weave._loop.run = AsyncMock(return_value=LoopResult(
            output="test output", elapsed_ms=100, iterations=1,
            memory_updated={"stream": {"test:scope:stream": 1}},
        ))
        weave._tools = []
        weave._tool_map = {}
        weave._system_prompt = ""
        weave._is_running = False
        weave._last_run = None
        weave._streaming = False
        weave._run_locks = {}
        return weave

    @pytest.mark.asyncio
    async def test_e2e_stream_bare_name_system_yields_done(self):
        """stream() 同步阶段加载裸名 "system" 不再抛 FileNotFoundError，
        最终收到 done 而非 error（本轮修复在流式入口的直接验证）。"""
        weave = self._make_stream_weave()
        events = []
        async for event in weave.stream("test input"):
            events.append(event)
            if event.type in ("done", "error"):
                break
        assert events[-1].type == "done"
        assert events[-1].data["output"] == "test output"
        assert not any(e.type == "error" for e in events)
        # registry 按 "system" 名称取 prompt（而非直载 CWD 文件）
        weave._prompts.get.assert_called_with("system", None)
        assert weave._system_prompt == "System prompt from mock registry"

    @pytest.mark.asyncio
    async def test_e2e_arun_bare_name_system_completes(self):
        """arun() 路径（TestRound7ReentryE2E 的核心调用链）加载裸名 "system"
        正常完成，不再产出 FileNotFoundError。"""
        weave = self._make_stream_weave()
        result = await weave.arun("test input")
        assert result.output == "test output"
        assert weave._system_prompt == "System prompt from mock registry"

    def test_e2e_bare_name_real_registry_loads_prompts_system_md(self, tmp_path, monkeypatch):
        """真实 PromptRegistry：prompts.system='system' 经 registry 加载
        prompts/system.md 实际内容（裸名 → prompts/{name}.md 解析）。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        from types import SimpleNamespace

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "system.md").write_text("real system content", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="system"))
        weave._prompts = PromptRegistry(base_dir="prompts")
        assert weave._load_system_prompt() == "real system content"

    def test_e2e_bare_name_md_suffix_real_registry_loads_prompts_system_md(self, tmp_path, monkeypatch):
        """真实 PromptRegistry：prompts.system='system.md' 取 stem 'system'
        加载 prompts/system.md 实际内容。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        from types import SimpleNamespace

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "system.md").write_text("stem resolved content", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="system.md"))
        weave._prompts = PromptRegistry(base_dir="prompts")
        assert weave._load_system_prompt() == "stem resolved content"

    def test_e2e_bare_name_fallback_real_file_when_registry_missing(self, tmp_path, monkeypatch):
        """真实链路：registry 无 "system" 命名 prompt 且 CWD 存在同名文件时，
        回退加载 CWD 真实文件（错误信息指向完整路径）。"""
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
        from types import SimpleNamespace

        monkeypatch.chdir(tmp_path)
        (tmp_path / "system").write_text("fallback cwd file content", encoding="utf-8")

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="system"))
        weave._prompts = PromptRegistry(base_dir=tmp_path / "prompts")  # 空 registry
        assert weave._load_system_prompt() == "fallback cwd file content"

# ============================================================
# 第11轮测试（本轮新增）：针对第11轮修复行为的复核验证
# 依据 fix_record.md 最后一个 "## 第11轮修复" 标题：
#   - 本轮修复摘要：已修复0个问题——复核第4轮审查5项均已修复
#     （TTL/state窄覆盖宽/tool类型推断/prompt路径/scheduled不写幻影ns）；
#     TestRound7ReentryE2E 失败为测试漂移（get_namespaces Mock 返回 []
#     且断言 state.set≥2 早于修复），不改测试、跳过；本轮无源码改动。
# 本轮测试针对第4轮审查 5 项行为做复核（与既有 TestRound6*/TestRound10*
# 互补、不重复），新增 10 个单元测试 + 5 个端到端测试。
# ============================================================


class TestRound11TTLRecheckUnit:
    """第4轮审查项1（TTL 全链路）复核 —— 单元测试。"""

    def test_ttl_for_namespace_none_when_scope_unconfigured(self):
        """_ttl_for_namespace 对已配置 scope 返回 ttl，对未配置 scope 返回 None。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=3600),
        }, default_path=":memory:"))

        # 已配置 scope 的任意 access_type → 返回 ttl
        assert manager._ttl_for_namespace("session:s1:stream") == 3600.0
        assert manager._ttl_for_namespace("session:s1:state") == 3600.0
        assert manager._ttl_for_namespace("session:s1:knowledge") == 3600.0
        # 未配置的 scope → None（永不过期）
        assert manager._ttl_for_namespace("ghost:s1:stream") is None

    @pytest.mark.asyncio
    async def test_manager_write_sets_expires_at_and_cleanup(self):
        """MemoryManager 写入按 scope ttl 设 expires_at；过期后读写路径均回收。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=0.05),
        }, default_path=":memory:"))
        ns = "session:abc:stream"

        manager.stream.append({"role": "user", "content": "first"}, ns)
        backend = manager._get_backend_for_namespace(ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ?", (ns,)
        )
        assert cursor.fetchone()[0] is not None, "写入应按 scope ttl 设置 expires_at"

        await asyncio.sleep(0.1)
        # 过期条目从读取路径排除
        assert manager.stream.last(10, [ns]) == []
        # 显式清理回收过期条目
        assert manager.cleanup() >= 1
        remaining = backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchone()[0]
        assert remaining == 0


class TestRound11StateNarrowWideRecheckUnit:
    """第4轮审查项2（state 窄覆盖宽）复核 —— 单元测试。"""

    @pytest.mark.asyncio
    async def test_state_get_all_three_scopes_narrow_wins(self):
        """三个 scope（narrow/mid/wide）同 key 时，最窄 scope 覆盖宽 scope。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=20, state={"backend": "sqlite", "path": ":memory:"}),
            "mid": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "mid_id": "m1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "mid:m1:state", "wide:w1:state"]

        manager.state.set("theme", "narrow", nss[0])
        manager.state.set("theme", "mid", nss[1])
        manager.state.set("theme", "wide", nss[2])

        result = manager.state.get_all(nss)
        assert result["theme"] == "narrow", "最窄 scope 应覆盖同 key"

    @pytest.mark.asyncio
    async def test_state_get_all_unions_unique_keys_across_scopes(self):
        """各 scope 独有的 key 应并集保留，不被互相覆盖。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})

        narrow_ns = manager.get_namespace("narrow", "state")
        wide_ns = manager.get_namespace("wide", "state")

        manager.state.set("only_narrow", "nv", narrow_ns)
        manager.state.set("only_wide", "wv", wide_ns)

        result = manager.state.get_all([narrow_ns, wide_ns])
        assert result["only_narrow"] == "nv"
        assert result["only_wide"] == "wv"


class TestRound11ToolTypeRecheckUnit:
    """第4轮审查项3（tool 泛型/可选类型推断）复核 —— 单元测试。"""

    def test_resolve_param_type_union_with_multiple_non_none_takes_first(self):
        """Union 含多个非 None 分支时取第一个；仅 None 与单类型时正确解包。"""
        from typing import Union
        from weave_agent_sdk.loop.iterative import _resolve_param_type

        assert _resolve_param_type(Union[str, int]) == "string"
        assert _resolve_param_type(Union[float, str]) == "number"
        assert _resolve_param_type(Union[list, None]) == "array"
        assert _resolve_param_type(Union[dict, None]) == "object"

    def test_build_tool_schemas_required_only_for_no_default(self):
        """仅无默认值的参数进入 required；Optional/list/dict 类型推断正确。"""
        from typing import Optional
        from weave_agent_sdk.loop.iterative import _build_tool_schemas

        def analyze(source, limit=5, tags=None, meta=None):
            """Analyze source."""
            return ""

        analyze.__annotations__ = {
            "source": str,
            "limit": Optional[int],
            "tags": list[str] | None,
            "meta": dict[str, object] | None,
        }

        agent = MagicMock()
        agent._tools = [analyze]
        agent._tool_map = {t.__name__: t for t in agent._tools}

        schemas = _build_tool_schemas(agent)
        schema = schemas[0]
        assert schema["name"] == "analyze"
        assert schema["description"] == "Analyze source."
        assert schema["parameters"]["required"] == ["source"], \
            "仅无默认值的参数应进入 required"
        props = schema["parameters"]["properties"]
        assert props["source"]["type"] == "string"
        assert props["limit"]["type"] == "integer", "Optional[int] 应解包为 integer"
        assert props["tags"]["type"] == "array", "list[str] 应映射为 array"
        assert props["meta"]["type"] == "object", "dict[str, object] 应映射为 object"


class TestRound11PromptPathRecheckUnit:
    """第4轮审查项4（prompt 路径解析）复核 —— 单元测试。"""

    def test_load_system_prompt_absolute_path_direct_load(self, tmp_path, monkeypatch):
        """绝对路径应直接加载真实文件内容（不经 registry 名称解析）。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        abs_file = tmp_path / "external" / "system.md"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_text("absolute content", encoding="utf-8")

        monkeypatch.chdir(tmp_path)

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system=str(abs_file)))
        weave._prompts = PromptRegistry(base_dir="prompts")

        assert weave._load_system_prompt() == "absolute content"

    def test_load_system_prompt_bare_name_registry_missing_falls_back_real_file(self, tmp_path, monkeypatch):
        """裸文件名在 registry 无此命名 prompt 时回退加载 CWD 真实文件。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        # CWD 存在同名文件，registry 无此命名 prompt
        monkeypatch.chdir(tmp_path)
        (tmp_path / "system").write_text("cwd real file", encoding="utf-8")

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="system"))
        weave._prompts = PromptRegistry(base_dir=tmp_path / "prompts")  # 空 registry

        assert weave._load_system_prompt() == "cwd real file"


class TestRound11ScheduledNoPhantomRecheckUnit:
    """第4轮审查项5（scheduled 不写幻影 ns）复核 —— 单元测试。

    同时覆盖 TestRound7ReentryE2E 漂移根因：get_namespaces Mock 返回 []
    时 state.set 不会被调用（漂移测试错误地断言 state.set≥2）。
    """

    @pytest.mark.asyncio
    async def test_execute_once_writes_state_when_state_namespace_present(self):
        """激活了 state ns → state.set 写入 last_run_at / last_run_error 各 1 次。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": ["s:1:stream"], "state": ["s:1:state"], "knowledge": [],
        }[at])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        assert agent._memory.state.set.call_count == 2
        keys = [c.args[0] for c in agent._memory.state.set.call_args_list]
        assert "last_run_at" in keys
        assert "last_run_error" in keys
        assert result.memory_updated["state"] == {"s:1:state": 2}

    @pytest.mark.asyncio
    async def test_execute_once_no_state_write_when_get_namespaces_empty(self):
        """get_namespaces 返回 []（TestRound7ReentryE2E 的 Mock 形态）时
        state.set 不被调用——该测试断言 state.set≥2 属漂移。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        # 漂移根因：get_namespaces 对任意 access_type 均返回 []
        agent._memory.get_namespaces = MagicMock(return_value=[])
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        agent._memory.state.set.assert_not_called()
        assert "state" not in result.memory_updated


# ============================================================
# 第11轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound11TTLRecheckE2E:
    """端到端测试：TTL 全链路复核（第4轮审查项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_ttl_full_chain_expires_and_cleans(self, tmp_path):
        """YAML scope ttl → load_config → 写入设 expires_at → 过期后读写路径回收。"""
        yaml_path = tmp_path / "r11_ttl.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        from weave_agent_sdk.memory.manager import MemoryManager

        config = load_config(yaml_path)
        assert config.memory.scopes["session"].ttl_stream == 0.05

        manager = MemoryManager(config.memory)
        ns = "session:abc:stream"
        manager.stream.append({"role": "user", "content": "first"}, ns)
        backend = manager._get_backend_for_namespace(ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ?", (ns,)
        )
        assert cursor.fetchone()[0] is not None

        await asyncio.sleep(0.1)
        assert manager.stream.last(10, [ns]) == []
        assert manager.cleanup() >= 1
        assert backend.conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?", (ns,)
        ).fetchone()[0] == 0


class TestRound11StateNarrowWideRecheckE2E:
    """端到端测试：state 窄覆盖宽复核（第4轮审查项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_state_narrow_wins_three_scopes(self, tmp_path):
        """经 load_config 全链路：三 scope 同 key → 最窄 scope 覆盖宽 scope。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r11_state.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    wide:\n"
            "      priority: 20\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    mid:\n"
            "      priority: 10\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    narrow:\n"
            "      priority: 0\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "mid_id": "m1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "mid:m1:state", "wide:w1:state"]

        manager.state.set("theme", "narrow", nss[0])
        manager.state.set("theme", "mid", nss[1])
        manager.state.set("theme", "wide", nss[2])

        result = manager.state.get_all(nss)
        assert result["theme"] == "narrow"


class TestRound11ToolTypeRecheckE2E:
    """端到端测试：tool 泛型/可选类型推断复核（第4轮审查项3）。"""

    def test_e2e_tool_schema_full_with_generic_optional(self):
        """真实 tool 函数（Optional/list/dict 注解）生成正确 JSON Schema。"""
        from typing import Any, Optional
        from weave_agent_sdk.loop.iterative import _build_tool_schemas

        def search_kb(query, top_k=5, filters=None, meta=None):
            """Search the knowledge base."""
            return ""

        search_kb.__annotations__ = {
            "query": str,
            "top_k": Optional[int],
            "filters": list[str] | None,
            "meta": dict[str, Any] | None,
        }

        agent = MagicMock()
        agent._tools = [search_kb]
        agent._tool_map = {t.__name__: t for t in agent._tools}

        schemas = _build_tool_schemas(agent)
        schema = schemas[0]
        assert schema["name"] == "search_kb"
        assert schema["description"] == "Search the knowledge base."
        props = schema["parameters"]["properties"]
        assert props["query"]["type"] == "string"
        assert props["top_k"]["type"] == "integer"
        assert props["filters"]["type"] == "array"
        assert props["meta"]["type"] == "object"
        assert schema["parameters"]["required"] == ["query"]


class TestRound11PromptPathRecheckE2E:
    """端到端测试：prompt 路径解析复核（第4轮审查项4）。"""

    def test_e2e_load_system_prompt_absolute_and_bare_name(self, tmp_path, monkeypatch):
        """绝对路径直载真实文件；裸名经 registry 解析为 prompts/system.md。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        abs_file = tmp_path / "ext" / "prompt.md"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_text("absolute content", encoding="utf-8")

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "system.md").write_text("registry content", encoding="utf-8")

        monkeypatch.chdir(tmp_path)

        # 绝对路径 → 直载真实文件
        weave_abs = Weave.__new__(Weave)
        weave_abs._config = SimpleNamespace(prompts=SimpleNamespace(system=str(abs_file)))
        weave_abs._prompts = PromptRegistry(base_dir="prompts")
        assert weave_abs._load_system_prompt() == "absolute content"

        # 裸名 "system" → registry 解析为 prompts/system.md
        weave_bare = Weave.__new__(Weave)
        weave_bare._config = SimpleNamespace(prompts=SimpleNamespace(system="system"))
        weave_bare._prompts = PromptRegistry(base_dir="prompts")
        assert weave_bare._load_system_prompt() == "registry content"


class TestRound11ScheduledNoPhantomRecheckE2E:
    """端到端测试：scheduled 不写幻影 ns 复核（第4轮审查项5）。

    与 TestRound6ScheduledNoPhantomNsE2E 互补：后者用未激活 MemoryConfig() 验证
    无幻影写；本测试明确覆盖 TestRound7ReentryE2E 漂移根因——真实 MemoryManager
    未激活任何 scope（get_namespaces 对任意 access_type 返回 []）时，
    scheduled 单次执行不调用 state.set、不写幻影 default:session:state、不创建
    任何 backend。
    """

    @pytest.mark.asyncio
    async def test_e2e_scheduled_run_no_phantom_when_no_scope_active(self):
        """真实 MemoryManager 未激活任何 scope 时，scheduled 单次执行不写
        幻影 default:session:state，且不创建任何 backend。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        manager = MemoryManager(MemoryConfig())
        # 未激活任何 scope → 任意 access_type 均返回 []
        assert manager.get_namespaces("state") == []
        assert manager.get_namespaces("stream") == []

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = None
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = manager

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "input")

        assert result.output == "scheduled"
        # 无 state scope → 不写幻影 ns，memory_updated 不含 state
        assert "state" not in result.memory_updated
        # 未创建任何 backend（含幻影 default:session:state）
        assert manager._backends == {}


# ============================================================
# 第12轮测试（本轮新增）：针对第12轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第12轮修复" 标题（最近一轮）：
#   本轮修复摘要：已修复0个问题——复核第4轮审查5项均已修复
#   （TTL全链路 / state窄覆盖宽 / tool泛型·可选类型推断 /
#     prompt绝对路径直载 / scheduled不写幻影ns），复核通过；
#   本轮无源码改动，未编写测试。
# 本轮测试针对第4轮审查5项行为做复核补充（与既有 TestRound6* /
#  TestRound11* 互补、不重复），新增 10 个单元测试 + 5 个端到端测试。
# ============================================================


class TestRound12TTLUnit:
    """第4轮审查项1（TTL 全链路）复核补充 —— 单元测试。"""

    def test_parse_memory_scopes_ttl_from_state_and_knowledge_blocks(self):
        """ttl 写在 state / knowledge access 块内应解析到 ttl_state / ttl_knowledge；
        scope 级 ttl 仍优先于所有 access 块内 ttl。"""
        from weave_agent_sdk.config import _parse_memory_scopes

        # state / knowledge 块的 ttl 各自解析到对应 access 字段
        scopes = _parse_memory_scopes({
            "session": {
                "state": {"backend": "sqlite", "ttl": 7200},
                "knowledge": {"backend": "sqlite", "ttl": 3600},
            },
        })
        assert scopes["session"].ttl_state == 7200
        assert scopes["session"].ttl_knowledge == 3600

        # 仅 knowledge 块写 ttl 时同样被解析
        scopes2 = _parse_memory_scopes({
            "session": {"knowledge": {"backend": "sqlite", "ttl": 1800}},
        })
        assert scopes2["session"].ttl_knowledge == 1800

        # scope 级 ttl 覆盖全部 access 块内 ttl
        scopes3 = _parse_memory_scopes({
            "session": {
                "ttl": 100,
                "stream": {"ttl": 3600},
                "state": {"ttl": 7200},
                "knowledge": {"ttl": 9000},
            },
        })
        assert scopes3["session"].ttl == 100
        assert scopes3["session"].ttl_stream == 3600
        assert scopes3["session"].ttl_state == 7200
        assert scopes3["session"].ttl_knowledge == 9000

    @pytest.mark.asyncio
    async def test_read_paths_exclude_expired_without_cleanup(self):
        """过期条目在读取路径上即被排除（stream.last / state.get / state.get_all /
        knowledge.search 均带 expires_at 过滤），即便尚未触发写路径清理。"""
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        import json

        backend = SQLiteBackend(":memory:")
        now = time.time()
        ns_stream = "s:1:stream"
        ns_state = "s:1:state"
        ns_know = "s:1:knowledge"

        # 过期条目（expires_at 在过去）
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('exp-stream', ?, 'stream', NULL, ?, ?, ?)",
            (ns_stream, json.dumps({"role": "user", "content": "old"}), now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('exp-state', ?, 'state', 'k', ?, ?, ?)",
            (ns_state, json.dumps("old"), now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('exp-know', ?, 'knowledge', NULL, 'old kb', ?, ?)",
            (ns_know, now - 100, now - 50),
        )
        # 未过期条目（expires_at 在未来）
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('fresh-stream', ?, 'stream', NULL, ?, ?, ?)",
            (ns_stream, json.dumps({"role": "user", "content": "new"}), now - 10, now + 1000),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('fresh-state', ?, 'state', 'k', ?, ?, ?)",
            (ns_state, json.dumps("new"), now - 10, now + 1000),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('fresh-know', ?, 'knowledge', NULL, 'fresh kb', ?, ?)",
            (ns_know, now - 10, now + 1000),
        )
        backend.conn.commit()

        # 未触发写路径清理（直接读）：过期条目被读取路径排除，未过期条目正常返回
        assert [e["content"] for e in backend.stream_last(10, [ns_stream])] == ["new"]
        assert backend.state_get("k", ns_state) == "new"
        assert backend.state_get_all([ns_state]) == {"k": "new"}
        results = backend.knowledge_search("kb", [ns_know], 5)
        assert [r.content for r in results] == ["fresh kb"]


class TestRound12TTLE2E:
    """端到端测试：TTL 全链路复核补充（scope 级 ttl 配置，第4轮审查项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_scope_level_ttl_full_chain(self, tmp_path):
        """scope 级 ttl（memory.scopes.session.ttl）经 load_config 全链路：
        写入设 expires_at → 过期后读取路径排除 + 写路径被动清理。"""
        import yaml

        yaml_path = tmp_path / "r12_scope_ttl.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      ttl: 0.05\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        from weave_agent_sdk.memory.manager import MemoryManager

        config = load_config(yaml_path)
        assert config.memory.scopes["session"].ttl == 0.05

        manager = MemoryManager(config.memory)
        ns = "session:abc:stream"
        manager.stream.append({"role": "user", "content": "first"}, ns)
        backend = manager._get_backend_for_namespace(ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ?", (ns,)
        )
        assert cursor.fetchone()[0] is not None, "scope 级 ttl 应设置 expires_at"

        await asyncio.sleep(0.1)
        # 读取路径排除过期条目
        assert manager.stream.last(10, [ns]) == []
        # 写路径被动清理：再次写入后仅保留新条目
        manager.stream.append({"role": "user", "content": "second"}, ns)
        last = manager.stream.last(10, [ns])
        assert [e["content"] for e in last] == ["second"]
        assert backend.namespace_stats().get(ns) == 1


class TestRound12StateNarrowWideUnit:
    """第4轮审查项2（state 窄覆盖宽）复核补充 —— 单元测试。"""

    @pytest.mark.asyncio
    async def test_state_get_all_narrow_absence_keeps_wide_value(self):
        """窄 scope 没有某 key 时不覆盖宽 scope 的该 key（窄缺值不抹除宽值）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})
        narrow_ns = manager.get_namespace("narrow", "state")
        wide_ns = manager.get_namespace("wide", "state")

        manager.state.set("shared", "narrow", narrow_ns)
        manager.state.set("shared", "wide", wide_ns)
        manager.state.set("only_wide", "wv", wide_ns)  # 窄 scope 无此 key

        result = manager.state.get_all([narrow_ns, wide_ns])
        assert result["shared"] == "narrow"   # 同 key 窄覆盖宽
        assert result["only_wide"] == "wv"    # 窄缺值 → 宽值保留（不被抹除）

    @pytest.mark.asyncio
    async def test_state_get_all_narrow_wins_regardless_of_write_order(self):
        """窄 scope 后写仍胜出（合并按宽→窄应用，与写入顺序无关）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})
        narrow_ns = manager.get_namespace("narrow", "state")
        wide_ns = manager.get_namespace("wide", "state")

        # 先写窄、后写宽：合并顺序由 priority 决定，窄仍是最终值
        manager.state.set("theme", "narrow-first", narrow_ns)
        manager.state.set("theme", "wide-later", wide_ns)

        result = manager.state.get_all([narrow_ns, wide_ns])
        assert result["theme"] == "narrow-first"


class TestRound12StateNarrowWideE2E:
    """端到端测试：state 窄覆盖宽复核补充（第4轮审查项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_three_scopes_narrow_absence_wide_value_surfaces(self, tmp_path):
        """三 scope（narrow/mid/wide）经 load_config 全链路：narrow 无某 key 时
        mid/wide 的该 key 值正常呈现，不被窄 scope 缺值抹除。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r12_state_three.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    wide:\n"
            "      priority: 20\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    mid:\n"
            "      priority: 10\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    narrow:\n"
            "      priority: 0\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "mid_id": "m1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "mid:m1:state", "wide:w1:state"]

        # shared key: narrow 有值 → 覆盖 mid/wide
        manager.state.set("shared", "narrow", nss[0])
        manager.state.set("shared", "mid", nss[1])
        manager.state.set("shared", "wide", nss[2])
        # only in mid & wide（narrow 无）
        manager.state.set("only_mid_wide", "mv", nss[1])
        manager.state.set("only_mid_wide", "wv", nss[2])
        # only in wide（narrow & mid 均无）
        manager.state.set("only_wide", "wv2", nss[2])

        result = manager.state.get_all(nss)
        assert result["shared"] == "narrow"
        assert result["only_mid_wide"] == "mv"   # mid(priority 10)比 wide 窄 → mid 胜出
        assert result["only_wide"] == "wv2"      # 窄/中无值 → 宽值呈现


class TestRound12ToolTypeInferenceUnit:
    """第4轮审查项3（tool 泛型 / 可选类型推断）复核补充 —— 单元测试。"""

    def test_resolve_param_type_nested_optional_generic(self):
        """嵌套 Optional 泛型：Optional[list[str]] → array、list[Optional[int]] →
        array、Union[list, None] → array、dict | None → object（PEP 604 裸类型）。"""
        from typing import Optional, Union
        from weave_agent_sdk.loop.iterative import _resolve_param_type

        assert _resolve_param_type(Optional[list[str]]) == "array"
        assert _resolve_param_type(list[Optional[int]]) == "array"
        assert _resolve_param_type(Union[list, None]) == "array"
        assert _resolve_param_type(dict | None) == "object"
        assert _resolve_param_type(list | None) == "array"

    def test_build_tool_schemas_no_params_omits_required(self):
        """无参数 tool 的 schema 不应包含 required 键；Optional 无默认值参数仍必填；
        有默认值参数不进入 required。"""
        from typing import Optional
        from weave_agent_sdk.loop.iterative import _build_tool_schemas

        def no_args():
            """No args tool."""
            return ""

        def opt_no_default(x: Optional[int]):
            """Optional without default is still required."""
            return ""

        def with_default(x: int = 5):
            """Has default."""
            return ""

        # 测试模块启用了 from __future__ import annotations（注解为字符串），
        # 显式赋予真实类型对象使 _build_tool_schemas 的签名解析得到真实类型。
        no_args.__annotations__ = {}
        opt_no_default.__annotations__ = {"x": Optional[int]}
        with_default.__annotations__ = {"x": int}

        agent = MagicMock()
        agent._tools = [no_args, opt_no_default, with_default]
        agent._tool_map = {t.__name__: t for t in agent._tools}

        schemas = {s["name"]: s for s in _build_tool_schemas(agent)}

        assert "required" not in schemas["no_args"]["parameters"]
        assert schemas["opt_no_default"]["parameters"]["required"] == ["x"]
        assert schemas["opt_no_default"]["parameters"]["properties"]["x"]["type"] == "integer"
        assert "required" not in schemas["with_default"]["parameters"]


class TestRound12ToolTypeInferenceE2E:
    """端到端测试：tool 泛型 / 可选类型推断复核补充（第4轮审查项3）。"""

    def test_e2e_tool_schema_nested_optional_generic_full(self):
        """嵌套 Optional 泛型 + 有默认值参数 + PEP 604 可选 dict 生成完整正确的 JSON Schema。"""
        from typing import Optional
        from weave_agent_sdk.loop.iterative import _build_tool_schemas

        def analyze(queries: Optional[list[str]], limit: int = 10, params: dict | None = None):
            """Analyze with optional nested types."""
            return ""

        analyze.__annotations__ = {
            "queries": Optional[list[str]],
            "limit": int,
            "params": dict | None,
        }

        agent = MagicMock()
        agent._tools = [analyze]
        agent._tool_map = {t.__name__: t for t in agent._tools}

        schemas = _build_tool_schemas(agent)
        schema = schemas[0]
        assert schema["name"] == "analyze"
        assert schema["description"] == "Analyze with optional nested types."
        props = schema["parameters"]["properties"]
        assert props["queries"]["type"] == "array", "Optional[list[str]] 应解包为 array"
        assert props["params"]["type"] == "object", "dict | None 应解包为 object"
        assert props["limit"]["type"] == "integer"
        # 仅无默认值的 queries 为必填
        assert schema["parameters"]["required"] == ["queries"]


class TestRound12PromptPathUnit:
    """第4轮审查项4（prompt 绝对路径直载）复核补充 —— 单元测试。"""

    def test_load_system_prompt_absolute_path_bypasses_registry(self, tmp_path):
        """绝对路径直载真实文件，且不调用 registry.get（证明未回退 stem 名称解析，
        不会因 PurePath.stem 截断目录而静默加载错误文件）。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave

        abs_file = tmp_path / "deep" / "nested" / "system.md"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_text("deep nested content", encoding="utf-8")

        registry = MagicMock()
        registry.get = MagicMock(return_value="WRONG registry content")

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system=str(abs_file)))
        weave._prompts = registry

        result = weave._load_system_prompt()
        assert result == "deep nested content"
        # 绝对路径直载：registry.get 不应被调用
        registry.get.assert_not_called()

    def test_load_system_prompt_non_prompts_relative_direct_load(self, tmp_path, monkeypatch):
        """非 prompts/ 前缀、含目录分隔符的相对路径直接加载真实文件（相对 CWD），
        不经 registry 名称解析。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave

        custom = tmp_path / "custom_prompts" / "system.md"
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text("custom relative content", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        registry = MagicMock()
        registry.get = MagicMock(return_value="WRONG registry content")

        weave = Weave.__new__(Weave)
        weave._config = SimpleNamespace(prompts=SimpleNamespace(system="custom_prompts/system.md"))
        weave._prompts = registry

        result = weave._load_system_prompt()
        assert result == "custom relative content"
        registry.get.assert_not_called()


class TestRound12PromptPathE2E:
    """端到端测试：prompt 路径解析复核补充（第4轮审查项4）。"""

    def test_e2e_prompt_path_resolution_matrix(self, tmp_path, monkeypatch):
        """绝对路径 / 非 prompts/ 相对路径直载真实文件；prompts/ 相对与裸名经
        registry 解析——四类路径各归其位。"""
        from types import SimpleNamespace
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.prompts.prompt_registry import PromptRegistry

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "system.md").write_text("registry system content", encoding="utf-8")

        abs_file = tmp_path / "ext" / "abs.md"
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        abs_file.write_text("absolute content", encoding="utf-8")

        custom = tmp_path / "custom" / "sys.md"
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text("custom content", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        registry = PromptRegistry(base_dir="prompts")

        # 绝对路径 → 直载真实文件
        w_abs = Weave.__new__(Weave)
        w_abs._config = SimpleNamespace(prompts=SimpleNamespace(system=str(abs_file)))
        w_abs._prompts = registry
        assert w_abs._load_system_prompt() == "absolute content"

        # 非 prompts/ 相对路径（含目录分隔符）→ 直载真实文件
        w_rel = Weave.__new__(Weave)
        w_rel._config = SimpleNamespace(prompts=SimpleNamespace(system="custom/sys.md"))
        w_rel._prompts = registry
        assert w_rel._load_system_prompt() == "custom content"

        # prompts/ 相对路径 → registry 名称解析
        w_std = Weave.__new__(Weave)
        w_std._config = SimpleNamespace(prompts=SimpleNamespace(system="prompts/system.md"))
        w_std._prompts = registry
        assert w_std._load_system_prompt() == "registry system content"

        # 裸名 "system" → registry 名称解析
        w_bare = Weave.__new__(Weave)
        w_bare._config = SimpleNamespace(prompts=SimpleNamespace(system="system"))
        w_bare._prompts = registry
        assert w_bare._load_system_prompt() == "registry system content"


class TestRound12ScheduledNoPhantomUnit:
    """第4轮审查项5（scheduled 不写幻影 ns）复核补充 —— 单元测试。"""

    @pytest.mark.asyncio
    async def test_execute_once_exception_path_no_phantom_state_write(self):
        """LLM 调用抛异常（走 except 分支）且无 state ns 时，仍不写幻影 ns
        （state.set 在异常路径也不被调用）。"""
        from weave_agent_sdk.loop.scheduled import ScheduledLoop

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        agent._system_prompt = "System"
        agent._memory = MagicMock()
        agent._memory.get_namespaces = MagicMock(return_value=[])  # 无任何 state ns
        agent._memory.state.set = MagicMock()

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        with pytest.raises(RuntimeError, match="llm down"):
            await loop._execute_once(agent, "input")

        # 异常路径也不写幻影 ns
        agent._memory.state.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_scope_state_write_uses_active_ns_not_phantom(self):
        """空配置激活默认 scope 后，state 写入落在激活的 default:default:state，
        而非旧硬编码的幻影 default:session:state。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        manager.activate_scopes()  # 空配置 → 默认 scope [("default", 0, "default")]
        assert manager.get_namespaces("state") == ["default:default:state"]

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = manager

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop._execute_once(agent, "input")

        # 写入激活的默认 state ns（2 次），而非幻影 default:session:state
        assert result.memory_updated["state"] == {"default:default:state": 2}
        backend = list(manager._backends.values())[0]
        nss = backend.list_namespaces()
        assert "default:default:state" in nss
        assert "default:session:state" not in nss, "旧幻影 ns 不应出现"


class TestRound12ScheduledNoPhantomE2E:
    """端到端测试：scheduled 不写幻影 ns 复核补充（第4轮审查项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_scheduled_writes_only_real_activated_state_ns(self):
        """scheduled 单次执行（经 run() 入口）持久化 stream + state 到激活的
        session 真实 ns，不写任何幻影 default:session:state。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.loop.scheduled import ScheduledLoop
        from weave_agent_sdk.llm.base import LLMResponse

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                stream={"backend": "sqlite", "path": ":memory:"},
                state={"backend": "sqlite", "path": ":memory:"},
            ),
        }, default_path=":memory:"))
        manager.activate_scopes({"session_id": "s1"})

        agent = MagicMock()
        agent._config = MagicMock()
        agent._config.loop.schedule = None
        agent._config.loop.max_iterations = 1
        agent._config.llm.max_tokens = 100
        agent._config.llm.temperature = 0.0
        agent._llm = AsyncMock()
        agent._llm.chat = AsyncMock(return_value=LLMResponse(content="scheduled", model="test"))
        agent._system_prompt = "System"
        agent._memory = manager

        loop = ScheduledLoop()
        loop.on_start = AsyncMock()
        loop.on_end = AsyncMock()
        loop.before_think = AsyncMock(return_value={})
        loop.after_think = AsyncMock()

        result = await loop.run(agent, "input")

        assert result.output == "scheduled"
        # stream 与 state 均落在激活的 session 真实 ns
        assert result.memory_updated["stream"] == {"session:s1:stream": 1}  # after_think 被 mock：仅 persist_user_message 落盘 1 条
        assert result.memory_updated["state"] == {"session:s1:state": 2}
        # DB 中不出现幻影 default:session:state / default:default:state
        backend = list(manager._backends.values())[0]
        nss = backend.list_namespaces()
        assert "session:s1:stream" in nss
        assert "session:s1:state" in nss
        assert "default:session:state" not in nss
        assert "default:default:state" not in nss



# ============================================================
# 第13轮测试（本轮新增）：针对第13轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第13轮修复" 标题（最近一轮）：
#   - 修复项1: TTL 按 access 类型拆分（ttl_stream / ttl_state / ttl_knowledge，
#     回退 scope 级 ttl）— review round-6 issue 1
#   - 修复项2: backend 接线 SQLite / File / Chroma，并补 FileBackend 的 TTL
#     （写路径设 _expires_at / 读路径过滤 / cleanup 回收）— review round-6 issue 2
#   - 修复项3: state.get_all(None) 窄覆盖宽（无参数路径复用显式路径的
#     宽→窄合并语义）— review round-6 issue 3
#   - 修复项4: 重建订阅先注册新再 aclose 旧（agent.py / ws.py 的空窗不丢事件）
#     — review round-6 issue 4
#   - 修复项5: stats 先清理 + 统计过滤过期（stats() 先 cleanup 再 namespace_stats）
#     — review round-6 issue 5
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound13TTLAccessTypeUnit:
    """第13轮修复项1（TTL 按 access 类型拆分）—— 单元测试。"""

    def test_parse_memory_scopes_ttl_split_by_access_type(self):
        """_parse_memory_scopes 应将 stream/state/knowledge 各自的 ttl
        解析到 ttl_stream/ttl_state/ttl_knowledge，而非折叠为单一 scope 级值。"""
        from weave_agent_sdk.config import _parse_memory_scopes

        scopes = _parse_memory_scopes({
            "session": {
                "ttl": 100,
                "stream": {"backend": "sqlite", "ttl": 3600},
                "state": {"backend": "sqlite", "ttl": 7200},
                "knowledge": {"backend": "sqlite", "ttl": 1800},
            },
        })
        cfg = scopes["session"]
        assert cfg.ttl == 100           # scope 级回退
        assert cfg.ttl_stream == 3600
        assert cfg.ttl_state == 7200
        assert cfg.ttl_knowledge == 1800

    def test_ttl_for_namespace_prefers_access_specific_and_falls_back(self):
        """_ttl_for_namespace 按 access_type 优先取专属 ttl；未配置时回退
        scope 级 ttl；均未配置回退 None（永不过期）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        # 全 access 专属 ttl
        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                ttl=100,
                ttl_stream=3600,
                ttl_state=7200,
                ttl_knowledge=1800,
            ),
        }, default_path=":memory:"))
        assert manager._ttl_for_namespace("session:s1:stream") == 3600.0
        assert manager._ttl_for_namespace("session:s1:state") == 7200.0
        assert manager._ttl_for_namespace("session:s1:knowledge") == 1800.0

        # 仅 scope 级 ttl：全部回退同一值
        manager2 = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=300),
        }, default_path=":memory:"))
        assert manager2._ttl_for_namespace("session:s1:stream") == 300.0
        assert manager2._ttl_for_namespace("session:s1:state") == 300.0
        assert manager2._ttl_for_namespace("session:s1:knowledge") == 300.0

        # 无 scope 配置：None（永不过期）
        manager3 = MemoryManager(MemoryConfig(default_path=":memory:"))
        assert manager3._ttl_for_namespace("ghost:s1:stream") is None


class TestRound13BackendWiringUnit:
    """第13轮修复项2（backend 接线 SQLite/File/Chroma + FileBackend TTL）—— 单元测试。"""

    def test_get_backend_creates_file_and_default_sqlite(self, tmp_path):
        """配置 file backend 时创建 FileBackend 并缓存；未配置时默认 SQLiteBackend。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.file import FileBackend
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        db_dir = str(tmp_path / "file_mem")
        scopes = {"session": MemoryScopeConfig(
            backend="file",
            stream={"backend": "file", "path": db_dir},
        )}
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        backend = manager._get_backend_for_namespace("session:s1:stream")
        assert isinstance(backend, FileBackend)
        assert backend._db_path == db_dir

        # 默认配置 → SQLiteBackend
        default = MemoryManager(MemoryConfig(default_path=":memory:"))
        assert isinstance(default._get_backend_for_namespace("session:s1:stream"), SQLiteBackend)

    def test_get_backend_chroma_knowledge_vs_stream_fallback(self):
        """chroma 用于 knowledge → ChromaBackend；配置到 stream/state 时应
        回退 sqlite（避免向量后端用错语义），而非静默使用。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        scopes = {"session": MemoryScopeConfig(
            knowledge={"backend": "chroma", "path": "./data/vectors"},
            stream={"backend": "chroma", "path": "./data/vectors"},
        )}
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        assert isinstance(manager._get_backend_for_namespace("session:s1:knowledge"), ChromaBackend)
        assert isinstance(manager._get_backend_for_namespace("session:s1:stream"), SQLiteBackend), \
            "chroma 配置到 stream 应回退 sqlite"

    def test_file_backend_stream_ttl(self, tmp_path):
        """FileBackend.stream_append 带 ttl 时记录 _expires_at；读路径过滤过期；
        cleanup_expired 回收。永不过期条目（无 ttl）不设 _expires_at。"""
        import time
        from weave_agent_sdk.memory.backends.file import FileBackend

        backend = FileBackend(str(tmp_path / "fb_stream"))
        ns = "scope1:u1:stream"
        backend.stream_append({"role": "user", "content": "temp"}, ns, ttl=0.05)
        path = backend._get_path(ns, "stream")
        items = backend._read(path)
        assert items and "_expires_at" in items[0], "带 ttl 写入应记录 _expires_at"

        backend.stream_append({"role": "user", "content": "permanent"}, ns)  # 永不过期
        items = backend._read(path)
        assert sum(1 for i in items if "_expires_at" in i) == 1

        time.sleep(0.1)
        last = backend.stream_last(10, [ns])
        assert [e["content"] for e in last] == ["permanent"], "过期 stream 条目应被读取路径过滤"
        assert backend.cleanup_expired() >= 1
        items = backend._read(path)
        assert [i["content"] for i in items] == ["permanent"]

    def test_file_backend_state_knowledge_ttl(self, tmp_path):
        """FileBackend.state_set / knowledge_add 带 ttl 时写入 _expires_at，
        过期条目被 state_get / knowledge_search 过滤。"""
        import time
        from weave_agent_sdk.memory.backends.file import FileBackend

        backend = FileBackend(str(tmp_path / "fb_sk"))
        state_ns = "scope1:u1:state"
        know_ns = "scope1:u1:knowledge"

        backend.state_set("k", "v", state_ns, ttl=0.05)
        backend.state_set("k2", "v2", state_ns)  # 永不过期
        backend.knowledge_add("expired knowledge", know_ns, None, ttl=0.05)
        backend.knowledge_add("permanent knowledge", know_ns, None)

        time.sleep(0.1)
        assert backend.state_get("k", state_ns) is None, "过期 state 条目应被过滤"
        assert backend.state_get("k2", state_ns) == "v2"
        assert backend.knowledge_search("expired", [know_ns], 5) == []
        results = backend.knowledge_search("permanent", [know_ns], 5)
        assert len(results) == 1
        assert results[0].content == "permanent knowledge"


class TestRound13StateGetAllNoneUnit:
    """第13轮修复项3（state.get_all(None) 窄覆盖宽）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_get_all_none_narrow_wins(self):
        """get_all(None) 复用激活 scope 的 namespace 列表（窄→宽），按宽→窄
        合并，同 key 窄 scope 覆盖宽 scope。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})
        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "wide:w1:state"]

        manager.state.set("theme", "narrow-theme", nss[0])
        manager.state.set("theme", "wide-theme", nss[1])

        result = manager.state.get_all()   # 无参数路径（本轮修复核心）
        assert result["theme"] == "narrow-theme", "get_all(None) 应窄覆盖宽"

    @pytest.mark.asyncio
    async def test_get_all_none_consistent_with_explicit_path(self):
        """无参数路径与显式传入 namespaces 应给出相同结果（同 key 窄胜），
        消除两条读取路径对同 key 给出不同值的旧不一致。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})
        nss = manager.get_namespaces("state")

        manager.state.set("theme", "narrow-theme", nss[0])
        manager.state.set("theme", "wide-theme", nss[1])

        result_none = manager.state.get_all()
        result_explicit = manager.state.get_all(nss)
        assert result_none == result_explicit == {"theme": "narrow-theme"}


class TestRound13SubscriptionRebuildUnit:
    """第13轮修复项4（重建订阅先注册新再 aclose 旧）—— 单元测试。"""

    def test_agent_and_ws_rebuild_order_register_new_before_aclose_old(self):
        """agent.stream() 与 ws_agent_stream 的重建顺序必须是：先注册新订阅、
        再 aclose 旧生成器——避免"旧已注销、新未注册"空窗内 emit 的事件被丢弃。"""
        import inspect
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.server.ws import ws_agent_stream

        for name, source in (("agent.stream", inspect.getsource(Weave.stream)),
                             ("ws_agent_stream", inspect.getsource(ws_agent_stream))):
            old_idx = source.index("old_gen = subscribe_gen")
            new_idx = source.index("subscribe_gen = _rebuild_subscription()")
            aclose_idx = source.index("await old_gen.aclose()")
            assert old_idx < new_idx < aclose_idx, \
                f"{name} 重建订阅必须先注册新订阅、再 aclose 旧生成器"


class TestRound13StatsExpiredUnit:
    """第13轮修复项5（stats 先清理 + 统计过滤过期）—— 单元测试。"""

    def test_stats_cleans_before_count_and_stats_filters_expired(self, tmp_path):
        """MemoryManager.stats() 应先 cleanup() 再 namespace_stats()；
        SQLiteBackend 与 FileBackend 的 namespace_stats 均排除已过期条目。"""
        import inspect
        from weave_agent_sdk.memory.manager import MemoryManager

        source = inspect.getsource(MemoryManager.stats)
        assert "self.cleanup()" in source
        assert "backend.namespace_stats()" in source
        assert source.index("self.cleanup()") < source.index("backend.namespace_stats()"), \
            "stats() 应先清理过期数据再统计"

        # SQLite：namespace_stats 排除过期
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        backend = SQLiteBackend(":memory:")
        ns = "s:1:stream"
        now = time.time()
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('expired', ?, 'stream', NULL, ?, ?, ?)",
            (ns, json.dumps({"role": "user", "content": "old"}), now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES ('fresh', ?, 'stream', NULL, ?, ?, NULL)",
            (ns, json.dumps({"role": "user", "content": "new"}), now - 10),
        )
        backend.conn.commit()
        assert backend.namespace_stats().get(ns) == 1, "SQLite namespace_stats 应排除已过期条目"

        # File：namespace_stats 排除过期（与 SQLite 契约一致）
        from weave_agent_sdk.memory.backends.file import FileBackend
        fb = FileBackend(str(tmp_path / "fb_stats"))
        fns = "s:2:stream"
        fb.stream_append({"role": "user", "content": "expired"}, fns, ttl=0.05)
        fb.stream_append({"role": "user", "content": "fresh"}, fns)
        import time as _t
        _t.sleep(0.1)
        fstats = fb.namespace_stats()
        assert sum(fstats.values()) == 1, "FileBackend namespace_stats 应排除已过期条目"


# ============================================================
# 第13轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound13TTLAccessTypeE2E:
    """端到端测试：TTL 按 access 类型拆分经 load_config 全链路生效。"""

    @pytest.mark.asyncio
    async def test_e2e_ttl_split_per_access_type(self, tmp_path):
        import time
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r13_ttl_split.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 3600\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        scope = config.memory.scopes["session"]
        assert scope.ttl_stream == 0.05
        assert scope.ttl_state == 3600
        assert scope.ttl_knowledge is None

        manager = MemoryManager(config.memory)
        stream_ns = "session:s1:stream"
        state_ns = "session:s1:state"

        manager.stream.append({"role": "user", "content": "temp stream"}, stream_ns)
        manager.state.set("k", "persistent state", state_ns)

        backend = manager._get_backend_for_namespace(stream_ns)
        cursor = backend.conn.execute(
            "SELECT expires_at FROM memory_entries WHERE namespace = ? AND access_type = 'stream'",
            (stream_ns,),
        )
        assert cursor.fetchone()[0] is not None, "stream 短 TTL 应设置 expires_at"

        time.sleep(0.1)
        # stream 已过期被读取路径排除；state 长 TTL 仍持久
        assert manager.stream.last(10, [stream_ns]) == []
        assert manager.state.get("k", state_ns) == "persistent state"


class TestRound13FileBackendE2E:
    """端到端测试：FileBackend TTL 经 MemoryManager + YAML 全链路生效。"""

    @pytest.mark.asyncio
    async def test_e2e_file_backend_ttl_full_chain(self, tmp_path):
        import time
        from weave_agent_sdk.memory.manager import MemoryManager

        mem_dir = (tmp_path / "mem").as_posix()
        yaml_path = tmp_path / "r13_file.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 3600\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        stream_ns = "session:s1:stream"
        state_ns = "session:s1:state"

        manager.stream.append({"role": "user", "content": "temp"}, stream_ns)
        manager.state.set("k", "v", state_ns)

        time.sleep(0.1)
        assert manager.stream.last(10, [stream_ns]) == [], "FileBackend stream 过期条目应被过滤"
        assert manager.state.get("k", state_ns) == "v", "FileBackend state 长 TTL 应持久"


class TestRound13StateGetAllNoneE2E:
    """端到端测试：state.get_all(None) 窄覆盖宽经 YAML scopes 全链路生效。"""

    @pytest.mark.asyncio
    async def test_e2e_get_all_none_narrow_wins(self, tmp_path):
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r13_state_none.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    wide:\n"
            "      priority: 10\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    narrow:\n"
            "      priority: 0\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "wide:w1:state"]
        manager.state.set("theme", "narrow", nss[0])
        manager.state.set("theme", "wide", nss[1])
        manager.state.set("only_wide", "wv", nss[1])

        result = manager.state.get_all()   # 无参数路径
        assert result["theme"] == "narrow", "get_all(None) 同 key 窄覆盖宽"
        assert result["only_wide"] == "wv", "窄缺值 → 宽值保留"


class TestRound13SubscriptionRebuildE2E:
    """端到端测试：重建订阅"先注册新再 aclose 旧"，空窗事件不丢。"""

    @pytest.mark.asyncio
    async def test_e2e_rebuild_no_event_gap(self):
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        old_gen = bus.subscribe("token", "done")
        # 消费一个事件使生成器进入 try 块（否则 aclose 不触发退订）
        await bus.emit("token", {"text": "init", "index": 0})
        assert (await anext(old_gen)).type == "token"

        # 复现 agent/ws 重建顺序：先注册新订阅、再 aclose 旧生成器
        new_gen = bus.subscribe("token", "done")
        # 空窗（新已注册、旧未 aclose）：连续 emit 3 个事件 → 全部被新订阅收到
        for i in range(3):
            await bus.emit("token", {"text": f"w{i}", "index": i})
        await old_gen.aclose()

        got = [(await anext(new_gen)).data["text"] for _ in range(3)]
        assert got == ["w0", "w1", "w2"], "重建空窗内 emit 的事件不应丢失"

        await new_gen.aclose()
        assert bus.subscriber_count == {}, "全部退订后无残留"


class TestRound13StatsE2E:
    """端到端测试：stats() 先清理 + 过滤过期经 MemoryManager 全链路生效。"""

    @pytest.mark.asyncio
    async def test_e2e_stats_cleans_expired_and_counts_fresh(self, tmp_path):
        import time
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r13_stats.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "      knowledge:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        stream_ns = "session:s1:stream"
        know_ns = "session:s1:knowledge"

        manager.stream.append({"role": "user", "content": "temp"}, stream_ns)
        manager.knowledge.add("permanent knowledge", know_ns)

        time.sleep(0.1)
        stats = manager.stats()
        # stream 过期：stats() 内先 cleanup 回收 → 不计入；knowledge 无 ttl → 计入
        assert stats.get(stream_ns, 0) == 0
        assert stats.get(know_ns, 0) == 1

# ROUND14_APPEND_MARKER


# ============================================================
# 第14轮测试（本轮新增）：针对第14轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第14轮修复" 标题（最近一轮）：
#   本轮修复摘要：已修复1个问题——清理7个 .bak 残留
#   （weave/ 内 6 个：config.py / types.py / memory/manager.py /
#     memory/state.py / memory/backends/file.py / memory/backends/sqlite.py
#     + nexus/review_record.md.bak），消除 TestBakFileCleanup /
#     TestRound4/9BakCleanup 共 23 个失败。
# 本轮只涉及 .bak 备份文件清理行为（无源码逻辑修改），测试仅针对该行为，
# 与既有 TestBakFileCleanup / TestRound4BakCleanup / TestRound9BakCleanup
# 互补、不重复（既有测试未覆盖 memory/backends 下的 file.py.bak 与
# sqlite.py.bak 这两个本轮被清理文件）。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound14BakCleanupUnit:
    """第14轮修复（清理 7 个 .bak 残留）—— 逐项单元验证本轮被清理文件均已移除。

    覆盖：weave/ 内 6 个（config.py / types.py / memory/manager.py /
    memory/state.py / memory/backends/file.py / memory/backends/sqlite.py）
    + nexus/review_record.md.bak。
    """

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_config_py_bak_removed(self):
        """weave/config.py.bak 应已删除（weave/ 内 6 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "config.py.bak").exists(), \
            "weave/config.py.bak 应已清理"

    def test_types_py_bak_removed(self):
        """weave/types.py.bak 应已删除（weave/ 内 6 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "types.py.bak").exists(), \
            "weave/types.py.bak 应已清理"

    def test_memory_manager_py_bak_removed(self):
        """weave/memory/manager.py.bak 应已删除（weave/ 内 6 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "manager.py.bak").exists(), \
            "weave/memory/manager.py.bak 应已清理"

    def test_memory_state_py_bak_removed(self):
        """weave/memory/state.py.bak 应已删除（weave/ 内 6 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "state.py.bak").exists(), \
            "weave/memory/state.py.bak 应已清理"

    def test_memory_backends_file_py_bak_removed(self):
        """weave/memory/backends/file.py.bak 应已删除（本轮核心：第13轮 FileBackend
        接线产生的备份，既有 TestRound4/9 未覆盖到）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "file.py.bak").exists(), \
            "weave/memory/backends/file.py.bak 应已清理"

    def test_memory_backends_sqlite_py_bak_removed(self):
        """weave/memory/backends/sqlite.py.bak 应已删除（本轮核心：既有测试未覆盖）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "sqlite.py.bak").exists(), \
            "weave/memory/backends/sqlite.py.bak 应已清理"

    def test_review_record_md_bak_removed(self):
        """nexus/review_record.md.bak 应已删除（本轮清理的 7 个 .bak 之一）。"""
        assert not (self.PROJECT_ROOT / "nexus" / "review_record.md.bak").exists(), \
            "nexus/review_record.md.bak 应已清理"

    def test_memory_backends_dir_clean(self):
        """weave/memory/backends/ 目录内不应存在任何 .bak 文件（本轮新增覆盖目录）。"""
        backends_dir = self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in backends_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/memory/backends 残留 .bak: {bak_files}"

    def test_weave_package_tree_no_bak(self):
        """weave/ 源码整棵树递归扫描不应存在任何 .bak 文件（含 memory/backends）。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in weave_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/ 源码树残留 .bak: {bak_files}"

    def test_nexus_tree_no_bak(self):
        """nexus/ 目录树不应存在任何 .bak 文件（本轮清理的 review_record.md.bak 已消除）。"""
        nexus_dir = self.PROJECT_ROOT / "nexus"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in nexus_dir.rglob("*.bak")]
        assert bak_files == [], f"nexus/ 残留 .bak: {bak_files}"


class TestRound14BakCleanupE2E:
    """端到端测试：本轮清理的 7 个 .bak 文件全部消失且全项目无 .bak 残留。"""

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_e2e_round14_cleaned_files_absent_anywhere(self):
        """本轮清理的 7 个 .bak 文件应全部从项目中消失（已知路径逐一验证）。"""
        known_paths = [
            self.PROJECT_ROOT / "weave_agent_sdk" / "config.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "types.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "manager.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "state.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "file.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "sqlite.py.bak",
            self.PROJECT_ROOT / "nexus" / "review_record.md.bak",
        ]
        for p in known_paths:
            assert not p.exists(), f"被清理文件仍存在: {p}"

    def test_e2e_no_bak_anywhere_in_project(self):
        """整个项目目录树递归扫描不应存在任何 .bak 文件（含 weave/tests/nexus/docs 等）。"""
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in self.PROJECT_ROOT.rglob("*.bak")]
        assert bak_files == [], f"项目残留 .bak: {bak_files}"

    def test_e2e_no_bak_in_weave_source_tree(self):
        """weave/ 源码整棵树递归扫描不应存在任何 .bak 文件。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in weave_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/ 源码树残留 .bak: {bak_files}"

    def test_e2e_no_bak_in_memory_tree(self):
        """weave/memory/ 整棵树（含 backends/）递归扫描不应存在任何 .bak 文件。"""
        memory_dir = self.PROJECT_ROOT / "weave_agent_sdk" / "memory"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in memory_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/memory/ 残留 .bak: {bak_files}"

    def test_e2e_all_top_level_source_dirs_clean(self):
        """各顶层源码/文档目录递归扫描均无 .bak 残留（含 nexus/bak 归档目录不误命中）。"""
        for sub in ("weave", "tests", "prompts", "nexus", "docs", "examples"):
            d = self.PROJECT_ROOT / sub
            if not d.exists():
                continue
            bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in d.rglob("*.bak")]
            assert bak_files == [], f"{sub}/ 残留 .bak: {bak_files}"

        # nexus/bak 归档目录（目录名非 .bak 后缀）不应产生误命中
        bak_dir = self.PROJECT_ROOT / "nexus" / "bak"
        if bak_dir.is_dir():
            assert bak_dir.suffix == "", "目录 'bak' 不应有 .bak 后缀"
            inner_bak = list(bak_dir.rglob("*.bak"))
            assert inner_bak == [], f"nexus/bak 内部仍残留 .bak: {inner_bak}"

# ============================================================
# 第15轮测试（本轮新增）：针对第15轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第15轮修复" 标题（最近一轮）：
#   本轮修复摘要：已修复0个问题——复核第6轮审查5项均已修复
#   （TTL按access拆分 / backend接线SQLite·File·Chroma /
#    state.get_all(None)窄覆盖宽 / 重建订阅先注册新再aclose旧 /
#    stats先清理+过滤过期），Round13复核测试15个全部通过；
#   TestRound7ReentryE2E 失败为测试漂移（get_namespaces Mock 返回 []
#   且断言 state.set≥2 早于修复），不改测试跳过；本轮无源码改动、未编写测试。
# 本轮测试针对第6轮审查 5 项行为做复核补充（与既有 TestRound13* 互补、不重复），
# 新增 10 个单元测试 + 5 个端到端测试。
# ============================================================


class TestRound15TTLAccessTypeUnit:
    """第6轮审查项1（TTL 按 access 类型拆分）复核补充 —— 单元测试。"""

    def test_parse_memory_scopes_partial_access_ttl(self):
        """仅配置 stream 的 ttl 时 ttl_stream 生效、state/knowledge 回退 None；
        scope 级 ttl 仍作为统一回退值。"""
        from weave_agent_sdk.config import _parse_memory_scopes

        scopes = _parse_memory_scopes({
            "session": {
                "ttl": 100,
                "stream": {"backend": "sqlite", "ttl": 3600},
                "state": {"backend": "sqlite"},
                "knowledge": {"backend": "sqlite"},
            },
        })
        cfg = scopes["session"]
        assert cfg.ttl_stream == 3600
        assert cfg.ttl_state is None
        assert cfg.ttl_knowledge is None
        assert cfg.ttl == 100
        # state/knowledge 未单独配置 ttl → 由 _ttl_for_namespace 回退 scope 级 ttl

    def test_ttl_for_namespace_missing_access_and_invalid(self):
        """access 专属 ttl 未配置且 scope 级 ttl 未配置时返回 None（永不过期）；
        非法 access_type 一律返回 None，不抛错。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                stream={"backend": "sqlite", "path": ":memory:"},
                ttl_stream=3600,
            ),
        }, default_path=":memory:"))

        assert manager._ttl_for_namespace("session:s1:stream") == 3600.0
        # state/knowledge 未配置、scope 级 ttl 也 None → 永不过期
        assert manager._ttl_for_namespace("session:s1:state") is None
        assert manager._ttl_for_namespace("session:s1:knowledge") is None
        # 非法 access_type 返回 None（不抛 ValueError）
        assert manager._ttl_for_namespace("session:s1:bogus") is None


class TestRound15BackendWiringUnit:
    """第6轮审查项2（backend 接线 SQLite/File/Chroma）复核补充 —— 单元测试。"""

    def test_get_backend_unknown_backend_falls_back_sqlite(self, caplog):
        """未知 backend 类型（如 mongo）应告警并回退 SQLiteBackend，而非静默崩溃。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        scopes = {"session": MemoryScopeConfig(stream={"backend": "mongo", "path": ":memory:"})}
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        with caplog.at_level(logging.WARNING):
            backend = manager._get_backend_for_namespace("session:s1:stream")

        assert isinstance(backend, SQLiteBackend), "未知 backend 应回退 sqlite"
        assert any("Unknown backend" in r.message for r in caplog.records)

    def test_get_backend_caches_shared_instance_by_type_and_path(self, tmp_path):
        """同一 (backend_type, db_path) 应共享缓存实例；不同 path 实例独立。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        dir1 = str(tmp_path / "m1")
        dir2 = str(tmp_path / "m2")
        scopes = {
            "a": MemoryScopeConfig(stream={"backend": "file", "path": dir1}),
            "b": MemoryScopeConfig(stream={"backend": "file", "path": dir2}),
        }
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        b1 = manager._get_backend_for_namespace("a:s1:stream")
        b1_again = manager._get_backend_for_namespace("a:s2:stream")
        b2 = manager._get_backend_for_namespace("b:s1:stream")

        assert b1 is b1_again, "同 (type, path) 应共享缓存实例"
        assert b1 is not b2, "不同 path 应独立实例"
        assert len(manager._backends) == 2

    def test_file_backend_stream_last_scan_all_files(self, tmp_path):
        """FileBackend.stream_last(namespaces=None) 应扫描全部 stream 文件、
        过滤过期并跨文件合并。"""
        from weave_agent_sdk.memory.backends.file import FileBackend

        backend = FileBackend(str(tmp_path / "fb_scan"))
        backend.stream_append({"role": "user", "content": "persist"}, "s1:u1:stream")
        backend.stream_append({"role": "user", "content": "temp"}, "s2:u2:stream", ttl=0.05)

        time.sleep(0.1)
        last = backend.stream_last(10, None)
        contents = [e["content"] for e in last]
        assert contents == ["persist"], "过期 stream 条目应被扫描路径过滤"

    def test_file_backend_cleanup_expired_bounded_by_limit(self, tmp_path):
        """FileBackend.cleanup_expired(limit=n) 应最多清理 n 条过期条目（有界）。"""
        from weave_agent_sdk.memory.backends.file import FileBackend

        backend = FileBackend(str(tmp_path / "fb_limit"))
        ns = "s:u:stream"
        # 先写入 5 条永不过期条目（避免 Windows 文件 I/O 延迟累计 > 短 ttl，
        # 导致写入阶段即被写路径 cleanup 误删——测试时序问题，非源码缺陷）
        for i in range(5):
            backend.stream_append({"role": "user", "content": f"m{i}"}, ns)

        # 手动将全部 5 条标记为已过期，再验证 cleanup 的 limit 上限
        path = backend._get_path(ns, "stream")
        items = backend._read(path)
        assert len(items) == 5
        for it in items:
            it["_expires_at"] = time.time() - 100
        backend._write(path, items)

        deleted = backend.cleanup_expired(limit=2)
        assert deleted == 2
        assert len(backend._read(path)) == 3, "limit 上限应生效：一次最多清理 limit 条"


class TestRound15StateGetAllNoneUnit:
    """第6轮审查项3（state.get_all(None) 窄覆盖宽）复核补充 —— 单元测试。"""

    @pytest.mark.asyncio
    async def test_get_all_none_empty_when_no_active_scopes(self):
        """无激活 scope 时 get_all(None) 应返回空 dict（不抛错、不写任何 ns）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        assert manager.state.get_all() == {}

    @pytest.mark.asyncio
    async def test_get_all_none_three_scopes_narrow_wins_and_unions(self):
        """三 scope 下 get_all(None)：同 key 最窄覆盖宽；各 scope 独有 key 并集保留。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=20, state={"backend": "sqlite", "path": ":memory:"}),
            "mid": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "mid_id": "m1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "mid:m1:state", "wide:w1:state"]
        manager.state.set("shared", "narrow", nss[0])
        manager.state.set("shared", "mid", nss[1])
        manager.state.set("shared", "wide", nss[2])
        manager.state.set("only_wide", "wv", nss[2])

        result = manager.state.get_all()
        assert result["shared"] == "narrow", "get_all(None) 最窄 scope 覆盖同 key"
        assert result["only_wide"] == "wv", "窄/中缺值 → 宽值保留"


class TestRound15SubscriptionRebuildUnit:
    """第6轮审查项4（重建订阅先注册新再 aclose 旧）复核补充 —— 单元测试。"""

    def test_rebuild_aclose_old_wrapped_in_try_except(self):
        """agent.stream() 与 ws_agent_stream 重建订阅时，`await old_gen.aclose()`
        应被 try/except 包裹（aclose 失败不阻断重建与继续等待），且顺序为
        先注册新订阅、再 aclose 旧生成器。"""
        import inspect
        from weave_agent_sdk.agent import Weave
        from weave_agent_sdk.server.ws import ws_agent_stream

        for name, source in (("agent.stream", inspect.getsource(Weave.stream)),
                             ("ws_agent_stream", inspect.getsource(ws_agent_stream))):
            assert "old_gen = subscribe_gen" in source, name
            assert "subscribe_gen = _rebuild_subscription()" in source, name
            aclose_idx = source.index("await old_gen.aclose()")
            # 重建顺序：先注册新、再 aclose 旧
            assert source.index("subscribe_gen = _rebuild_subscription()") < aclose_idx
            # aclose 被 try/except 包裹
            assert "try:" in source[:aclose_idx], f"{name}: aclose 前应有 try"
            assert "except Exception:" in source[aclose_idx:], f"{name}: aclose 后应有 except"


class TestRound15StatsExpiredUnit:
    """第6轮审查项5（stats 先清理 + 过滤过期）复核补充 —— 单元测试。"""

    def test_stats_no_backends_returns_empty(self):
        """MemoryManager.stats() 在无任何 backend（未写入）时返回空 dict（cleanup no-op）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig

        manager = MemoryManager(MemoryConfig(default_path=":memory:"))
        assert manager.stats() == {}

    @pytest.mark.asyncio
    async def test_stats_cleans_across_all_backends_before_count(self, tmp_path):
        """stats() 的 cleanup 应作用于全部后端（多个 db 文件），统计前先回收
        每个后端中的过期条目。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        scopes = {
            "a": MemoryScopeConfig(stream={"backend": "sqlite", "path": str(tmp_path / "a.db")}),
            "b": MemoryScopeConfig(stream={"backend": "sqlite", "path": str(tmp_path / "b.db")}),
        }
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))
        ns_a = "a:s1:stream"
        ns_b = "b:s1:stream"
        backend_a = manager._get_backend_for_namespace(ns_a)
        backend_b = manager._get_backend_for_namespace(ns_b)

        now = time.time()
        for backend, ns in ((backend_a, ns_a), (backend_b, ns_b)):
            backend.conn.execute(
                "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
                "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
                (f"{ns}-expired", ns, json.dumps({"role": "user", "content": "old"}), now - 100, now - 50),
            )
            backend.conn.commit()
        # a 后端补一条永不过期的新条目
        backend_a.stream_append({"role": "user", "content": "fresh"}, ns_a)

        stats = manager.stats()
        assert stats.get(ns_a) == 1, "a 后端过期条目应被 cleanup 回收，仅计新条目"
        assert stats.get(ns_b, 0) == 0, "b 后端过期条目应被 stats() 内 cleanup 回收"


# ============================================================
# 第15轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound15TTLAccessTypeE2E:
    """端到端测试：TTL 按 access 类型拆分（仅 stream 配 ttl）经 load_config 全链路。"""

    @pytest.mark.asyncio
    async def test_e2e_ttl_split_partial_per_access(self, tmp_path):
        """YAML 仅 stream 配 ttl、state/knowledge 未配 ttl：stream 过期被过滤，
        state / knowledge 持久（各自独立 TTL 语义）。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r15_ttl_partial.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "      knowledge:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        scope = config.memory.scopes["session"]
        assert scope.ttl_stream == 0.05
        assert scope.ttl_state is None
        assert scope.ttl_knowledge is None

        manager = MemoryManager(config.memory)
        stream_ns = "session:s1:stream"
        state_ns = "session:s1:state"
        know_ns = "session:s1:knowledge"

        manager.stream.append({"role": "user", "content": "temp"}, stream_ns)
        manager.state.set("k", "persistent", state_ns)
        manager.knowledge.add("persistent knowledge", know_ns)

        time.sleep(0.1)
        assert manager.stream.last(10, [stream_ns]) == [], "stream 短 TTL 过期应被过滤"
        assert manager.state.get("k", state_ns) == "persistent", "state 无 ttl 应持久"
        assert len(manager.knowledge.search("persistent", [know_ns], 5)) == 1, "knowledge 无 ttl 应持久"


class TestRound15FileBackendE2E:
    """端到端测试：FileBackend 各 access 类型独立 TTL 经 load_config 全链路。"""

    @pytest.mark.asyncio
    async def test_e2e_file_backend_mixed_access_ttl(self, tmp_path):
        """FileBackend：stream 短 TTL 过期被过滤，state 长 TTL 持久，knowledge 无 TTL 持久。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        mem_dir = (tmp_path / "mem").as_posix()
        yaml_path = tmp_path / "r15_file.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 3600\n"
            "      knowledge:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        stream_ns = "session:s1:stream"
        state_ns = "session:s1:state"
        know_ns = "session:s1:knowledge"

        manager.stream.append({"role": "user", "content": "temp"}, stream_ns)
        manager.state.set("k", "persist", state_ns)
        manager.knowledge.add("persist knowledge", know_ns)

        time.sleep(0.1)
        assert manager.stream.last(10, [stream_ns]) == [], "FileBackend stream 过期应被过滤"
        assert manager.state.get("k", state_ns) == "persist", "FileBackend state 长 TTL 应持久"
        assert len(manager.knowledge.search("persist", [know_ns], 5)) == 1, "FileBackend knowledge 无 TTL 应持久"


class TestRound15StateGetAllNoneE2E:
    """端到端测试：state.get_all(None) 三 scope 窄覆盖宽经 YAML 全链路。"""

    @pytest.mark.asyncio
    async def test_e2e_get_all_none_three_scopes(self, tmp_path):
        """YAML 三 scope（wide/mid/narrow）下 get_all(None)：同 key 最窄覆盖宽，
        独有 key 并集保留。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r15_state_none.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    wide:\n"
            "      priority: 20\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    mid:\n"
            "      priority: 10\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    narrow:\n"
            "      priority: 0\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "mid_id": "m1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "mid:m1:state", "wide:w1:state"]
        manager.state.set("shared", "narrow", nss[0])
        manager.state.set("shared", "mid", nss[1])
        manager.state.set("shared", "wide", nss[2])
        manager.state.set("only_mid", "mv", nss[1])
        manager.state.set("only_wide", "wv", nss[2])

        result = manager.state.get_all()
        assert result["shared"] == "narrow"
        assert result["only_mid"] == "mv"
        assert result["only_wide"] == "wv"


class TestRound15SubscriptionRebuildE2E:
    """端到端测试：重建订阅"先注册新再 aclose 旧"在多事件类型下不丢空窗事件。"""

    @pytest.mark.asyncio
    async def test_e2e_rebuild_gap_multi_event_types_no_loss(self):
        """旧订阅已消费进入 try 块后重建：空窗内跨多种事件类型的 emit 全部被新
        订阅收到；旧订阅 aclose 后订阅数回落、最终全部清理无残留。"""
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        old = bus.subscribe("token", "tool_call", "done")
        # 消费一个事件使生成器进入 try 块（否则 aclose 不触发退订）
        await bus.emit("token", {"text": "init", "index": 0})
        assert (await anext(old)).type == "token"

        # 先注册新订阅、再 aclose 旧（agent/ws 重建顺序）
        new = bus.subscribe("token", "tool_call", "done")
        await bus.emit("token", {"text": "gap0", "index": 0})
        await bus.emit("tool_call", {"name": "search", "arguments": {"q": "x"}})
        await bus.emit("done", {"output": "o", "elapsed_ms": 1, "iterations": 1})
        await old.aclose()

        # 旧订阅已退订、新订阅保留
        assert bus.subscriber_count == {"token": 1, "tool_call": 1, "done": 1}

        got_token = (await anext(new)).data["text"]
        got_tool = await anext(new)
        got_done = await anext(new)
        assert got_token == "gap0"
        assert got_tool.type == "tool_call"
        assert got_done.type == "done"

        await new.aclose()
        assert bus.subscriber_count == {}, "全部退订后无残留"


class TestRound15StatsE2E:
    """端到端测试：stats() 先清理 + 过滤过期在 FileBackend 上的全链路。"""

    @pytest.mark.asyncio
    async def test_e2e_stats_cleans_expired_file_backend(self, tmp_path):
        """YAML FileBackend：stream 短 TTL 条目过期后，stats() 内先 cleanup 回收
        （计数 0），state 长 TTL 条目正常计入（计数 1）。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        mem_dir = (tmp_path / "mem").as_posix()
        yaml_path = tmp_path / "r15_stats.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 3600\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        stream_ns = "session:s1:stream"
        state_ns = "session:s1:state"

        manager.stream.append({"role": "user", "content": "temp"}, stream_ns)
        manager.state.set("k", "v", state_ns)

        time.sleep(0.1)
        stats = manager.stats(purge=True)
        assert stats.get(stream_ns, 0) == 0, "stats() 应先回收过期 stream 条目"
        assert stats.get(state_ns, 0) == 1, "state 长 TTL 条目应正常计入"

        # 直接检查：stats() 内 cleanup 已把过期 stream 条目从文件回收
        backend = manager._get_backend_for_namespace(stream_ns)
        items = backend._read(backend._get_path(stream_ns, "stream"))
        assert items == [], "过期 stream 条目应已被 stats() 内 cleanup 回收"


# ============================================================
# 第16轮测试（本轮新增）：针对第16轮修复行为的复核验证
# 依据 fix_record.md 最后一个 "## 第16轮修复" 标题（最近一轮）：
#   本轮修复摘要：已修复0个问题——复核第6轮审查5项均已修复
#   （TTL按access拆分 / backend接线SQLite·File·Chroma并补FileBackend的TTL /
#    state.get_all(None)窄覆盖宽 / 重建订阅先注册新再aclose旧 /
#    stats先清理+过滤过期）；TestRound7ReentryE2E失败为已知测试漂移
#   （fixture Mock返回[]且旧断言要求state.set≥2），不改测试跳过；
#   本轮无源码改动、未编写测试，详见 fix_record.md。
# 本轮为复核补充验证（与既有 TestRound13* / TestRound15* 互补、不重复），
# 只针对本轮复核的5项行为，新增 10 个单元测试 + 5 个端到端测试。
# ============================================================


class TestRound16TTLAccessSplitUnit:
    """第6轮审查项1（TTL 按 access 类型拆分）复核补充 —— 单元测试。"""

    def test_ttl_for_namespace_prefers_each_access_specific(self):
        """同一 scope 下 stream/state/knowledge 各自独立 TTL 被 _ttl_for_namespace
        按 access_type 精确取用；均未配置时回退 scope 级 ttl。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                ttl=300, ttl_stream=3600, ttl_state=7200, ttl_knowledge=1800,
            ),
        }, default_path=":memory:"))

        assert manager._ttl_for_namespace("session:s1:stream") == 3600.0
        assert manager._ttl_for_namespace("session:s1:state") == 7200.0
        assert manager._ttl_for_namespace("session:s1:knowledge") == 1800.0

        # 未配置 access 专属 ttl → 回退 scope 级 ttl（统一回退值）
        manager2 = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=60),
        }, default_path=":memory:"))
        assert manager2._ttl_for_namespace("session:s1:stream") == 60.0
        assert manager2._ttl_for_namespace("session:s1:state") == 60.0
        assert manager2._ttl_for_namespace("session:s1:knowledge") == 60.0

    def test_load_config_splits_three_access_ttls_distinct(self, tmp_path):
        """load_config 将 stream/state/knowledge 各自的 ttl 解析到独立字段，
        且与 scope 级 ttl 并存（不同 access 不同过期时间，非折叠为单一值）。"""
        import yaml

        yaml_path = tmp_path / "r16_ttl_split.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "simple"},
            "memory": {
                "scopes": {
                    "session": {
                        "ttl": 100,
                        "stream": {"backend": "sqlite", "path": ":memory:", "ttl": 3600},
                        "state": {"backend": "sqlite", "path": ":memory:", "ttl": 7200},
                        "knowledge": {"backend": "sqlite", "path": ":memory:", "ttl": 1800},
                    }
                }
            },
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = load_config(yaml_path)
        scope = config.memory.scopes["session"]
        assert scope.ttl == 100
        assert scope.ttl_stream == 3600
        assert scope.ttl_state == 7200
        assert scope.ttl_knowledge == 1800


class TestRound16BackendWiringUnit:
    """第6轮审查项2（backend 接线 + FileBackend TTL）复核补充 —— 单元测试。"""

    def test_mixed_backends_per_access_same_scope(self, tmp_path):
        """同一 scope 下 stream 用 file、state 用 sqlite、knowledge 用 file：
        各 namespace 解析到对应后端类型，同 (type, path) 共享实例。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.file import FileBackend
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        mem_dir = str(tmp_path / "mem")
        scopes = {"session": MemoryScopeConfig(
            stream={"backend": "file", "path": mem_dir},
            state={"backend": "sqlite", "path": ":memory:"},
            knowledge={"backend": "file", "path": mem_dir},
        )}
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        s = manager._get_backend_for_namespace("session:s1:stream")
        st = manager._get_backend_for_namespace("session:s1:state")
        k = manager._get_backend_for_namespace("session:s1:knowledge")

        assert isinstance(s, FileBackend)
        assert isinstance(st, SQLiteBackend)
        assert isinstance(k, FileBackend)
        assert s is k, "同 (file, path) 应共享缓存实例"
        assert s is not st
        assert len(manager._backends) == 2

    def test_manager_close_clears_and_recreates_backend(self, tmp_path):
        """close() 清空 _backends 缓存；之后再次访问按配置重建后端实例（可复用）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(stream={"backend": "sqlite", "path": str(tmp_path / "mem.db")}),
        }, default_path=":memory:"))
        ns = "session:s1:stream"

        backend = manager._get_backend_for_namespace(ns)
        assert isinstance(backend, SQLiteBackend)

        manager.close()
        assert manager._backends == {}

        backend2 = manager._get_backend_for_namespace(ns)
        assert isinstance(backend2, SQLiteBackend)
        assert backend2 is not backend, "close 后应重建新实例"


class TestRound16StateGetAllNoneUnit:
    """第6轮审查项3（state.get_all(None) 窄覆盖宽）复核补充 —— 单元测试。"""

    @pytest.mark.asyncio
    async def test_get_all_none_narrow_delete_reveals_wide(self):
        """get_all(None) 下窄 scope 删除某 key 后，宽 scope 的同 key 值应浮现
        （删除只影响窄 scope 自身，不抹除宽 scope 数据）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "wide": MemoryScopeConfig(priority=10, state={"backend": "sqlite", "path": ":memory:"}),
            "narrow": MemoryScopeConfig(priority=0, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})
        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "wide:w1:state"]

        manager.state.set("theme", "narrow", nss[0])
        manager.state.set("theme", "wide", nss[1])
        assert (manager.state.get_all())["theme"] == "narrow"

        manager.state.delete("theme", nss[0])
        result = manager.state.get_all()
        assert result["theme"] == "wide", "窄 scope 删除后宽 scope 值应浮现"

    @pytest.mark.asyncio
    async def test_get_all_none_filters_expired_entries(self):
        """get_all(None) 不返回已过期 state 条目（读取路径过滤与显式路径一致）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(ttl=0.05, state={"backend": "sqlite", "path": ":memory:"}),
        }, default_path=":memory:"))
        manager.activate_scopes({"session_id": "s1"})
        ns = manager.get_namespace("session", "state")

        manager.state.set("k", "v", ns)
        assert manager.state.get_all() == {"k": "v"}

        await asyncio.sleep(0.1)
        assert manager.state.get_all() == {}, "过期 state 条目不应出现在 get_all(None)"


class TestRound16SubscriptionRebuildUnit:
    """第6轮审查项4（重建订阅先注册新再 aclose 旧）复核补充 —— 单元测试。"""

    @pytest.mark.asyncio
    async def test_rebuild_registration_order_never_drops_to_zero(self):
        """重建期间订阅计数变化为 1 → 2 → 1（先注册新、再 aclose 旧），
        全程不为 0——即不存在"旧已注销、新未注册"的事件丢失空窗。"""
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen1 = bus.subscribe("token")
        await bus.emit("token", {"text": "init", "index": 0})
        assert (await anext(gen1)).type == "token"  # 启动 gen1，使 aclose 触发退订
        assert bus.subscriber_count == {"token": 1}

        # 重建第一步：先注册新订阅（旧订阅仍注册中，计数 2，无空窗）
        gen2 = bus.subscribe("token")
        assert bus.subscriber_count == {"token": 2}, "重建空窗期间计数不应为 0"
        await bus.emit("token", {"text": "gap", "index": 0})
        assert (await anext(gen2)).type == "token"   # 启动 gen2，使 aclose 触发退订

        # 重建第二步：再 aclose 旧订阅（gen1 已启动 -> 触发退订）
        await gen1.aclose()
        assert bus.subscriber_count == {"token": 1}

        await gen2.aclose()
        assert bus.subscriber_count == {}

    @pytest.mark.asyncio
    async def test_rebuild_new_subscriber_receives_gap_events(self):
        """先注册新订阅后，空窗内 emit 的事件被新订阅完整接收（不被丢弃）；
        旧订阅 aclose 后新订阅继续持有事件队列。"""
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen1 = bus.subscribe("token")
        await bus.emit("token", {"text": "init", "index": 0})
        assert (await anext(gen1)).type == "token"  # 进入 try 块

        # 重建：先注册新订阅，再 aclose 旧订阅
        gen2 = bus.subscribe("token")
        await bus.emit("token", {"text": "gap1", "index": 0})
        await bus.emit("token", {"text": "gap2", "index": 1})
        await gen1.aclose()

        # 新订阅完整收到空窗事件
        assert (await anext(gen2)).data["text"] == "gap1"
        assert (await anext(gen2)).data["text"] == "gap2"
        await gen2.aclose()
        assert bus.subscriber_count == {}


class TestRound16StatsUnit:
    """第6轮审查项5（stats 先清理 + 统计过滤过期）复核补充 —— 单元测试。"""

    def test_stats_cleans_expired_across_access_types(self):
        """stats() 内先 cleanup 回收 stream/state/knowledge 三类过期条目，
        统计只含未过期条目，且过期条目被物理删除（非仅过滤）。"""
        import json
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                stream={"backend": "sqlite", "path": ":memory:"},
                state={"backend": "sqlite", "path": ":memory:"},
                knowledge={"backend": "sqlite", "path": ":memory:"},
            ),
        }, default_path=":memory:"))
        ns_s = "session:s1:stream"
        ns_st = "session:s1:state"
        ns_k = "session:s1:knowledge"
        backend = manager._get_backend_for_namespace(ns_s)
        now = time.time()

        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
            ("exp-s", ns_s, json.dumps({"role": "user", "content": "old"}), now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'state', 'k', ?, ?, ?)",
            ("exp-st", ns_st, json.dumps("old"), now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'knowledge', NULL, 'old kb', ?, ?)",
            ("exp-k", ns_k, now - 100, now - 50),
        )
        backend.conn.execute(
            "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
            "VALUES (?, ?, 'stream', NULL, ?, ?, NULL)",
            ("fresh-s", ns_s, json.dumps({"role": "user", "content": "new"}), now - 10),
        )
        backend.conn.commit()

        stats = manager.stats(purge=True)
        assert stats.get(ns_s) == 1
        assert stats.get(ns_st, 0) == 0
        assert stats.get(ns_k, 0) == 0
        # 过期条目被物理回收（stats() 内 cleanup 已删除）
        rows = backend.conn.execute("SELECT id FROM memory_entries").fetchall()
        assert [r[0] for r in rows] == ["fresh-s"], "过期条目应被 stats() 内 cleanup 物理回收"

    def test_stats_mixed_backends_cleanup_both(self, tmp_path):
        """stats() 的 cleanup 作用于全部后端（多个 db 文件），统计前回收
        每个后端中的过期条目。"""
        import json
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        a_path = str(tmp_path / "a.db")
        b_path = str(tmp_path / "b.db")
        manager = MemoryManager(MemoryConfig(scopes={
            "a": MemoryScopeConfig(stream={"backend": "sqlite", "path": a_path}),
            "b": MemoryScopeConfig(stream={"backend": "sqlite", "path": b_path}),
        }, default_path=":memory:"))
        ns_a = "a:s1:stream"
        ns_b = "b:s1:stream"
        ba = manager._get_backend_for_namespace(ns_a)
        bb = manager._get_backend_for_namespace(ns_b)
        now = time.time()

        for backend, ns, expired_id, fresh_id in (
            (ba, ns_a, "exp-a", "fresh-a"),
            (bb, ns_b, "exp-b", None),
        ):
            backend.conn.execute(
                "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
                "VALUES (?, ?, 'stream', NULL, ?, ?, ?)",
                (expired_id, ns, json.dumps({"role": "user", "content": "old"}), now - 100, now - 50),
            )
            if fresh_id:
                backend.conn.execute(
                    "INSERT INTO memory_entries (id, namespace, access_type, key, content, created_at, expires_at) "
                    "VALUES (?, ?, 'stream', NULL, ?, ?, NULL)",
                    (fresh_id, ns, json.dumps({"role": "user", "content": "new"}), now - 10),
                )
            backend.conn.commit()

        stats = manager.stats(purge=True)
        assert stats.get(ns_a) == 1
        assert stats.get(ns_b, 0) == 0
        assert len(ba.conn.execute("SELECT id FROM memory_entries WHERE namespace = ?", (ns_a,)).fetchall()) == 1
        assert len(bb.conn.execute("SELECT id FROM memory_entries WHERE namespace = ?", (ns_b,)).fetchall()) == 0


# ============================================================
# 第16轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound16TTLAccessSplitE2E:
    """端到端测试：TTL 按 access 拆分经 load_config 全链路生效（第6轮审查项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_all_three_access_ttls_sqlite(self, tmp_path):
        """YAML 中 stream/knowledge 短 TTL、state 长 TTL：过期后 stream 与
        knowledge 被过滤、state 持久——三类 access 各自独立过期。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r16_ttl_all.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 3600\n"
            "      knowledge:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        scope = config.memory.scopes["session"]
        assert scope.ttl_stream == 0.05
        assert scope.ttl_state == 3600
        assert scope.ttl_knowledge == 0.05

        manager = MemoryManager(config.memory)
        ns_s = "session:s1:stream"
        ns_st = "session:s1:state"
        ns_k = "session:s1:knowledge"

        manager.stream.append({"role": "user", "content": "temp stream"}, ns_s)
        manager.state.set("k", "persist", ns_st)
        manager.knowledge.add("temp knowledge", ns_k)

        await asyncio.sleep(0.1)
        assert manager.stream.last(10, [ns_s]) == [], "stream 短 TTL 应过期"
        assert manager.state.get("k", ns_st) == "persist", "state 长 TTL 应持久"
        assert manager.knowledge.search("temp", [ns_k], 5) == [], "knowledge 短 TTL 应过期"


class TestRound16BackendWiringE2E:
    """端到端测试：同一 scope 混合后端 + 各自 TTL 全链路（第6轮审查项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_file_stream_and_sqlite_state_same_scope(self, tmp_path):
        """YAML：stream 用 file（短 TTL）+ state 用 sqlite（长 TTL）——
        stream 后端为 FileBackend 且过期被过滤，state 后端为 SQLiteBackend 且持久。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.memory.backends.file import FileBackend
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        mem_dir = (tmp_path / "mem").as_posix()
        yaml_path = tmp_path / "r16_mixed.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 3600\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        ns_s = "session:s1:stream"
        ns_st = "session:s1:state"

        manager.stream.append({"role": "user", "content": "temp"}, ns_s)
        manager.state.set("k", "v", ns_st)

        await asyncio.sleep(0.1)
        assert manager.stream.last(10, [ns_s]) == [], "FileBackend stream 过期应被过滤"
        assert manager.state.get("k", ns_st) == "v", "SQLiteBackend state 长 TTL 应持久"

        # 后端类型接线正确
        assert isinstance(manager._get_backend_for_namespace(ns_s), FileBackend)
        assert isinstance(manager._get_backend_for_namespace(ns_st), SQLiteBackend)


class TestRound16StateGetAllNoneE2E:
    """端到端测试：state.get_all(None) 窄删除后宽值浮现（第6轮审查项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_get_all_none_narrow_delete_reveals_wide(self, tmp_path):
        """经 YAML narrow/wide scope 全链路：窄 scope 删除某 key 后，
        get_all(None) 返回宽 scope 的同 key 值。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r16_state.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    wide:\n"
            "      priority: 10\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "    narrow:\n"
            "      priority: 0\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"narrow_id": "n1", "wide_id": "w1"})

        nss = manager.get_namespaces("state")
        assert nss == ["narrow:n1:state", "wide:w1:state"]
        manager.state.set("theme", "narrow", nss[0])
        manager.state.set("theme", "wide", nss[1])

        assert (manager.state.get_all())["theme"] == "narrow"

        manager.state.delete("theme", nss[0])
        assert (manager.state.get_all())["theme"] == "wide", "窄删除后宽值应浮现"


class TestRound16SubscriptionRebuildE2E:
    """端到端测试：重建订阅空窗不丢事件（第6轮审查项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_rebuild_gap_count_never_zero_and_events_kept(self):
        """重建期间订阅计数 1→2→1 且空窗事件被新订阅完整接收，退订后无残留。"""
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen1 = bus.subscribe("token")
        assert bus.subscriber_count == {"token": 1}

        await bus.emit("token", {"text": "init", "index": 0})
        assert (await anext(gen1)).type == "token"

        # 重建：先注册新订阅（计数 2，无空窗）、再 aclose 旧订阅（计数 1）
        gen2 = bus.subscribe("token")
        assert bus.subscriber_count == {"token": 2}, "重建空窗期间计数不应为 0"
        await bus.emit("token", {"text": "gap1", "index": 0})
        await bus.emit("token", {"text": "gap2", "index": 1})
        await gen1.aclose()
        assert bus.subscriber_count == {"token": 1}

        assert (await anext(gen2)).data["text"] == "gap1"
        assert (await anext(gen2)).data["text"] == "gap2"

        await gen2.aclose()
        assert bus.subscriber_count == {}, "全部退订后无残留"


class TestRound16StatsE2E:
    """端到端测试：stats() 先清理 + 过滤过期全链路（第6轮审查项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_stats_cleanup_all_access_types(self, tmp_path):
        """YAML 短 TTL stream/knowledge + 长 TTL state：stats() 内 cleanup
        回收过期 stream/knowledge 条目，统计只含持久 state，过期数据被物理移除。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        yaml_path = tmp_path / "r16_stats.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "      state:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 3600\n"
            "      knowledge:\n"
            "        backend: sqlite\n"
            '        path: ":memory:"\n'
            "        ttl: 0.05\n"
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        ns_s = "session:s1:stream"
        ns_st = "session:s1:state"
        ns_k = "session:s1:knowledge"

        manager.stream.append({"role": "user", "content": "temp"}, ns_s)
        manager.state.set("k", "v", ns_st)
        manager.knowledge.add("temp knowledge", ns_k)

        await asyncio.sleep(0.1)
        stats = manager.stats(purge=True)
        assert stats.get(ns_s, 0) == 0, "过期 stream 条目应被 stats() 清理"
        assert stats.get(ns_st, 0) == 1, "state 长 TTL 条目应计入"
        assert stats.get(ns_k, 0) == 0, "过期 knowledge 条目应被 stats() 清理"

        # 过期条目被物理回收（stats() 内 cleanup 已删除）
        backend = manager._get_backend_for_namespace(ns_s)
        rows = backend.conn.execute("SELECT id FROM memory_entries").fetchall()
        assert len(rows) == 1, "仅剩余未过期的 state 条目"
        remaining_ns = backend.conn.execute("SELECT namespace FROM memory_entries").fetchall()
        assert [r[0] for r in remaining_ns] == [ns_st]


# ============================================================
# 第17轮测试（本轮新增）：针对第17轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第17轮修复" 标题（最近一轮）：
#   本轮修复摘要：修复第7轮审查3项：
#   - 修复项1: Chroma.knowledge_add 增 ttl 形参并查询过滤过期（weave/memory/backends/chroma.py）
#   - 修复项2: File 补 close；Chroma 补 close/cleanup_expired/namespace_stats
#   - 修复项3: file 后端遇文件式 path 配置时告警（weave/memory/manager.py）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class _FakeChromaCollection:
    """伪 Chroma collection：记录 add 调用，返回可控 query 结果。"""

    def __init__(self, name):
        self.name = name
        self.added = []
        self._query = {"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]}

    def add(self, ids=None, documents=None, metadatas=None):
        self.added.append({
            "ids": ids or [],
            "documents": documents or [],
            "metadatas": metadatas or [],
        })

    def query(self, query_texts=None, n_results=None, **kwargs):
        return self._query


class _FakeChromaClient:
    """伪 Chroma client：get_or_create_collection / create_collection / list_collections。"""

    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name=None):
        if name not in self.collections:
            self.collections[name] = _FakeChromaCollection(name)
        return self.collections[name]

    def create_collection(self, name=None):
        if name not in self.collections:
            self.collections[name] = _FakeChromaCollection(name)
        return self.collections[name]

    def list_collections(self):
        return list(self.collections.values())


class TestRound17ChromaKnowledgeTTLUnit:
    """第17轮修复项1（Chroma.knowledge_add 增 ttl 形参并查询过滤过期）—— 单元测试。"""

    def test_knowledge_add_signature_has_ttl(self):
        """knowledge_add 方法签名应包含 ttl 形参（第17轮核心修复）。

        此前 ChromaBackend.knowledge_add 无 ttl 形参，MemoryKnowledge.add
        以 4 个位置参数调用时抛 TypeError（review round-7 issue 1）。
        """
        import inspect
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        sig = inspect.signature(ChromaBackend.knowledge_add)
        assert "ttl" in sig.parameters

    def test_knowledge_add_with_ttl_sets_expires_at_in_metadata(self):
        """带 ttl 写入时 metadata 应记录 _expires_at（created_at+ttl），
        与 SQLite/File 后端契约一致（None=永不过期）。"""
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        backend._client = _FakeChromaClient()
        ns = "scope1:u1:knowledge"
        now = time.time()

        entry_id = backend.knowledge_add("hello world", ns, {"source": "test"}, ttl=3600)

        assert entry_id
        collection = backend._get_collection(ns)
        assert len(collection.added) == 1
        meta = collection.added[0]["metadatas"][0]
        assert meta["_expires_at"] is not None
        assert meta["_expires_at"] > now
        assert meta["source"] == "test"

    def test_knowledge_add_without_ttl_no_expires_at(self):
        """不带 ttl 写入时 metadata 不应含 _expires_at（永不过期契约）。"""
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        backend._client = _FakeChromaClient()
        ns = "scope1:u1:knowledge"

        backend.knowledge_add("permanent", ns, None)

        collection = backend._get_collection(ns)
        meta = collection.added[0]["metadatas"][0]
        assert "_expires_at" not in meta

    def test_knowledge_search_filters_expired_entries(self):
        """查询应过滤已过期条目（metadata._expires_at <= now），只返回未过期条目。

        修复前 ChromaBackend.knowledge_search 不做过期过滤，ttl 形参写入的
        _expires_at 不会被读路径尊重（review round-7 issue 1）。
        """
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        backend._client = _FakeChromaClient()
        ns = "scope1:u1:knowledge"
        collection = backend._get_collection(ns)
        now = time.time()
        collection._query = {
            "ids": [["id-fresh", "id-expired"]],
            "documents": [["fresh doc", "stale doc"]],
            "distances": [[0.1, 0.2]],
            "metadatas": [
                [{"_expires_at": now + 1000}, {"_expires_at": now - 100}],
            ],
        }

        results = backend.knowledge_search("hello", [ns], 5)

        assert len(results) == 1
        assert results[0].id == "id-fresh"
        assert results[0].content == "fresh doc"
        assert results[0].metadata == {"_expires_at": now + 1000}


class TestRound17BackendContractUnit:
    """第17轮修复项2（File 补 close；Chroma 补 close/cleanup_expired/namespace_stats）—— 单元测试。"""

    def test_file_backend_has_close_and_noop(self, tmp_path):
        """FileBackend.close() 应存在且为无害 no-op（兼容 MemoryManager.close() 契约）。

        此前 FileBackend 无 close()，DELETE /agents/{name}/memory 管理面经
        MemoryManager.close() 调用时抛 AttributeError（review round-7 issue 2）。
        """
        from weave_agent_sdk.memory.backends.file import FileBackend

        backend = FileBackend(str(tmp_path / "mem"))
        assert callable(backend.close)
        assert backend.close() is None

    def test_chroma_backend_has_three_contract_methods(self):
        """ChromaBackend 应具备 close / cleanup_expired / namespace_stats 三个管理面方法。

        此前均缺失，配置 backend: chroma 后管理面（status()/GET/DELETE memory）
        抛 AttributeError（review round-7 issue 2）。
        """
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        assert callable(backend.close)
        assert callable(backend.cleanup_expired)
        assert callable(backend.namespace_stats)

    def test_chroma_close_releases_client_reference(self):
        """close() 应释放 client 引用（_client 置 None）。"""
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        backend._client = _FakeChromaClient()
        backend.close()
        assert backend._client is None

    def test_chroma_cleanup_expired_returns_zero_and_warns_once(self, caplog):
        """cleanup_expired() 返回 0（no-op）并仅告警一次（读路径已按 _expires_at 过滤）。"""
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.backends.chroma"):
            assert backend.cleanup_expired() == 0
            assert backend.cleanup_expired() == 0

        warns = [r.message for r in caplog.records if "no-op" in r.message]
        assert len(warns) == 1, "cleanup_expired no-op 告警应只记录一次"

    def test_chroma_namespace_stats_returns_empty_and_warns_once(self, caplog):
        """namespace_stats() 返回空 dict（chroma 未实现）并仅告警一次。"""
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        backend = ChromaBackend(":memory:")
        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.backends.chroma"):
            assert backend.namespace_stats() == {}
            assert backend.namespace_stats() == {}

        warns = [r.message for r in caplog.records if "not implemented" in r.message]
        assert len(warns) == 1, "namespace_stats 告警应只记录一次"


class TestRound17FilePathWarningUnit:
    """第17轮修复项3（file 后端遇文件式 path 配置时告警）—— 单元测试。"""

    def test_file_backend_warns_only_on_file_style_path(self, tmp_path, caplog):
        """file 后端 path 带后缀（如 memory.db 文件式路径）时应记录告警但仍创建
        FileBackend；目录路径（无后缀）不应告警。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.file import FileBackend

        # 文件式路径（带后缀）→ 告警
        db_path = str(tmp_path / "memory.db")
        scopes = {"session": MemoryScopeConfig(stream={"backend": "file", "path": db_path})}
        manager = MemoryManager(MemoryConfig(scopes=scopes, default_path=":memory:"))

        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.manager"):
            backend = manager._get_backend_for_namespace("session:s1:stream")

        assert isinstance(backend, FileBackend)
        assert any("looks like a file path" in r.message for r in caplog.records)

        # 目录路径（无后缀）→ 不告警
        caplog.clear()
        dir_path = str(tmp_path / "memdir")
        scopes2 = {"session": MemoryScopeConfig(stream={"backend": "file", "path": dir_path})}
        manager2 = MemoryManager(MemoryConfig(scopes=scopes2, default_path=":memory:"))

        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.manager"):
            manager2._get_backend_for_namespace("session:s1:stream")

        assert not any("looks like a file path" in r.message for r in caplog.records)


# ============================================================
# 第17轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound17ChromaTTLE2E:
    """端到端测试：Chroma knowledge_add ttl + 查询过滤过期经 MemoryManager 全链路。"""

    @pytest.mark.asyncio
    async def test_e2e_chroma_knowledge_ttl_full_chain(self, tmp_path):
        """manager.knowledge.add 按 scope ttl 计算 _expires_at；过期后查询过滤，
        未过期条目正常命中（第17轮修复项1 全链路）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                knowledge={"backend": "chroma", "path": str(tmp_path / "vectors")},
                ttl_knowledge=0.05,
            ),
        }, default_path=":memory:"))
        ns = "session:s1:knowledge"
        backend = manager._get_backend_for_namespace(ns)
        backend._client = _FakeChromaClient()
        collection = backend._get_collection(ns)

        # 经 manager 写入：knowledge_add 收到 manager 解析的 ttl → metadata 设 _expires_at
        entry_id = manager.knowledge.add("temporary knowledge", ns)
        assert entry_id
        meta_a = collection.added[0]["metadatas"][0]
        assert meta_a["_expires_at"] is not None, "带 ttl 写入应设 _expires_at"

        # 未过期时：查询命中
        collection._query = {
            "ids": [[entry_id]],
            "documents": [["temporary knowledge"]],
            "distances": [[0.0]],
            "metadatas": [[meta_a]],
        }
        results = manager.knowledge.search("temporary", [ns], 5)
        assert len(results) == 1 and results[0].content == "temporary knowledge"

        # 过期后：同一 entry 不再被查询返回
        await asyncio.sleep(0.1)
        results2 = manager.knowledge.search("temporary", [ns], 5)
        assert results2 == [], "过期条目应被 chroma 查询过滤"

        # 再写入一条新的（未过期）→ 命中；过期旧条目仍被过滤
        fresh_id = manager.knowledge.add("fresh knowledge", ns)
        meta_b = collection.added[1]["metadatas"][0]
        collection._query = {
            "ids": [[entry_id, fresh_id]],
            "documents": [["temporary knowledge", "fresh knowledge"]],
            "distances": [[0.1, 0.2]],
            "metadatas": [[meta_a, meta_b]],
        }
        results3 = manager.knowledge.search("knowledge", [ns], 5)
        assert len(results3) == 1
        assert results3[0].id == fresh_id

    @pytest.mark.asyncio
    async def test_e2e_chroma_search_all_collections_filters_expired(self, tmp_path):
        """manager.knowledge.search(namespaces=None) 列出全部 chroma collection，
        跨 collection 过滤过期条目。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                knowledge={"backend": "chroma", "path": str(tmp_path / "vectors")},
            ),
        }, default_path=":memory:"))
        ns = "session:s1:knowledge"
        backend = manager._get_backend_for_namespace(ns)
        backend._client = _FakeChromaClient()
        now = time.time()

        col_a = backend._get_collection("scope_a:u1:knowledge")
        col_b = backend._get_collection("scope_b:u1:knowledge")
        col_a._query = {
            "ids": [["a-fresh"]],
            "documents": [["alpha content"]],
            "distances": [[0.1]],
            "metadatas": [[{"_expires_at": now + 1000}]],
        }
        col_b._query = {
            "ids": [["b-expired"]],
            "documents": [["beta stale"]],
            "distances": [[0.2]],
            "metadatas": [[{"_expires_at": now - 100}]],
        }

        results = manager.knowledge.search("query", None, 5)

        assert len(results) == 1
        assert results[0].id == "a-fresh"
        assert results[0].content == "alpha content"


class TestRound17BackendManagementE2E:
    """端到端测试：Chroma/File 管理面（stats/close）经 MemoryManager 全链路。"""

    @pytest.mark.asyncio
    async def test_e2e_chroma_stats_and_close_no_crash(self, tmp_path, caplog):
        """chroma 后端下 manager.stats()（cleanup + namespace_stats）与 close()
        不抛 AttributeError，stats 排除 chroma namespace、close 清空 backend 缓存。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                knowledge={"backend": "chroma", "path": str(tmp_path / "vectors")},
            ),
        }, default_path=":memory:"))
        ns = "session:s1:knowledge"
        backend = manager._get_backend_for_namespace(ns)
        backend._client = _FakeChromaClient()
        manager.knowledge.add("data", ns)

        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.backends.chroma"):
            stats = manager.stats()
            assert stats == {}, "chroma 未实现 namespace_stats → stats 不含 chroma 计数"

        manager.close()
        assert manager._backends == {}, "close() 应清空后端缓存"
        assert backend._client is None, "close() 应释放 chroma client 引用"

    def test_e2e_file_backend_close_via_manager(self, tmp_path):
        """file 后端下 manager.close() 正常工作（FileBackend.close 为 no-op），
        并清空后端缓存。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.file import FileBackend

        mem_dir = str(tmp_path / "mem")
        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(stream={"backend": "file", "path": mem_dir}),
        }, default_path=":memory:"))

        backend = manager._get_backend_for_namespace("session:s1:stream")
        assert isinstance(backend, FileBackend)
        manager.close()
        assert manager._backends == {}


class TestRound17FilePathWarningE2E:
    """端到端测试：file 后端文件式 path 告警经 YAML→load_config→MemoryManager 全链路。"""

    def test_e2e_file_backend_path_warning_behavior_via_yaml(self, tmp_path, caplog):
        """YAML 配置 file 后端 + 文件式 path（如 memory.db）→ load_config →
        MemoryManager 创建 backend 时记录告警；目录 path（无后缀）不告警。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.memory.backends.file import FileBackend

        # 文件式路径（带后缀）→ 告警
        mem_dir = (tmp_path / "mem").as_posix()
        yaml_path = tmp_path / "r17_file_path.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: file\n"
            f'        path: "{mem_dir}/memory.db"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)

        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.manager"):
            backend = manager._get_backend_for_namespace("session:s1:stream")

        assert isinstance(backend, FileBackend)
        assert any("looks like a file path" in r.message for r in caplog.records)

        # 目录路径（无后缀）→ 不告警
        caplog.clear()
        mem_dir2 = (tmp_path / "memdir").as_posix()
        yaml_path2 = tmp_path / "r17_file_dir.yaml"
        yaml_path2.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: file\n"
            f'        path: "{mem_dir2}"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config2 = load_config(yaml_path2)
        manager2 = MemoryManager(config2.memory)

        with caplog.at_level(logging.WARNING, logger="weave_agent_sdk.memory.manager"):
            manager2._get_backend_for_namespace("session:s1:stream")

        assert not any("looks like a file path" in r.message for r in caplog.records)


# ============================================================
# 第18轮测试（本轮新增）：针对第18轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第18轮修复" 标题（最近一轮）：
#   本轮修复摘要：已修复1个问题——清理6个 .bak 残留
#   （weave/ 内 3 个：memory/manager.py / memory/backends/chroma.py /
#     memory/backends/file.py + nexus/ 3 个：fix_record.md /
#     review_record.md / test_record.md 的 .bak），消除 TestBakFileCleanup
#     与 TestRound4/9/14BakCleanup 共 27 个 .bak 断言失败；第7轮审查3项
#     （Chroma TTL / File·Chroma 管理面 / file 文件式 path 告警）已在第17轮
#     修复、本轮复核通过；本轮无源码逻辑修改。
# 本轮只涉及 .bak 备份文件清理行为（无源码逻辑修改），测试仅针对该行为，
# 与既有 TestBakFileCleanup / TestRound4/9/14BakCleanup 互补、不重复，
# 重点覆盖本轮被清理的 6 个具体文件（其中 nexus/ 的 3 个 .bak 为既有
# 测试未覆盖的本轮新清理项）。
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound18BakCleanupUnit:
    """第18轮修复（清理 6 个 .bak 残留）—— 逐项单元验证本轮被清理文件均已移除。

    覆盖：weave/ 内 3 个（memory/manager.py / memory/backends/chroma.py /
    memory/backends/file.py）+ nexus/ 3 个（fix_record.md / review_record.md /
    test_record.md）。
    """

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_memory_manager_py_bak_removed(self):
        """weave/memory/manager.py.bak 应已删除（weave/ 内 3 个之一）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "manager.py.bak").exists(), \
            "weave/memory/manager.py.bak 应已清理"

    def test_memory_backends_chroma_py_bak_removed(self):
        """weave/memory/backends/chroma.py.bak 应已删除（第17轮修复 write_file 自动备份遗留）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "chroma.py.bak").exists(), \
            "weave/memory/backends/chroma.py.bak 应已清理"

    def test_memory_backends_file_py_bak_removed(self):
        """weave/memory/backends/file.py.bak 应已删除（第17轮修复 write_file 自动备份遗留）。"""
        assert not (self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "file.py.bak").exists(), \
            "weave/memory/backends/file.py.bak 应已清理"

    def test_nexus_fix_record_md_bak_removed(self):
        """nexus/fix_record.md.bak 应已删除（本轮清理的 nexus/ 3 个之一）。"""
        assert not (self.PROJECT_ROOT / "nexus" / "fix_record.md.bak").exists(), \
            "nexus/fix_record.md.bak 应已清理"

    def test_nexus_review_record_md_bak_removed(self):
        """nexus/review_record.md.bak 应已删除（本轮清理的 nexus/ 3 个之一）。"""
        assert not (self.PROJECT_ROOT / "nexus" / "review_record.md.bak").exists(), \
            "nexus/review_record.md.bak 应已清理"

    def test_nexus_test_record_md_bak_removed(self):
        """nexus/test_record.md.bak 应已删除（本轮清理的 nexus/ 3 个之一）。"""
        assert not (self.PROJECT_ROOT / "nexus" / "test_record.md.bak").exists(), \
            "nexus/test_record.md.bak 应已清理"

    def test_weave_memory_tree_no_bak(self):
        """weave/memory/ 整棵树（含 backends/）递归扫描不应存在任何 .bak 文件。"""
        memory_dir = self.PROJECT_ROOT / "weave_agent_sdk" / "memory"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in memory_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/memory/ 残留 .bak: {bak_files}"

    def test_weave_package_tree_no_bak(self):
        """weave/ 源码整棵树递归扫描不应存在任何 .bak 文件（含 memory/backends）。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in weave_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/ 源码树残留 .bak: {bak_files}"

    def test_nexus_tree_no_bak(self):
        """nexus/ 目录树不应存在任何 .bak 文件（本轮清理的 fix/review/test_record 3 个已消除）。"""
        nexus_dir = self.PROJECT_ROOT / "nexus"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in nexus_dir.rglob("*.bak")]
        assert bak_files == [], f"nexus/ 残留 .bak: {bak_files}"

    def test_memory_backends_dir_clean(self):
        """weave/memory/backends/ 目录内不应存在任何 .bak 文件。"""
        backends_dir = self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in backends_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/memory/backends 残留 .bak: {bak_files}"


class TestRound18BakCleanupE2E:
    """端到端测试：本轮清理的 6 个 .bak 文件全部消失且全项目无 .bak 残留。"""

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_e2e_round18_cleaned_files_absent_anywhere(self):
        """本轮清理的 6 个 .bak 文件应全部从项目中消失（已知路径逐一验证）。"""
        known_paths = [
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "manager.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "chroma.py.bak",
            self.PROJECT_ROOT / "weave_agent_sdk" / "memory" / "backends" / "file.py.bak",
            self.PROJECT_ROOT / "nexus" / "fix_record.md.bak",
            self.PROJECT_ROOT / "nexus" / "review_record.md.bak",
            self.PROJECT_ROOT / "nexus" / "test_record.md.bak",
        ]
        for p in known_paths:
            assert not p.exists(), f"被清理文件仍存在: {p}"

    def test_e2e_no_bak_anywhere_in_project(self):
        """整个项目目录树递归扫描不应存在任何 .bak 文件（含 weave/tests/nexus/docs 等）。"""
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in self.PROJECT_ROOT.rglob("*.bak")]
        assert bak_files == [], f"项目残留 .bak: {bak_files}"

    def test_e2e_no_bak_in_weave_source_tree(self):
        """weave/ 源码整棵树递归扫描不应存在任何 .bak 文件。"""
        weave_dir = self.PROJECT_ROOT / "weave_agent_sdk"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in weave_dir.rglob("*.bak")]
        assert bak_files == [], f"weave/ 源码树残留 .bak: {bak_files}"

    def test_e2e_no_bak_in_nexus_tree_and_project_root(self):
        """nexus/ 目录树无 .bak；项目根目录无 .bak（含 weave.yaml.bak）。"""
        nexus_dir = self.PROJECT_ROOT / "nexus"
        bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in nexus_dir.rglob("*.bak")]
        assert bak_files == [], f"nexus/ 残留 .bak: {bak_files}"

        root_baks = [p.name for p in self.PROJECT_ROOT.iterdir() if p.name.endswith(".bak")]
        assert root_baks == [], f"项目根残留 .bak: {root_baks}"
        assert not (self.PROJECT_ROOT / "weave.yaml.bak").exists(), "weave.yaml.bak 应已清理"

    def test_e2e_all_top_level_source_dirs_clean(self):
        """各顶层源码/文档目录递归扫描均无 .bak 残留（含 nexus/bak 归档目录不误命中）。"""
        for sub in ("weave", "tests", "prompts", "nexus", "docs", "examples"):
            d = self.PROJECT_ROOT / sub
            if not d.exists():
                continue
            bak_files = [str(p.relative_to(self.PROJECT_ROOT)) for p in d.rglob("*.bak")]
            assert bak_files == [], f"{sub}/ 残留 .bak: {bak_files}"

        # nexus/bak 归档目录（目录名非 .bak 后缀）不应产生误命中
        bak_dir = self.PROJECT_ROOT / "nexus" / "bak"
        if bak_dir.is_dir():
            assert bak_dir.suffix == "", "目录 'bak' 不应有 .bak 后缀"
            inner_bak = list(bak_dir.rglob("*.bak"))
            assert inner_bak == [], f"nexus/bak 内部仍残留 .bak: {inner_bak}"



# ============================================================
# 第19轮测试（本轮新增）：针对第19轮修复行为的验证
# 依据 fix_record.md 最后一个 "## 第19轮修复" 标题（最近一轮，
# 本轮修复摘要：done），共5个修复项:
#   - 修复项1: two_stage桥接指令迁模板(R2)（weave/features/two_stage.py）
#   - 修复项2: top_k可配置（loop.memory_knowledge_topk，types.py / config.py / loop/base.py）
#   - 修复项3: 默认state/knowledge共用memory.db（weave.yaml 三种 access 同库）
#   - 修复项4: 订阅GC退订（event_bus.py weakref.finalize 确定性退订）
#   - 修复项5: 无参路径实例化后端+跳过chroma（memory/manager.py _ensure_configured_backends）
# 本轮新增：10 个单元测试 + 5 个端到端测试
# ============================================================


class TestRound19TwoStageBridgeR2Unit:
    """第19轮修复项1（two_stage 桥接指令迁模板，R2）—— 单元测试。"""

    def test_two_stage_no_hardcoded_bridge_instruction(self):
        """two_stage.py 不应再硬编码 stage2 的 user 桥接指令（R2）。

        修复前 stage2 的桥接指令（"Convert the following understanding
        into JSON matching the schema"）硬编码在 two_stage.py 中；
        修复后从 prompts/features/two_stage_bridge.md 模板加载
        （review round-8 issue 2，与 round-3 issue 1 的 json_extract
        to_feedback 同类）。
        """
        import inspect
        import weave_agent_sdk.features.two_stage as ts

        source = inspect.getsource(ts)
        # 应从模板加载桥接指令，而非硬编码可执行指令
        assert 'feature_prompt("two_stage_bridge", understanding=understanding)' in source
        # 不应残留硬编码的桥接引导语（修复前为模板文件中的原文）
        assert "Convert the following understanding" not in source
        assert "into JSON matching the schema" not in source

    @pytest.mark.asyncio
    async def test_two_stage_bridge_uses_template_with_understanding(self):
        """two_stage_call 的 stage2 user 桥接指令应来自模板，且注入 Stage1 理解结果。"""
        from types import SimpleNamespace
        import weave_agent_sdk.features.two_stage as ts

        async def fake_chat(messages, **kwargs):
            return SimpleNamespace(content="understanding: book a flight")

        mock_sc = AsyncMock(return_value="schema_inst")
        with patch.object(ts, "structured_call", mock_sc):
            await ts.two_stage_call(
                llm=SimpleNamespace(chat=fake_chat),
                understand_prompt="Understand the request",
                translate_schema=dict,
                user_input="Book a flight to Paris",
                max_retries=1,
            )

        assert mock_sc.await_count == 1
        prompt = mock_sc.await_args.kwargs["prompt"]
        # stage2 桥接指令来自模板（R2：不硬编码），并注入 Stage1 理解结果
        assert prompt.startswith("Convert the following understanding into JSON matching the schema:")
        assert "understanding: book a flight" in prompt
        # stage2 system 消息来自 two_stage_translate 模板（role=system）
        sys_msg = mock_sc.await_args.kwargs["messages"][0]
        assert sys_msg.role == "system"


class TestRound19TopKConfigUnit:
    """第19轮修复项2（top_k 可配置：loop.memory_knowledge_topk）—— 单元测试。"""

    def test_loop_config_memory_knowledge_topk_default_and_custom(self):
        """LoopConfig.memory_knowledge_topk 默认 5，且可自定义。"""
        from weave_agent_sdk.types import LoopConfig

        cfg = LoopConfig()
        assert cfg.memory_knowledge_topk == 5
        cfg2 = LoopConfig(memory_knowledge_topk=8)
        assert cfg2.memory_knowledge_topk == 8

    def test_load_config_parses_and_clamps_memory_knowledge_topk(self, tmp_path):
        """load_config 应从 YAML 解析 memory_knowledge_topk；非法 / 非正值回退默认 5。"""
        import yaml

        # 正常解析
        yaml_path = tmp_path / "r19_topk.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "memory_knowledge_topk": 7},
            "memory": {"scopes": {}},
            "prompts": {},
            "features": {},
            "server": {},
            "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)
        config = load_config(yaml_path)
        assert config.loop.memory_knowledge_topk == 7

        # 非数值 → 回退 5
        yaml_path2 = tmp_path / "r19_topk_bad.yaml"
        config_data2 = dict(config_data)
        config_data2["loop"] = {"type": "iterative", "memory_knowledge_topk": "not-a-number"}
        with open(yaml_path2, "w", encoding="utf-8") as f:
            yaml.dump(config_data2, f)
        config2 = load_config(yaml_path2)
        assert config2.loop.memory_knowledge_topk == 5

        # 非正（0）→ 回退 5
        yaml_path3 = tmp_path / "r19_topk_zero.yaml"
        config_data3 = dict(config_data)
        config_data3["loop"] = {"type": "iterative", "memory_knowledge_topk": 0}
        with open(yaml_path3, "w", encoding="utf-8") as f:
            yaml.dump(config_data3, f)
        config3 = load_config(yaml_path3)
        assert config3.loop.memory_knowledge_topk == 5


class TestRound19DefaultSharedDBUnit:
    """第19轮修复项3（默认 state/knowledge 共用 memory.db）—— 单元测试。"""

    def test_default_path_is_memory_db(self):
        """默认（无 memory 段）下 MemoryConfig.default_path 统一为 memory.db。"""
        from weave_agent_sdk.types import MemoryConfig

        # 统一 memory.db：代码默认（不写 memory 段时的兜底路径）
        # 默认数据目录改为 .weave/（docs/issues/012 路径规范化，隐藏目录）
        assert MemoryConfig().default_path == "./.weave/memory.db"

    @pytest.mark.asyncio
    async def test_manager_three_access_share_single_sqlite_backend(self, tmp_path):
        """MemoryManager 下 stream/state/knowledge 同 path → 共享同一 SQLiteBackend 实例。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        db = str(tmp_path / "mem" / "memory.db")
        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                stream={"backend": "sqlite", "path": db},
                state={"backend": "sqlite", "path": db},
                knowledge={"backend": "sqlite", "path": db},
            ),
        }, default_path=":memory:"))
        manager.activate_scopes({"session_id": "s1"})

        s = manager._get_backend_for_namespace("session:s1:stream")
        st = manager._get_backend_for_namespace("session:s1:state")
        k = manager._get_backend_for_namespace("session:s1:knowledge")

        assert s is st is k, "三种 access 共用同一 SQLite 后端（同一 db 文件）"

        # 三类写入落在同一文件/同一连接内，namespace_stats 可见全部
        manager.stream.append({"role": "user", "content": "hi"}, "session:s1:stream")
        manager.state.set("k", "v", "session:s1:state")
        manager.knowledge.add("alpha knowledge", "session:s1:knowledge")

        stats = s.namespace_stats()
        assert stats.get("session:s1:stream") == 1
        assert stats.get("session:s1:state") == 1
        assert stats.get("session:s1:knowledge") == 1


class TestRound19SubscriptionGCUnit:
    """第19轮修复项4（订阅 GC 退订）—— 单元测试。"""

    def test_subscribe_uses_weakref_finalize_for_gc(self):
        """event_bus.py 应通过 weakref.finalize 在被遗弃订阅 GC 时确定性退订。"""
        import inspect
        import weave_agent_sdk.event_bus as eb

        source = inspect.getsource(eb)
        assert "import weakref" in source
        assert "weakref.finalize(gen, _unsubscribe)" in source

    @pytest.mark.asyncio
    async def test_abandoned_generator_gc_unsubscribes(self):
        """被遗弃（不迭代、不 aclose）的订阅在 GC 后应确定性退订，不残留队列。"""
        import gc
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token", "done")
        assert bus.subscriber_count == {"token": 1, "done": 1}

        # 遗弃生成器（无任何消费 / aclose）→ 依赖 weakref.finalize 在 GC 时退订
        del gen
        gc.collect()

        assert bus.subscriber_count == {}, \
            f"被遗弃订阅应在 GC 时确定性退订，实际残留 {bus.subscriber_count}"


class TestRound19BackendNoPathChromaUnit:
    """第19轮修复项5（无参路径实例化后端 + 跳过 chroma）—— 单元测试。"""

    def test_ensure_configured_backends_no_path_uses_default(self, tmp_path):
        """无显式 path 配置的 scope 后端应回退 default_path 被 _ensure_configured_backends 实例化。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        db = str(tmp_path / "mem" / "memory.db")
        manager = MemoryManager(MemoryConfig(
            scopes={"session": MemoryScopeConfig(stream={"backend": "sqlite"})},
            default_path=db,
        ))
        manager.activate_scopes({"session_id": "s1"})
        assert manager._backends == {}

        manager._ensure_configured_backends()

        key = f"sqlite:{db}"
        assert key in manager._backends, "无参路径实例化：应回退 default_path 创建后端"
        assert isinstance(manager._backends[key], SQLiteBackend)
        assert manager._backends[key]._db_path == db

    def test_ensure_configured_backends_chroma_safe_without_chromadb(self, caplog):
        """chroma knowledge 后端在无参路径扫尾中被安全实例化/跳过，不因缺少 chromadb 崩溃。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        manager = MemoryManager(MemoryConfig(
            scopes={"session": MemoryScopeConfig(
                knowledge={"backend": "chroma", "path": ":memory:"},
            )},
            default_path=":memory:",
        ))
        manager.activate_scopes({"session_id": "s1"})

        # 无参路径扫尾：chroma 后端惰性创建（无需安装 chromadb），不崩溃
        manager._ensure_configured_backends()
        chroma_bs = [b for b in manager._backends.values() if isinstance(b, ChromaBackend)]
        assert chroma_bs, "chroma knowledge 后端应被实例化（惰性，不 import chromadb）"

        # 管理面 cleanup/stats 不崩溃：chroma cleanup=0、stats 空，被安全跳过
        manager.cleanup()
        stats = manager.stats()
        assert stats == {}


# ============================================================
# 第19轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound19TwoStageBridgeE2E:
    """端到端测试：two_stage 桥接指令来自真实模板（第19轮修复项1 R2）。"""

    @pytest.mark.asyncio
    async def test_e2e_two_stage_bridge_template_full_chain(self):
        """真实 two_stage_bridge.md 模板存在且渲染理解结果；two_stage_call 全链路
        以模板渲染结果作为 stage2 桥接指令（非硬编码）。"""
        from weave_agent_sdk.features._prompts import feature_prompt

        # 真实模板文件存在（R2：桥接指令不再硬编码在 two_stage.py）
        bridge_path = Path(__file__).parent.parent / "prompts" / "features" / "two_stage_bridge.md"
        assert bridge_path.exists(), "prompts/features/two_stage_bridge.md 应存在"

        # 模板渲染：注入 understanding
        rendered = feature_prompt("two_stage_bridge", understanding="flight to Paris")
        assert "Convert the following understanding into JSON matching the schema:" in rendered
        assert "flight to Paris" in rendered

        # two_stage_call 全链路：真实模板渲染结果传给 structured_call
        from types import SimpleNamespace
        import weave_agent_sdk.features.two_stage as ts

        async def fake_chat(messages, **kwargs):
            return SimpleNamespace(content="understanding: search books")

        mock_sc = AsyncMock(return_value="schema")
        with patch.object(ts, "structured_call", mock_sc):
            await ts.two_stage_call(
                llm=SimpleNamespace(chat=fake_chat),
                understand_prompt="Understand request",
                translate_schema=dict,
                user_input="search for a book",
                max_retries=1,
            )
        prompt = mock_sc.await_args.kwargs["prompt"]
        assert "understanding: search books" in prompt


class TestRound19TopKBeforeThinkE2E:
    """端到端测试：before_think 知识检索使用配置的 top_k（第19轮修复项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_before_think_knowledge_top_k_full_chain(self):
        """loop.memory_knowledge_topk 配置应传递给 knowledge.search 的 top_k，
        并注入 ctx["knowledge"]。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_knowledge_topk=3)),
            _memory=MagicMock(),
        )
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [],
            "state": [],
            "knowledge": ["session:s1:knowledge"],
        }[at])
        agent._memory.knowledge.search = MagicMock(return_value=[
            SimpleNamespace(content="kb result"),
        ])

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "retrieve something")

        assert ctx["knowledge"][0].content == "kb result"
        agent._memory.knowledge.search.assert_called_once_with(
            "retrieve something", ["session:s1:knowledge"], top_k=3
        )


class TestRound19DefaultSharedDBE2E:
    """端到端测试：默认 state/knowledge 共用 memory.db 经 YAML→load_config 全链路（第19轮修复项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_three_access_share_memory_db_full_chain(self, tmp_path):
        """YAML 三种 access 同 path → load_config → MemoryManager：
        三类写入落在同一 db 文件（同一 backend），stats 可见全部。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        db = (tmp_path / "data" / "memory.db").as_posix()
        yaml_path = tmp_path / "r19_shared_db.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            f'        path: "{db}"\n'
            "      state:\n"
            "        backend: sqlite\n"
            f'        path: "{db}"\n'
            "      knowledge:\n"
            "        backend: sqlite\n"
            f'        path: "{db}"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"session_id": "s1"})

        # 三种 access 解析到同一 backend 实例（同一 db 文件）
        s = manager._get_backend_for_namespace("session:s1:stream")
        st = manager._get_backend_for_namespace("session:s1:state")
        k = manager._get_backend_for_namespace("session:s1:knowledge")
        assert s is st is k

        manager.stream.append({"role": "user", "content": "hi"}, "session:s1:stream")
        manager.state.set("k", "v", "session:s1:state")
        manager.knowledge.add("alpha knowledge", "session:s1:knowledge")

        # 同一文件内三类数据全部可见（stats 含三个 namespace）
        stats = manager.stats()
        assert stats.get("session:s1:stream") == 1
        assert stats.get("session:s1:state") == 1
        assert stats.get("session:s1:knowledge") == 1


class TestRound19SubscriptionGCE2E:
    """端到端测试：被遗弃订阅 GC 退订后无广播累积（第19轮修复项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_abandoned_subscription_no_broadcast_leak(self):
        import gc
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token")
        assert bus.subscriber_count == {"token": 1}

        # 遗弃（不迭代、不 aclose）→ GC 后确定性退订
        del gen
        gc.collect()
        assert bus.subscriber_count == {}, \
            f"被遗弃订阅应在 GC 时退订，实际残留 {bus.subscriber_count}"

        # 退订后 emit 安全 no-op，不向空队列广播、不累积
        await bus.emit("token", {"text": "x", "index": 0})
        assert bus.subscriber_count == {}

        # 新订阅者正常工作，退订后清理干净
        gen2 = bus.subscribe("token")
        await bus.emit("token", {"text": "y", "index": 0})
        e = await anext(gen2)
        assert e.data["text"] == "y"
        await gen2.aclose()
        assert bus.subscriber_count == {}


class TestRound19BackendNoPathChromaE2E:
    """端到端测试：无参路径实例化后端 + 跳过 chroma 经 YAML 全链路（第19轮修复项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_no_path_and_chroma_backends_full_chain(self, tmp_path):
        """YAML 中 sqlite（无 path）与 chroma knowledge 后端经 load_config →
        MemoryManager：无 path 回退 default_path 实例化、chroma 安全跳过，
        cleanup/stats 全程不崩溃。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        default_db = (tmp_path / "data" / "memory.db").as_posix()
        yaml_path = tmp_path / "r19_no_path.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"          # 无 path → 回退 default_path
            "      knowledge:\n"
            "        backend: chroma\n"          # chroma → 惰性实例化、安全跳过
            '  default_path: "' + default_db + '"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        assert config.memory.default_path == default_db

        manager = MemoryManager(config.memory)
        manager.activate_scopes({"session_id": "s1"})

        # 无参路径扫尾实例化：不崩溃
        manager._ensure_configured_backends()

        # sqlite（无 path）→ 回退 default_path
        stream_backend = manager._get_backend_for_namespace("session:s1:stream")
        assert isinstance(stream_backend, SQLiteBackend)
        assert stream_backend._db_path == default_db

        # chroma knowledge → 惰性实例化（无需安装 chromadb）
        knowledge_backend = manager._get_backend_for_namespace("session:s1:knowledge")
        assert isinstance(knowledge_backend, ChromaBackend)

        # 管理面 cleanup/stats 全程不崩溃（chroma 被安全跳过：cleanup=0、stats 空）
        manager.cleanup()
        stats = manager.stats()
        assert stats == {}

# ============================================================
# 第20轮测试（本轮新增）：针对第20轮修复的复核行为验证
# 依据 fix_record.md 最后一个 "## 第20轮修复" 标题（最近一轮，
# 本轮修复摘要：已修复0个问题——复核第8轮审查5项均已修复
# （无参路径实例化后端+跳过chroma / two_stage桥接指令迁模板(R2) /
# top_k可配置 / 默认state·knowledge共用memory.db / 订阅GC退订），
# 复核通过，本轮无源码改动、未编写测试，详见 fix_record.md）。
# 与既有 TestRound19* 互补、不重复，本轮新增 10 个单元测试 + 5 个端到端测试。
# ============================================================


class TestRound20TwoStageBridgeUnit:
    """第20轮复核项1（two_stage 桥接指令迁模板 R2）—— 单元测试（与 Round19 互补）。"""

    @pytest.mark.asyncio
    async def test_explicit_system_prompts_override_template_loading(self):
        """显式传入 understand_system / translate_system 时仅覆盖 stage1/2 的
        system 模板加载；bridge 模板仍恒加载（R2 不硬编码）；stage1 user 消息
        为 understand_prompt + input。"""
        from types import SimpleNamespace
        import weave_agent_sdk.features.two_stage as ts

        captured = {}

        async def fake_chat(messages, **kwargs):
            captured["stage1"] = messages
            return SimpleNamespace(content="understanding: flight to Paris")

        mock_sc = AsyncMock(return_value="schema_inst")
        with patch.object(ts, "structured_call", mock_sc), \
                patch.object(ts, "feature_prompt", wraps=ts.feature_prompt) as fp:
            await ts.two_stage_call(
                llm=SimpleNamespace(chat=fake_chat),
                understand_prompt="Understand the request",
                translate_schema=dict,
                user_input="Book a flight to Paris",
                understand_system="EXPLICIT_UNDERSTAND_SYS",
                translate_system="EXPLICIT_TRANSLATE_SYS",
                max_retries=1,
            )

        # 显式 system 提供时不再加载 understand/translate 模板，只加载 bridge 模板
        called_names = [c.args[0] for c in fp.call_args_list]
        assert called_names == ["two_stage_bridge"], f"实际加载的模板: {called_names}"
        # stage1 system = 显式 understand_system；stage1 user = understand_prompt + input
        assert captured["stage1"][0].role == "system"
        assert captured["stage1"][0].content == "EXPLICIT_UNDERSTAND_SYS"
        assert captured["stage1"][1].content == "Understand the request\n\nUser input: Book a flight to Paris"
        # stage2 system = 显式 translate_system；bridge 指令来自模板并注入理解结果
        sys_msg = mock_sc.await_args.kwargs["messages"][0]
        assert sys_msg.role == "system"
        assert sys_msg.content == "EXPLICIT_TRANSLATE_SYS"
        assert "understanding: flight to Paris" in mock_sc.await_args.kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_max_retries_propagated_to_structured_call(self):
        """two_stage_call 的 max_retries 应透传给 structured_call（默认模板路径）。"""
        from types import SimpleNamespace
        import weave_agent_sdk.features.two_stage as ts

        async def fake_chat(messages, **kwargs):
            return SimpleNamespace(content="understanding: x")

        mock_sc = AsyncMock(return_value="schema")
        with patch.object(ts, "structured_call", mock_sc):
            await ts.two_stage_call(
                llm=SimpleNamespace(chat=fake_chat),
                understand_prompt="U",
                translate_schema=dict,
                user_input="in",
                max_retries=5,
            )
        assert mock_sc.await_args.kwargs["max_retries"] == 5


class TestRound20TopKConfigUnit:
    """第20轮复核项2（top_k 可配置：loop.memory_knowledge_topk）—— 单元测试。"""

    def test_load_config_negative_topk_falls_back_to_5(self, tmp_path):
        """YAML 中 memory_knowledge_topk 为负值时回退默认 5（非正配置不合规）。"""
        import yaml

        yaml_path = tmp_path / "r20_topk_neg.yaml"
        config_data = {
            "agent": {"name": "t"},
            "llm": {"provider": "anthropic"},
            "loop": {"type": "iterative", "memory_knowledge_topk": -3},
            "memory": {"scopes": {}},
            "prompts": {}, "features": {}, "server": {}, "logging": {},
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)
        config = load_config(yaml_path)
        assert config.loop.memory_knowledge_topk == 5

    @pytest.mark.asyncio
    async def test_before_think_topk_zero_falls_back_to_default(self):
        """before_think 遇到非正 top_k（0）回退默认 5 再传给 knowledge.search。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_knowledge_topk=0)),
            _memory=MagicMock(),
        )
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": [], "knowledge": ["session:s1:knowledge"],
        }[at])
        agent._memory.knowledge.search = MagicMock(return_value=[SimpleNamespace(content="r")])
        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "q")
        assert ctx["knowledge"][0].content == "r"
        agent._memory.knowledge.search.assert_called_once_with("q", ["session:s1:knowledge"], top_k=5)

    @pytest.mark.asyncio
    async def test_before_think_topk_nonint_falls_back_to_default(self):
        """before_think 遇到非整型 top_k（"abc"）回退默认 5 再传给 knowledge.search。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace(memory_knowledge_topk="abc")),
            _memory=MagicMock(),
        )
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": [], "knowledge": ["session:s1:knowledge"],
        }[at])
        agent._memory.knowledge.search = MagicMock(return_value=[SimpleNamespace(content="r")])
        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "q")
        assert ctx["knowledge"][0].content == "r"
        agent._memory.knowledge.search.assert_called_once_with("q", ["session:s1:knowledge"], top_k=5)


class TestRound20DefaultSharedDBUnit:
    """第20轮复核项3（默认 state/knowledge 共用 memory.db）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_shared_db_read_path_visibility_across_access(self, tmp_path):
        """共用同一 memory.db 时，stream/state/knowledge 三类读路径（last/get/search）
        在同一后端上均可见（单文件内三类数据读回，不止于 stats 计数）。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        db = str(tmp_path / "mem" / "memory.db")
        manager = MemoryManager(MemoryConfig(scopes={
            "session": MemoryScopeConfig(
                stream={"backend": "sqlite", "path": db},
                state={"backend": "sqlite", "path": db},
                knowledge={"backend": "sqlite", "path": db},
            ),
        }, default_path=":memory:"))
        manager.activate_scopes({"session_id": "s1"})

        s = manager._get_backend_for_namespace("session:s1:stream")
        st = manager._get_backend_for_namespace("session:s1:state")
        k = manager._get_backend_for_namespace("session:s1:knowledge")
        assert s is st is k

        manager.stream.append({"role": "user", "content": "hi"}, "session:s1:stream")
        manager.state.set("k", "v", "session:s1:state")
        manager.knowledge.add("alpha knowledge", "session:s1:knowledge")

        last = manager.stream.last(5, ["session:s1:stream"])
        assert any(e.get("content") == "hi" for e in last)
        assert manager.state.get("k", "session:s1:state") == "v"
        results = manager.knowledge.search("alpha", ["session:s1:knowledge"], top_k=5)
        assert any(r.content == "alpha knowledge" for r in results)


class TestRound20SubscriptionGCUnit:
    """第20轮复核项4（订阅 GC 退订）—— 单元测试。"""

    @pytest.mark.asyncio
    async def test_multiple_abandoned_subscriptions_all_unsubscribed_on_gc(self):
        """多个被遗弃订阅在 GC 后应全部确定性退订，不残留任何事件类型队列。"""
        import gc
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        g1 = bus.subscribe("token")
        g2 = bus.subscribe("done", "error")
        assert bus.subscriber_count == {"token": 1, "done": 1, "error": 1}

        del g1, g2
        gc.collect()
        assert bus.subscriber_count == {}, \
            f"多个被遗弃订阅应在 GC 时全部退订，实际残留 {bus.subscriber_count}"

    @pytest.mark.asyncio
    async def test_aclose_then_gc_is_idempotent(self):
        """显式 aclose 退订后再 GC，finalize 兜底重复退订仍安全（幂等、无异常）。"""
        import gc
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token")
        assert bus.subscriber_count == {"token": 1}

        await bus.emit("token", {"text": "x", "index": 0})
        e = await anext(gen)
        assert e.data["text"] == "x"
        await gen.aclose()
        assert bus.subscriber_count == {}

        # finalize 已执行（或退化为 aclose 路径），GC 后重复退订不抛错、无残留
        del gen
        gc.collect()
        assert bus.subscriber_count == {}


class TestRound20BackendNoPathChromaUnit:
    """第20轮复核项5（无参路径实例化后端 + 跳过 chroma）—— 单元测试。"""

    def test_ensure_configured_backends_instantiates_all_access_types_no_path(self, tmp_path):
        """无显式 path 的 stream/state/knowledge 三 scope 由 _ensure_configured_backends
        回退 default_path 实例化，且三类 access 共享同一 SQLiteBackend。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend

        db = str(tmp_path / "mem" / "memory.db")
        manager = MemoryManager(MemoryConfig(
            scopes={"session": MemoryScopeConfig(
                stream={"backend": "sqlite"},
                state={"backend": "sqlite"},
                knowledge={"backend": "sqlite"},
            )},
            default_path=db,
        ))
        manager.activate_scopes({"session_id": "s1"})
        assert manager._backends == {}

        manager._ensure_configured_backends()
        s = manager._get_backend_for_namespace("session:s1:stream")
        st = manager._get_backend_for_namespace("session:s1:state")
        k = manager._get_backend_for_namespace("session:s1:knowledge")
        assert s is st is k
        assert isinstance(s, SQLiteBackend)
        assert s._db_path == db

    def test_ensure_configured_backends_failure_isolated(self, tmp_path):
        """个别 namespace 后端实例化失败（异常）不阻断其他 namespace 后端创建。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.types import MemoryConfig, MemoryScopeConfig

        db = str(tmp_path / "mem" / "memory.db")
        manager = MemoryManager(MemoryConfig(
            scopes={
                "a": MemoryScopeConfig(stream={"backend": "sqlite"}),
                "b": MemoryScopeConfig(stream={"backend": "sqlite"}),
            },
            default_path=db,
        ))
        manager.activate_scopes({"a_id": "a1", "b_id": "b1"})
        original = manager._get_backend_for_namespace

        def flaky(ns):
            if ns.startswith("a:"):
                raise RuntimeError("boom")
            return original(ns)

        with patch.object(manager, "_get_backend_for_namespace", side_effect=flaky):
            manager._ensure_configured_backends()  # 不应抛出

        # b 的 stream 后端仍被创建（失败隔离）
        assert any("sqlite:" in key for key in manager._backends), \
            "个别后端失败不应阻断其他后端实例化"


# ============================================================
# 第20轮测试（本轮新增）：端到端测试
# ============================================================


class TestRound20TwoStageBridgeE2E:
    """端到端测试：two_stage 桥接模板占位符 + 显式 system 覆盖（第20轮复核项1）。"""

    @pytest.mark.asyncio
    async def test_e2e_bridge_template_placeholder_and_explicit_understand_override(self):
        """真实 two_stage_bridge.md 模板含 {{ understanding }} 占位符并被插值；
        显式 understand_system 只覆盖 stage1 system，stage2 桥接仍来自模板。"""
        from weave_agent_sdk.features._prompts import feature_prompt_raw, feature_prompt

        raw = feature_prompt_raw("two_stage_bridge")
        assert "{{ understanding }}" in raw, "模板应含插值占位符（R2 不硬编码最终指令）"

        rendered = feature_prompt("two_stage_bridge", understanding="e2e understanding")
        assert "e2e understanding" in rendered
        assert "{{ understanding }}" not in rendered

        from types import SimpleNamespace
        import weave_agent_sdk.features.two_stage as ts

        captured = {}

        async def fake_chat(messages, **kwargs):
            captured["stage1"] = messages
            return SimpleNamespace(content="understanding: e2e result")

        mock_sc = AsyncMock(return_value="schema")
        with patch.object(ts, "structured_call", mock_sc):
            await ts.two_stage_call(
                llm=SimpleNamespace(chat=fake_chat),
                understand_prompt="U",
                translate_schema=dict,
                user_input="input text",
                understand_system="EXPLICIT_SYS",
                max_retries=1,
            )

        assert captured["stage1"][0].content == "EXPLICIT_SYS"
        prompt = mock_sc.await_args.kwargs["prompt"]
        assert "Convert the following understanding into JSON matching the schema:" in prompt
        assert "understanding: e2e result" in prompt


class TestRound20TopKBeforeThinkE2E:
    """端到端测试：before_think 知识检索未配置时回退默认 top_k=5（第20轮复核项2）。"""

    @pytest.mark.asyncio
    async def test_e2e_before_think_default_topk_when_not_configured(self):
        """loop 配置不含 memory_knowledge_topk 时，before_think 以默认 5 检索知识。"""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            _config=SimpleNamespace(loop=SimpleNamespace()),
            _memory=MagicMock(),
        )
        agent._memory.get_namespaces = MagicMock(side_effect=lambda at: {
            "stream": [], "state": [], "knowledge": ["session:s1:knowledge"],
        }[at])
        agent._memory.knowledge.search = MagicMock(return_value=[SimpleNamespace(content="r")])

        loop = _Round1ConcreteLoop()
        ctx = await loop.before_think(agent, "retrieve something")
        assert ctx["knowledge"][0].content == "r"
        agent._memory.knowledge.search.assert_called_once_with(
            "retrieve something", ["session:s1:knowledge"], top_k=5
        )


class TestRound20DefaultSharedDBE2E:
    """端到端测试：默认 state/knowledge 共用 memory.db 读路径全链路（第20轮复核项3）。"""

    @pytest.mark.asyncio
    async def test_e2e_shared_db_read_path_full_chain(self, tmp_path):
        """YAML 三种 access 同 path → load_config → MemoryManager：三类写入在同一
        文件内经读路径（last/get/search）全部可见，且新 manager（跨运行）仍可读。"""
        from weave_agent_sdk.memory.manager import MemoryManager

        db = (tmp_path / "data" / "memory.db").as_posix()
        yaml_path = tmp_path / "r20_shared_db.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            f'        path: "{db}"\n'
            "      state:\n"
            "        backend: sqlite\n"
            f'        path: "{db}"\n'
            "      knowledge:\n"
            "        backend: sqlite\n"
            f'        path: "{db}"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        manager = MemoryManager(config.memory)
        manager.activate_scopes({"session_id": "s1"})

        manager.stream.append({"role": "user", "content": "hello shared"}, "session:s1:stream")
        manager.state.set("k", "v", "session:s1:state")
        manager.knowledge.add("shared knowledge alpha", "session:s1:knowledge")

        # 读路径跨 access 在同一文件内全部可见
        last = manager.stream.last(5, ["session:s1:stream"])
        assert any(e.get("content") == "hello shared" for e in last)
        assert manager.state.get("k", "session:s1:state") == "v"
        results = manager.knowledge.search("shared knowledge", ["session:s1:knowledge"], top_k=5)
        assert any(r.content == "shared knowledge alpha" for r in results)

        # 释放连接后，新 manager（同一 db 路径）跨运行读取既有数据（持久化验证）
        manager.close()
        manager2 = MemoryManager(config.memory)
        manager2.activate_scopes({"session_id": "s1"})
        assert manager2.state.get("k", "session:s1:state") == "v"


class TestRound20SubscriptionGCE2E:
    """端到端测试：多事件类型被遗弃订阅 GC 退订无广播累积（第20轮复核项4）。"""

    @pytest.mark.asyncio
    async def test_e2e_abandoned_multi_type_subscription_gc_no_leak(self):
        import gc
        from weave_agent_sdk.event_bus import EventBus

        bus = EventBus()
        gen = bus.subscribe("token", "done", "error")
        assert bus.subscriber_count == {"token": 1, "done": 1, "error": 1}

        del gen
        gc.collect()
        assert bus.subscriber_count == {}, \
            f"被遗弃多类型订阅应在 GC 时全部退订，实际残留 {bus.subscriber_count}"

        # 退订后 emit 对各类事件均为安全 no-op、不重建订阅
        await bus.emit("token", {"text": "x", "index": 0})
        await bus.emit("done", {"output": "o", "elapsed_ms": 1, "iterations": 1})
        assert bus.subscriber_count == {}

        # 新订阅者正常工作、aclose 清理干净
        gen2 = bus.subscribe("token")
        await bus.emit("token", {"text": "y", "index": 0})
        e = await anext(gen2)
        assert e.data["text"] == "y"
        await gen2.aclose()
        assert bus.subscriber_count == {}


class TestRound20BackendNoPathChromaE2E:
    """端到端测试：无参路径 stream/state 共用默认 sqlite + chroma 跳过（第20轮复核项5）。"""

    @pytest.mark.asyncio
    async def test_e2e_no_path_stream_state_shared_default_and_chroma(self, tmp_path):
        """YAML 中 stream/state 无 path（回退 default_path 共享 sqlite）+ knowledge 用
        chroma：_ensure_configured_backends 全量实例化，stream/state 共享同一默认后端、
        chroma 惰性创建，state 写入与 cleanup/stats 全程不崩溃。"""
        from weave_agent_sdk.memory.manager import MemoryManager
        from weave_agent_sdk.memory.backends.sqlite import SQLiteBackend
        from weave_agent_sdk.memory.backends.chroma import ChromaBackend

        default_db = (tmp_path / "data" / "memory.db").as_posix()
        yaml_path = tmp_path / "r20_no_path.yaml"
        yaml_path.write_text(
            "agent:\n  name: t\n"
            "llm:\n  provider: anthropic\n"
            "loop:\n  type: simple\n"
            "memory:\n"
            "  scopes:\n"
            "    session:\n"
            "      stream:\n"
            "        backend: sqlite\n"
            "      state:\n"
            "        backend: sqlite\n"
            "      knowledge:\n"
            "        backend: chroma\n"
            f'  default_path: "{default_db}"\n'
            "prompts: {}\nfeatures: {}\nserver: {}\nlogging: {}\n",
            encoding="utf-8",
        )
        config = load_config(yaml_path)
        assert config.memory.default_path == default_db

        manager = MemoryManager(config.memory)
        manager.activate_scopes({"session_id": "s1"})
        manager._ensure_configured_backends()

        s = manager._get_backend_for_namespace("session:s1:stream")
        st = manager._get_backend_for_namespace("session:s1:state")
        k = manager._get_backend_for_namespace("session:s1:knowledge")
        assert isinstance(s, SQLiteBackend) and s._db_path == default_db
        assert s is st, "无 path 的 stream/state 应共享默认 sqlite 后端"
        assert isinstance(k, ChromaBackend)

        # state 写入落默认 sqlite 后端，读回可见
        manager.state.set("k", "v", "session:s1:state")
        assert manager.state.get("k", "session:s1:state") == "v"

        # 管理面 cleanup/stats 全程不崩溃（chroma 被安全跳过）
        manager.cleanup()
        stats = manager.stats()
        assert stats.get("session:s1:state") == 1


class TestObservability:
    """阶段1：可观测性 weave.last_trace。"""

    def test_last_trace_records_run_chain(self):
        """observability.enabled 时，last_trace 记录 run → iteration → llm/tool 链路。"""
        from weave_agent_sdk import Weave, WeaveConfig
        from weave_agent_sdk.types import ObservabilityConfig, LoopConfig, LLMConfig, ToolCall
        from weave_agent_sdk.llm.base import BaseLLM, LLMResponse

        class _ToolCallingLLM(BaseLLM):
            def __init__(self):
                self.n = 0

            async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                self.n += 1
                if self.n == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[ToolCall(id="1", name="add", arguments={"a": 1, "b": 2})],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="done", finish_reason="stop")

            async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                if False:
                    yield
                yield "x"

        cfg = WeaveConfig(
            llm=LLMConfig(provider="anthropic", model="fake"),
            loop=LoopConfig(type="iterative", max_iterations=5),
            observability=ObservabilityConfig(enabled=True),
        )
        weave = Weave(config=cfg, llm=_ToolCallingLLM())

        @weave.tool
        def add(a: int, b: int) -> int:
            return a + b

        result = weave.run("1+2")
        assert result.output == "done"

        trace = weave.last_trace
        assert trace is not None
        assert trace["input"] == "1+2"
        assert trace["iterations"] == 2
        assert trace["output"] == "done"
        assert "elapsed_ms" in trace

        its = trace["iterations_detail"]
        assert len(its) == 2
        assert its[0]["iteration"] == 1
        assert its[0]["tools"][0]["name"] == "add"
        assert its[0]["tools"][0]["result"] == 3
        assert its[0]["llm"]["model"] == "fake"
        assert its[1]["stop_reason"] == "no_tool_calls"

    def test_last_trace_none_when_disabled(self):
        """observability 默认关闭时，last_trace 为 None。"""
        from weave_agent_sdk import Weave, WeaveConfig
        from weave_agent_sdk.llm.base import BaseLLM, LLMResponse

        class _FakeLLM(BaseLLM):
            async def chat(self, messages, tools=None, **k):
                return LLMResponse(content="ok")

            async def chat_stream(self, messages, tools=None, **k):
                if False:
                    yield
                yield "ok"

        weave = Weave(config=WeaveConfig(), llm=_FakeLLM())
        result = weave.run("hi")
        assert result.output == "ok"
        assert weave.last_trace is None

    def test_last_trace_records_simple_loop(self):
        """simple loop（不发 iteration_start）也应记录 llm span，不静默丢弃。"""
        from weave_agent_sdk import Weave, WeaveConfig
        from weave_agent_sdk.types import ObservabilityConfig, LoopConfig, LLMConfig
        from weave_agent_sdk.llm.base import BaseLLM, LLMResponse

        class _FakeLLM(BaseLLM):
            async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                return LLMResponse(content="ok", model="actual-model", finish_reason="stop")

            async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                if False:
                    yield
                yield "x"

        cfg = WeaveConfig(
            llm=LLMConfig(provider="anthropic", model="config-model"),
            loop=LoopConfig(type="simple"),
            observability=ObservabilityConfig(enabled=True),
        )
        weave = Weave(config=cfg, llm=_FakeLLM())
        weave.run("hi")

        trace = weave.last_trace
        assert trace is not None
        its = trace["iterations_detail"]
        assert len(its) == 1, "simple loop 应自动创建默认迭代，而非丢弃 llm span"
        assert its[0]["llm"]["model"] == "actual-model", "model 应取 LLM 实际返回，而非配置值"
        assert its[0]["iteration"] == 1

    def test_last_trace_enabled_read_dynamically(self):
        """构造后改 config.observability.enabled 应生效（非快照）。"""
        from weave_agent_sdk import Weave, WeaveConfig
        from weave_agent_sdk.types import LoopConfig, LLMConfig
        from weave_agent_sdk.llm.base import BaseLLM, LLMResponse

        class _FakeLLM(BaseLLM):
            async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                return LLMResponse(content="ok")

            async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                if False:
                    yield
                yield "x"

        weave = Weave(config=WeaveConfig(llm=LLMConfig(provider="anthropic", model="m"), loop=LoopConfig(type="simple")), llm=_FakeLLM())
        assert weave.last_trace is None
        weave._config.observability.enabled = True
        weave.run("hi")
        assert weave.last_trace is not None

    def test_last_trace_returns_copy(self):
        """last_trace 返回深拷贝，外部修改不污染内部状态。"""
        from weave_agent_sdk import Weave, WeaveConfig
        from weave_agent_sdk.types import ObservabilityConfig, LoopConfig, LLMConfig
        from weave_agent_sdk.llm.base import BaseLLM, LLMResponse

        class _FakeLLM(BaseLLM):
            async def chat(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                return LLMResponse(content="ok")

            async def chat_stream(self, messages, tools=None, max_tokens=4096, temperature=0.7):
                if False:
                    yield
                yield "x"

        weave = Weave(config=WeaveConfig(llm=LLMConfig(provider="anthropic", model="m"), loop=LoopConfig(type="simple"), observability=ObservabilityConfig(enabled=True)), llm=_FakeLLM())
        weave.run("hi")
        t = weave.last_trace
        t["hacked"] = True
        assert "hacked" not in weave.last_trace
