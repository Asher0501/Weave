"""Weave Agent — 核心编排器。

编排 Loop + Memory + LLM + Tools，提供统一的 run() / arun() 入口。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path, PurePath
from typing import Any

from weave_agent_sdk.config import load_config
from weave_agent_sdk.checkpoint import CheckpointManager
from weave_agent_sdk.event_bus import EventBus
from weave_agent_sdk.llm.factory import create_llm
from weave_agent_sdk.llm.base import BaseLLM
from weave_agent_sdk.loop.base import BaseLoop, _llm_call_in_flight, _tool_call_in_flight
from weave_agent_sdk.loop.factory import LOOP_REGISTRY
from weave_agent_sdk.memory.manager import MemoryManager
from weave_agent_sdk.prompts.prompt_registry import PromptRegistry
from weave_agent_sdk.types import (
    LoopResult,
    MemoryConfig,
    WeaveConfig,
    WeaveEvent,
)

logger = logging.getLogger(__name__)


class Weave:
    """Weave Agent — 非侵入式 Agent 框架的核心入口。

    用法:
        weave = Weave("weave.yaml")

        @weave.tool
        def search_kb(query: str) -> str: ...

        # Sync
        result = weave.run("我应该学什么?")

        # Async
        result = await weave.arun("我应该学什么?")
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        config: WeaveConfig | None = None,
        llm: BaseLLM | None = None,
        loop: BaseLoop | None = None,
    ):
        if config is not None:
            # 程序化构造：直接使用传入的 WeaveConfig 对象（无需落盘 yaml）
            self._config: WeaveConfig = config
            self._config_dir = Path.cwd()
        elif config_path is not None:
            # 从 YAML 文件加载
            self._config: WeaveConfig = load_config(config_path)
            # 配置文件目录，作为 prompts 相对路径的基准（docs/issues/012 路径规范化）
            self._config_dir = Path(config_path).resolve().parent
        else:
            # 零配置：全默认值（provider 自动检测，凭证从环境变量读取）
            self._config = WeaveConfig()
            self._config_dir = Path.cwd()
        self._llm: BaseLLM = llm if llm is not None else create_llm(
            provider=self._config.llm.provider,
            model=self._config.llm.model,
            api_key=self._config.llm.api_key,
            base_url=self._config.llm.base_url,
        )
        self._event_bus = EventBus()
        self._memory = MemoryManager(self._config.memory)
        self._checkpoint = CheckpointManager(self._memory, self._config.checkpoint)
        self._prompts = PromptRegistry(
            base_dir=self._config_dir / "prompts",
            config=self._config,
        )

        # Tool 注册
        self._tools: list[Any] = []
        self._tool_map: dict[str, Any] = {}
        # 工具元信息：name → {"description": str|None, "schema": dict|None}
        # None 表示未显式指定，回退自动推断（fn.__doc__ / 类型注解）
        self._tool_meta: dict[str, dict[str, Any]] = {}

        # Loop 策略
        self._loop: BaseLoop = loop if loop is not None else self._create_loop()

        # System prompt（缓存）
        self._system_prompt: str = ""

        # 运行状态追踪
        self._is_running: bool = False
        self._last_run: float | None = None

        # 流式模式标记：stream() 运行期间为 True，供 Loop 决定是否 emit 事件
        self._streaming: bool = False

        # 并发运行锁：每个事件循环一把，串行化并发调用（防止共享实例状态互相污染）
        self._run_locks: dict[Any, asyncio.Lock] = {}

    # ── 公共 API ──────────────────────────────────────

    def run(self, input: str, scope_hints: dict[str, str] | None = None, context: dict[str, Any] | None = None, tool_filter: list[str] | None = None) -> LoopResult:
        """同步入口。

        注意：
            当调用线程已存在运行中的事件循环（如 Jupyter、FastAPI、异步测试）时，
            无法在同一线程内对该 loop 进行同步阻塞（run_until_complete 必抛
            RuntimeError）。此时请改用异步入口 `await weave.arun(...)`。

        Raises:
            RuntimeError: 当调用线程已存在运行中的事件循环时抛出，引导使用 arun()。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环 —— 安全创建新 loop 执行
            return asyncio.run(self._run_impl(input, scope_hints, context, tool_filter))
        else:
            # 已有运行中的事件循环 —— 不能对其 run_until_complete，否则必然抛错
            raise RuntimeError(
                "Weave.run() cannot be called from within a running event loop. "
                "Use 'await weave.arun(...)' instead, or call run() from a "
                "synchronous (non-async) context."
            )

    async def arun(self, input: str, scope_hints: dict[str, str] | None = None, context: dict[str, Any] | None = None, tool_filter: list[str] | None = None) -> LoopResult:
        """异步入口。"""
        return await self._run_impl(input, scope_hints, context, tool_filter)

    async def stream(self, input: str, scope_hints: dict[str, str] | None = None, context: dict[str, Any] | None = None, tool_filter: list[str] | None = None) -> Any:
        """流式入口：通过事件总线逐 token 产出。

        用法:
            async for event in weave.stream("..."):
                print(event)

        事件类型与 schema 遵循 basic.md §5 / public-api.md §1.6 的不可变契约：
            token       — {"text": str, "index": int}
            tool_call   — {"name": str, "arguments": dict}
            tool_result — {"name": str, "result": Any, "error": bool}
            done        — {"output": str, "elapsed_ms": int, "iterations": int}
            error       — {"message": str, "exception": str}

        参数与 arun() 对齐：支持 scope_hints / context / tool_filter。

        注意:
            - 订阅必须在 _run_and_emit 之前建立（EventBus.subscribe 调用时急切
              注册队列），task 在 async for 之前创建，防止死锁 / 同步失败的错误
              事件被丢弃。
            - Loop 在流式模式下 emit 真实事件（逐 token / tool 执行过程）。
            - 消费者提前退出（break）时，后台任务会被取消，防止任务泄漏。
            - loop.timeout 作为"收到 done/error 后等待后台任务收尾"的宽限时长
              （默认 5.0 秒），不作为整次运行期限。
            - 空闲超时兜底（可选）：若配置了 loop.stream_timeout（默认不启用），
              以"自上次事件的时间"计时——每次等待事件都以 stream_timeout 为上限、
              事件到达即重置。仅当长时间无任何事件（LLM 调用挂起）时才触发，取消
              在途调用并 emit error 事件，消费者得以退出而非永久阻塞。只要事件持续
              流动（token / tool / done），健康的长运行不会被误杀。
            - 非流式 LLM 调用（含 tools）与 tool 执行期间按设计零事件产出：该调用
              / 执行在途时视为"活动"，空闲超时不触发（避免 tool 型流程单次
              >stream_timeout 的健康慢速调用/工具被误判为挂起）。
        """

        # 空闲超时兜底（可选）：LLM 调用挂起（不产生任何事件、subscribe() 的
        # queue.get() 无限等待）时，若配置了 loop.stream_timeout，超时后会取消
        # 在途调用并 emit error 事件，消费者得以退出而非永久阻塞。
        #
        # 语义为"空闲超时"而非"整次运行总时长上限"：
        #   - 每次等待事件都以 stream_timeout 为上限，事件到达即重置计时；
        #   - 一个持续正常 emit token、总时长超过 stream_timeout 的多轮 tool 运行
        #     （iterative 10 轮常 30-60s）不会被无差别取消——只有长时间无任何事件
        #     （LLM 调用挂起）才会触发。
        #   - 非流式 LLM 调用（含 tools）与 tool 执行期间零事件属正常设计：调用/
        #     执行在途时（agent._llm_in_flight=True / agent._tool_in_flight=True）
        #     不视为挂起，空闲超时不触发，防止误杀健康慢速调用/工具
        #     （review round-14 issue 1 / round-15 issue 2）。
        #   - 在途标记不能无限期视为"活动"：若 LLM 调用真正挂起，标记会一直保持，
        #     消费端将无限 continue 而永久阻塞。在途标记设"最长在途时长"上限
        #     （loop.llm_timeout，默认 120s）：超过后视为挂起，允许空闲超时触发
        #     取消（review round-15 issue 1）。
        # stream_timeout 默认 None（不启用）；0/负值/非数值配置同样视为不启用。
        #
        # 注意：空闲超时不能复用 loop.timeout —— 后者（默认 5.0s）仅作为"收到
        # done/error 后等待后台任务收尾"的宽限时长。若将 loop.timeout 用作空闲
        # 等待上限，会误杀正常慢速运行（单次 LLM 调用常 3-10s，iterative 多轮
        # 更久）并取消在途 LLM 调用。
        stream_timeout = getattr(self._config.loop, "stream_timeout", None)
        has_timeout = isinstance(stream_timeout, (int, float)) and stream_timeout > 0
        # loop.timeout 仅用于"收到 done/error 后等待后台任务收尾"的宽限时长
        timeout = self._config.loop.timeout

        async def _run_and_emit():
            try:
                result = await self._run_impl(input, scope_hints, context, tool_filter, _streaming=True)
                await self._event_bus.emit("done", {
                    "output": result.output,
                    "elapsed_ms": result.elapsed_ms,
                    "iterations": result.iterations,
                })
            except Exception as e:
                await self._event_bus.emit("error", {
                    "message": str(e),
                    "exception": type(e).__name__,
                })

        # 1. 先订阅事件（EventBus.subscribe 在调用时急切注册队列，emit 立即可见），
        #    再创建后台任务——保证后台任务在首个调度槽内同步失败（如未配置
        #    prompts.system → _load_system_prompt 抛 ValueError）时，error 事件
        #    也不会因订阅队列尚未注册而被丢弃，消费者立即收到 error 而非永久挂起
        #    （review round-1 issue 1）。
        #    注意：不在生产者上套 asyncio.wait_for 总时长限制——总时长限制会
        #    误杀持续 emit token 的健康长运行。空闲超时由消费端实现（见下）。
        subscribe_gen = self._event_bus.subscribe(
            "token", "tool_call", "tool_result", "done", "error"
        )

        def _rebuild_subscription():
            """重建事件订阅（EventBus.subscribe 调用时急切注册，emit 立即可见）。

            asyncio.wait_for 超时会取消 anext(subscribe_gen)，CancelledError 会
            传播进 EventBus.subscribe 的 async generator 并将其终止（PEP 479）：
            已终止的生成器再次 anext 会抛 RuntimeError("async generator raised
            StopAsyncIteration")。因此在途分支继续等待前需重建订阅，保证在途调用
            恢复产出事件后消费端仍能收到（review round-16 idle-timeout drift）。
            """
            return self._event_bus.subscribe(
                "token", "tool_call", "tool_result", "done", "error"
            )

        # 2. 再创建后台任务（确保事件能被产生）
        _run_task = asyncio.create_task(_run_and_emit())

        try:
            # 3. 消费事件。
            #
            # 空闲超时：每次等待事件都以 stream_timeout 为上限，事件到达即
            # 重置计时（新的 wait_for 覆盖下一次 anext）。这与"整次运行总时长
            # 上限"不同——只要事件持续流动，健康的长运行不会被误杀；仅当长时间
            # 无任何事件（LLM 调用挂起）时才触发，取消在途调用并 emit error。
            while True:
                if has_timeout:
                    try:
                        event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)
                    except asyncio.TimeoutError:
                        # 空闲超时：先检查是否存在"在途"的非流式 LLM 调用或
                        # tool 执行。两者期间按设计零事件产出，若此时触发超时
                        # 会误杀健康慢速调用/工具（单次 LLM 调用常 3-10s，
                        # tool 型流程可能更久）。在途操作视为"活动"，重置空闲
                        # 计时继续等待，而非当作挂起取消
                        # （review round-14 issue 1 / round-15 issue 2）。
                        # 注意：在途标记有"最长在途时长"上限（loop.llm_timeout，
                        # 默认 120s）——真正挂起的 LLM 调用超过该上限后不再视为
                        # 活动，此处会触发取消，消费者得以退出而非永久阻塞
                        # （review round-15 issue 1）。
                        if _llm_call_in_flight(self) or _tool_call_in_flight(self):
                            # wait_for 超时会取消 anext(subscribe_gen)，CancelledError
                            # 传播进 subscribe 的 async generator 将其终止（PEP 479），
                            # 已终止的生成器再次 anext 会抛 RuntimeError("async
                            # generator raised StopAsyncIteration")。因此在途继续
                            # 等待前重建订阅（EventBus.subscribe 急切注册，emit
                            # 立即可见，不丢后续事件）。
                            #
                            # 重建顺序：先注册新订阅、再 aclose 旧生成器——避免
                            # "旧订阅已注销、新订阅未注册"的空窗内 emit 的事件被
                            # 丢弃（边界事件丢失，review round-6 issue 4）；显式
                            # aclose 保证退订不依赖 CPython async generator 终结
                            # （GC/loop 调度），否则旧队列在终结前继续注册在
                            # EventBus 上，每次 emit 都向这些无消费者队列广播、
                            # 无限累积（unbounded 队列）。
                            old_gen = subscribe_gen
                            subscribe_gen = _rebuild_subscription()
                            try:
                                await old_gen.aclose()
                            except Exception:
                                pass
                            continue
                        # 真正空闲（LLM 调用挂起 / 无任何活动）：取消在途运行，
                        # 向消费者 yield 一个 error 事件
                        if not _run_task.done():
                            _run_task.cancel()
                        yield WeaveEvent(
                            type="error",
                            data={
                                "message": f"Stream timed out after {stream_timeout}s (no events received)",
                                "exception": "TimeoutError",
                            },
                            timestamp=time.time(),
                        )
                        break
                else:
                    event = await anext(subscribe_gen)
                yield event
                if event.type in ("done", "error"):
                    break

            # 4. 正常结束后等待后台任务收尾（宽限时长从 loop 配置读取）
            try:
                await asyncio.wait_for(_run_task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        finally:
            # 消费者提前退出（或异常）时取消后台任务，防止泄漏
            if not _run_task.done():
                _run_task.cancel()
            try:
                await _run_task
            except (asyncio.CancelledError, Exception):
                pass
            # 关闭事件订阅，触发 EventBus 退订清理
            try:
                await subscribe_gen.aclose()
            except Exception:
                pass

    # ── Tool 注册 ─────────────────────────────────────

    def _register_tool_internal(
        self,
        fn: Any,
        *,
        name: str | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> None:
        """内部方法：注册 tool 到 _tools / _tool_map / _tool_meta。

        name / description / schema 为可选显式覆盖：未提供时回退
        fn.__name__ / fn.__doc__ / 类型注解自动推断。
        """
        tool_name = name or fn.__name__
        self._tools.append(fn)
        self._tool_map[tool_name] = fn
        # 容错：Weave.__new__ 构造（绕过 __init__）时 _tool_meta 可能未初始化
        meta = self.__dict__.setdefault("_tool_meta", {})
        meta[tool_name] = {
            "description": description,
            "schema": schema,
        }

    def tool(
        self,
        fn: Any = None,
        *,
        name: str | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Any:
        """装饰器：注册 tool 函数。

        支持两种用法：
            @weave.tool
            def f(...): ...

            @weave.tool(name="search_kb", description="搜索知识库")
            def _search(q: str) -> str: ...
        """
        if fn is None:
            def decorator(f: Any) -> Any:
                self._register_tool_internal(
                    f, name=name, description=description, schema=schema
                )
                return f
            return decorator
        self._register_tool_internal(
            fn, name=name, description=description, schema=schema
        )
        return fn

    def register_tool(
        self,
        fn: Any,
        *,
        name: str | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> None:
        """直接注册 tool 函数（等价 @weave.tool，供无装饰器场景）。"""
        self._register_tool_internal(
            fn, name=name, description=description, schema=schema
        )

    # ── 事件总线 ──────────────────────────────────────

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        await self._event_bus.emit(event_type, data)

    def on(self, *event_types: str) -> Any:
        return self._event_bus.subscribe(*event_types)

    # ── Memory 公共 API ───────────────────────────────

    @property
    def memory(self) -> MemoryManager:
        return self._memory

    # ── 内部实现 ──────────────────────────────────────

    async def _run_impl(self, input: str, scope_hints: dict[str, str] | None = None, context: dict[str, Any] | None = None, tool_filter: list[str] | None = None, _streaming: bool = False) -> LoopResult:
        """统一的 async 实现。

        通过"每事件循环一把锁"串行化同一实例上的并发调用（arun / stream / run），
        避免共享实例状态（_is_running / _system_prompt / _tools / active_scopes /
        _memory_writes）被并发修改而互相污染。同步 run() 每次使用全新事件循环，
        使用独立的锁，避免跨事件循环复用锁导致绑定错误。

        例外：常驻 scheduled 循环（loop.type=scheduled 且配置了 schedule）会持续
        运行直到 shutdown。若在共享锁内执行，其整个生命周期都会持有该事件循环的
        锁，饿死同 loop 上的所有其他 arun()/stream()/run() 操作（如 FastAPI 主循环
        中启动 scheduled 后，所有 /agents/*/run 请求会全部挂起）。因此将其排除在
        共享锁之外（与审查建议"排除在共享锁之外"一致）。

        Raises:
            RuntimeError: 常驻 scheduled 已在运行时再次调用 run/arun/stream，
                拒绝重复启动第二个独立无限调度循环。
        """
        # 清理已关闭事件循环的锁条目（含常驻 scheduled 启停场景）：
        # 同步 run() 每次 asyncio.run() 都会创建全新事件循环，若不清理由
        # scheduled 触发周期累积的锁条目，_run_locks 会无限增长（内存泄漏）。
        self._cleanup_closed_run_locks()

        if self._is_continuous_scheduled():
            # 常驻 scheduled 无重入保护：若同一实例已有常驻调度在运行，
            # 拒绝再次启动。否则第二次 arun()/run()/stream() 会再启动一个
            # 独立无限调度循环，互相覆盖 _current_task、竞争同一把锁，
            # 导致 cron 触发被重复执行、共享实例状态被并发改写。
            if getattr(self, "_is_running", False):
                raise RuntimeError(
                    "A resident scheduled loop is already running on this "
                    "Weave instance. Do not call arun()/stream()/run() again "
                    "while it is active."
                )
            return await self._run_impl_inner(input, scope_hints, context, tool_filter, _streaming)

        loop = asyncio.get_running_loop()
        locks = self.__dict__.setdefault("_run_locks", {})
        lock = locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            locks[loop] = lock

        async with lock:
            return await self._run_impl_inner(input, scope_hints, context, tool_filter, _streaming)

    def _cleanup_closed_run_locks(self) -> None:
        """清理已关闭事件循环的锁条目，防止 _run_locks 无限增长。

        同步 run() 每次 asyncio.run() 都会创建全新事件循环，常驻 scheduled
        路径（绕过共享锁）每触发周期也会向 _run_locks 按 loop 添加锁。若
        不清理，_run_locks 会随调用 / 启停次数无限增长（内存泄漏）。已关闭的
        loop 不可能再有活动等待者，安全移除。
        """
        locks = self.__dict__.get("_run_locks")
        if not locks:
            return
        for existing_loop in list(locks):
            is_closed = getattr(existing_loop, "is_closed", None)
            if is_closed is not None and is_closed():
                locks.pop(existing_loop, None)

    def _is_continuous_scheduled(self) -> bool:
        """判断是否配置为常驻 scheduled 循环（持续运行直到 shutdown）。

        常驻 scheduled 循环的 run() 会无限循环等待下一次触发（cron / 间隔 /
        事件驱动），若在共享锁内执行会饿死同事件循环上的所有其他操作。
        因此需要在 _run_impl 中将其排除在共享锁之外。

        返回:
            True 当 loop.type == "scheduled" 且配置了非空 schedule（字符串）。
        """
        try:
            loop_type = self._config.loop.type
            schedule = self._config.loop.schedule
        except AttributeError:
            return False
        return (
            loop_type == "scheduled"
            and isinstance(schedule, str)
            and bool(schedule.strip())
        )

    async def _run_impl_inner(self, input: str, scope_hints: dict[str, str] | None = None, context: dict[str, Any] | None = None, tool_filter: list[str] | None = None, _streaming: bool = False) -> LoopResult:
        """带状态管理的运行主体。

        状态复位（_is_running / _last_run / _streaming）放入 finally，
        保证任何异常路径下都不会把 is_running 永久卡在 True。
        """
        self._is_running = True
        self._streaming = _streaming
        # 清空上次运行的 memory 写入计数（由 after_think 钩子记录）
        self.__dict__.pop("_memory_writes", None)

        try:
            # 0. 非功能：prompt 注入防御（默认关闭；features.prompt_defense: true 时生效）
            if self._config.features.prompt_defense:
                from weave_agent_sdk.features.prompt_defense import sanitize
                input, _detected = sanitize(input)

            # 1. 激活 Memory scope
            self._memory.activate_scopes(scope_hints)

            # 2. 加载 System Prompt
            self._system_prompt = self._load_system_prompt(context)

            # 3. 如果有 tool_filter，临时过滤 tools（按工具 name，含显式 name 覆盖）
            if tool_filter:
                original_tools = self._tools
                original_tool_map = self._tool_map
                self._tool_map = {k: v for k, v in self._tool_map.items() if k in tool_filter}
                self._tools = [t for t in self._tools if t in self._tool_map.values()]

            try:
                result = await self._loop.run(self, input)
            finally:
                if tool_filter:
                    self._tools = original_tools
                    self._tool_map = original_tool_map

            return result
        finally:
            # 无论成功或失败，都复位运行状态，避免 status() 永久报告 running
            self._is_running = False
            self._last_run = time.time()
            self._streaming = False

    def _create_loop(self) -> BaseLoop:
        """根据配置创建对应的 Loop 实例。

        Raises:
            ValueError: 当配置了未知的 loop.type 时抛出，避免静默回退导致用户未察觉配置错误。
        """
        loop_type = self._config.loop.type

        try:
            return LOOP_REGISTRY.create(loop_type)
        except ValueError:
            # 保持向后兼容的报错文案（测试断言了具体格式与 weave.yaml 引导，
            # 见 docs/issues/010）：names() 按注册顺序返回，保证排序稳定。
            supported = ", ".join(LOOP_REGISTRY.names())
            raise ValueError(
                f"Unknown loop type '{loop_type}'. "
                f"Supported loop types: {supported}. "
                f"Please check the 'loop.type' setting in weave.yaml."
            ) from None

    def _load_system_prompt(self, context: dict[str, Any] | None = None) -> str:
        """加载并渲染 system prompt。

        配置路径完整解析（review round-3 issue 5 / round-4 issue 4）：
        - prompts/ 前缀相对路径：以 PromptRegistry 的 base_dir（"prompts"）为
          基准保留子目录相对部分（prompts/custom/system.md → "custom/system"，
          registry 解析为 prompts/custom/system.md）；prompts/system.md →
          "system"（兼容既有行为）。
        - 裸文件名相对路径（无目录分隔符，如 "system" / "system.md"）：先按
          registry 命名 prompt 解析（prompts/system.md）——配置写 "system"
          时应加载 prompts/system.md，而非把 CWD 下同名文件当作真实文件
          （测试路径 prompts.system='system' 触发 registry 直载分支，
          review round-4 issue 4 的裸名边界）。registry 无此命名 prompt 时
          回退按真实文件加载（错误信息仍指向完整路径）。
        - 绝对路径 / 含目录分隔符的非 prompts/ 前缀路径：不能经 registry 的
          name 解析——若回退 PurePath.stem 会丢弃目录部分
          （/abs/.../prompts/system.md → "system"），根目录恰有
          prompts/system.md 时会**静默加载错误文件**。此时直接按配置路径加载
          真实文件，结果与配置一致。
        - 非字符串配置（如测试 MagicMock / 非法值）：不按文件路径解析——
          Path() 对非路径对象会静默构造垃圾路径（MagicMock 被拆成
          "MagicMock/mock.prompts.system/<id>" 多段），随后 load_prompt 抛
          FileNotFoundError，导致 stream() 同步阶段产出 error 事件而非 done
          （TestStreamRaceCondition fixture 依赖 _prompts.get 返回 mock 内容，
          处理 _load_system_prompt 的 MagicMock 兼容性，round-7 test-drift）。
          回退 registry 的 "system" 命名 prompt，与 _prompts.get 契约一致。

        未配置 prompts.system 时，加载包内默认 prompt（weave/prompts/defaults/system.md），
        不再抛错（C1：内置默认 prompt）。
        """
        if not self._config.prompts.system:
            # 未配置 prompts.system → 加载包内默认 prompt。R2 仍满足：prompt 从
            # .md 文件加载，不在代码里硬编码；默认文件随包分发（pyproject.toml
            # package-data 已声明）。
            from weave_agent_sdk.prompts.loader import load_prompt
            default_path = Path(__file__).parent / "prompts" / "defaults" / "system.md"
            return load_prompt(default_path, context, self._config)
        prompt_path = self._config.prompts.system
        # 非字符串配置（如测试 MagicMock / 非法值）回退 registry 命名 prompt：
        # 见 docstring 第三条。注意此分支必须保留 _prompts.get("system", context)
        # 的契约——既有测试依赖它返回 mock 内容（round-7 test-drift 修复）。
        if not isinstance(prompt_path, str):
            return self._prompts.get("system", context)
        try:
            rel = Path(prompt_path).relative_to(Path("prompts"))
            name = str(rel.with_suffix(""))
            return self._prompts.get(name, context)
        except ValueError:
            pass
        # 绝对路径 / 非 prompts/ 前缀路径：
        # 1) 裸文件名相对路径（无目录分隔符，如 "system" / "system.md"）：先按
        #    registry 命名 prompt 解析（prompts/{name}.md）——配置写 "system"
        #    时应加载 prompts/system.md，而非把 CWD 下同名文件当作真实文件
        #    （测试路径：prompts.system='system' 触发 registry 直载分支；
        #    review round-4 issue 4 的裸名边界）。registry 无此命名 prompt
        #    （文件不存在）时再回退按真实文件加载。
        # 2) 绝对路径 / 含目录分隔符的相对路径（config/prompts/system.md）：
        #    不能经 registry 的 name 解析——若回退 PurePath.stem 会丢弃目录
        #    部分（/abs/.../prompts/system.md → "system"），根目录恰有
        #    prompts/system.md 时会静默加载错误文件。此时直接按配置路径加载
        #    真实文件，结果与配置一致（review round-3 issue 5 / round-4
        #    issue 4）。
        from weave_agent_sdk.prompts.loader import is_schema_path, load_prompt, load_prompt_schema
        path = Path(prompt_path)
        if not path.is_absolute() and path.parent == Path("."):
            # 裸文件名（无目录分隔符）：registry 命名 prompt 优先
            name = path.stem if path.suffix else path.name
            try:
                return self._prompts.get(name, context)
            except FileNotFoundError:
                # registry 无此命名 prompt：回退按真实文件加载（错误信息指向
                # 完整路径，语义与绝对路径直载一致）
                pass
        if not path.is_absolute():
            # 相对路径：相对配置文件目录解析（docs/issues/012 路径规范化），
            # 而非 CWD——`pip install` 后从任意目录运行，prompt 落点可预期。
            # 单元测试可能用 Weave.__new__ 绕过 __init__（无 _config_dir），
            # 此时回退 CWD 以保持兼容。
            base = getattr(self, "_config_dir", None) or Path.cwd()
            path = base / path
        # 显式路径指向 .schema.yaml/.yml/.json：走 schema 渲染而非纯文本插值，
        # 否则 YAML 骨架会整段漏进 prompt（docs/issues/005）
        if is_schema_path(str(path)):
            return load_prompt_schema(path, context, self._config)
        return load_prompt(path, context, self._config)

    # ── 状态回滚 ──────────────────────────────────────

    def checkpoint(self) -> str:
        """手动打一个状态快照，返回 checkpoint_id。

        需在 checkpoint.enabled 之外显式调用；返回的 id 可用于 rollback()。
        """
        return self._run_checkpoint_coro(self._checkpoint.checkpoint())

    def checkpoints(self) -> list[dict]:
        """列出当前 session 的所有快照（按时间升序）。"""
        return self._run_checkpoint_coro(self._checkpoint.checkpoints())

    def rollback(self, checkpoint_id: str | None = None) -> str:
        """回滚到指定快照（默认最近一个）。

        Args:
            checkpoint_id: 目标快照 id；None 表示最近一个。

        Returns:
            实际回滚到的 checkpoint_id

        Raises:
            RuntimeError: 调用线程已存在运行中的事件循环（改用 arollback）
            ValueError: 无快照或 checkpoint_id 不存在
        """
        return self._run_checkpoint_coro(self._checkpoint.rollback(checkpoint_id))

    async def acheckpoint(self) -> str:
        """异步版 checkpoint()。"""
        return await self._checkpoint.checkpoint()

    async def arollback(self, checkpoint_id: str | None = None) -> str:
        """异步版 rollback()。"""
        return await self._checkpoint.rollback(checkpoint_id)

    @staticmethod
    def _run_checkpoint_coro(coro: Any) -> Any:
        """在无运行中事件循环的前提下同步执行 checkpoint 协程。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        else:
            raise RuntimeError(
                "Weave.checkpoint()/rollback() cannot be called from within a "
                "running event loop. Use 'await weave.acheckpoint()' / "
                "'await weave.arollback()' instead."
            )

    # ── 状态查询 ──────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """返回 Weave Agent 状态。"""
        return {
            "agent_name": self._config.agent.name,
            "loop_type": self._config.loop.type,
            "is_running": self._is_running,
            "last_run": self._last_run,
            "memory_stats": {
                "total_entries": sum(self._memory.stats().values()),
                "by_scope": self._memory.stats(),
            },
        }
