"""WebSocket 桥接 — 将 EventBus 事件推送到 WebSocket 客户端。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from weave_agent_sdk.loop.base import _llm_call_in_flight, _tool_call_in_flight
from weave_agent_sdk.types import WeaveEvent


async def ws_agent_stream(websocket: Any, weave: Any) -> None:
    """WebSocket 端点：接收 input 并流式推送 Agent 执行过程。

    协议:
        客户端发送: {"input": "..."}
                    {"input": "...", "scope_hints": {...}, "context": {...}, "tool_filter": [...]}
        服务端推送: {"type": "token",       "data": {"text": ..., "index": ...}}   # 逐 token
                    {"type": "tool_call",   "data": {"name": ..., "arguments": ...}}
                    {"type": "tool_result", "data": {"name": ..., "result": ..., "error": bool}}
                    {"type": "done",        "data": {"output": ..., "elapsed_ms": ..., "iterations": ...}}
                    {"type": "error",       "data": {"message": ..., "exception": ...}}

    事件类型与 schema 遵循 basic.md §5 / public-api.md §1.6 的不可变契约，
    与 EventBus / agent.stream() 保持一致（token / tool_call / tool_result /
    done / error），客户端按上述事件名解析。

    scope_hints / context / tool_filter 为可选字段，与 REST
    POST /agents/{name}/run 支持的能力对齐，透传给 _run_impl。

    空闲超时兜底与 agent.stream() 路径保持一致：若配置了
    loop.stream_timeout，以"自上次事件的时间"计时（事件到达即重置），
    仅当长时间无任何事件（LLM 调用挂起）时超时并 emit error 事件，
    避免 WS 客户端永久等待；持续 emit token 的健康长运行不会被误杀。
    非流式 LLM 调用（含 tools）与 tool 执行期间零事件属正常设计：
    调用/执行在途时（agent._llm_in_flight=True / agent._tool_in_flight=True）
    视为"活动"，空闲超时不触发（review round-14 issue 1 / round-15 issue 2）。
    在途标记有"最长在途时长"上限（loop.llm_timeout，默认 120s）——真正挂起的
    LLM 调用超过该上限后不再视为活动，空闲超时触发取消，客户端得以退出而非
    永久阻塞（review round-15 issue 1）。
    """
    await websocket.accept()

    try:
        data = await websocket.receive_json()
        input_text = data.get("input", "")
        # 与 REST POST /agents/{name}/run 能力对齐：透传 scope_hints / context / tool_filter
        scope_hints = data.get("scope_hints") or None
        context = data.get("context") or None
        tool_filter = data.get("tool_filter") or None

        # 后台启动 Agent 执行，emit 事件到 EventBus。
        # 通过内部 _run_impl 传入 _streaming=True，与 agent.stream() 路径一致，
        # 使 Loop 在流式模式下 emit 真实 token / tool_call / tool_result 事件
        # （否则走非流式 chat()，WS 客户端只能收到 done / error）。
        async def _run_and_emit():
            try:
                result = await weave._run_impl(
                    input_text,
                    scope_hints=scope_hints,
                    context=context,
                    tool_filter=tool_filter,
                    _streaming=True,
                )
                await weave.emit("done", {
                    "output": result.output,
                    "elapsed_ms": result.elapsed_ms,
                    "iterations": result.iterations,
                })
            except Exception as e:
                await weave.emit("error", {
                    "message": str(e),
                    "exception": type(e).__name__,
                })

        # 空闲超时兜底：以"自上次事件的时间"计时，事件到达即重置。
        # 与 agent.stream() 路径保持一致（复用同一配置项 loop.stream_timeout），
        # 仅当长时间无任何事件（LLM 调用挂起）时触发；持续 emit token 的健康
        # 长运行不会被总时长限制误杀。
        stream_timeout = getattr(weave._config.loop, "stream_timeout", None)
        has_timeout = isinstance(stream_timeout, (int, float)) and stream_timeout > 0

        # 先订阅事件（EventBus.subscribe 调用时急切注册队列，emit 立即可见），
        # 再创建后台任务——保证后台任务在首个调度槽内同步失败（如未配置
        # prompts.system）时，error 事件也不会因订阅队列尚未注册而被丢弃，
        # 客户端立即收到 error 而非永久挂起（review round-1 issue 1）。
        subscribe_gen = weave.on(
            "token", "tool_call", "tool_result", "done", "error"
        )

        def _rebuild_subscription():
            """重建事件订阅（weave.on 调用时急切注册，emit 立即可见）。

            asyncio.wait_for 超时会取消 anext(subscribe_gen)，CancelledError 会
            传播进 EventBus.subscribe 的 async generator 并将其终止（PEP 479）：
            已终止的生成器再次 anext 会抛 RuntimeError("async generator raised
            StopAsyncIteration")。因此在途分支继续等待前需重建订阅，保证在途调用
            恢复产出事件后客户端仍能收到（review round-16 idle-timeout drift）。
            """
            return weave.on(
                "token", "tool_call", "tool_result", "done", "error"
            )

        task = asyncio.create_task(_run_and_emit())

        try:
            # 在消费端做空闲超时：每次等待事件以 stream_timeout
            # 为上限，事件到达即重置计时
            try:
                while True:
                    if has_timeout:
                        try:
                            event = await asyncio.wait_for(anext(subscribe_gen), timeout=stream_timeout)
                        except asyncio.TimeoutError:
                            # 空闲超时：先检查是否存在"在途"的非流式 LLM 调用或
                            # tool 执行。两者期间按设计零事件产出，在途操作视为
                            # "活动"，重置空闲计时继续等待，而非当作挂起取消
                            # （与 agent.stream() 一致，review round-14 issue 1 /
                            # round-15 issue 2）。
                            # 注意：在途标记有"最长在途时长"上限（loop.llm_timeout，
                            # 默认 120s）——真正挂起的 LLM 调用超过该上限后不再
                            # 视为活动，此处触发取消，客户端得以退出而非永久阻塞
                            # （review round-15 issue 1）。
                            if _llm_call_in_flight(weave) or _tool_call_in_flight(weave):
                                # wait_for 超时会取消 anext(subscribe_gen)，
                                # CancelledError 传播进 subscribe 的 async
                                # generator 将其终止（PEP 479），已终止的生成器
                                # 再次 anext 会抛 RuntimeError("async generator
                                # raised StopAsyncIteration")。因此在途继续等待
                                # 前重建订阅（weave.on 急切注册，emit 立即可见）。
                                #
                                # 重建顺序：先注册新订阅、再 aclose 旧生成器——
                                # 避免"旧订阅已注销、新订阅未注册"的空窗内 emit
                                # 的事件被丢弃（边界事件丢失，review round-6
                                # issue 4）；显式 aclose 保证退订不依赖 CPython
                                # async generator 终结，否则旧队列在终结前继续
                                # 注册在 EventBus 上，每次 emit 都向这些无消费者
                                # 队列广播、无限累积。
                                old_gen = subscribe_gen
                                subscribe_gen = _rebuild_subscription()
                                try:
                                    await old_gen.aclose()
                                except Exception:
                                    pass
                                continue
                            # 真正空闲（LLM 调用挂起 / 无任何活动）：取消在途运行，
                            # 向客户端推送 error 事件
                            if not task.done():
                                task.cancel()
                            err_event = WeaveEvent(
                                type="error",
                                data={
                                    "message": f"Stream timed out after {stream_timeout}s (no events received)",
                                    "exception": "TimeoutError",
                                },
                                timestamp=time.time(),
                            )
                            await websocket.send_json({
                                "type": err_event.type,
                                "data": err_event.data,
                                "timestamp": err_event.timestamp,
                            })
                            break
                    else:
                        event = await anext(subscribe_gen)
                    await websocket.send_json({
                        "type": event.type,
                        "data": event.data,
                        "timestamp": event.timestamp,
                    })
                    if event.type in ("done", "error"):
                        break
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                try:
                    await subscribe_gen.aclose()
                except Exception:
                    pass
        except Exception as e:
            try:
                await websocket.send_json({"type": "error", "data": {"message": str(e), "exception": type(e).__name__}})
            except Exception:
                pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "data": {"message": str(e), "exception": type(e).__name__}})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
