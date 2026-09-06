"""IterativeLoop — 多轮 Tool 调用循环。

LLM 可反复调用 Tool 直到满足停止条件。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import types as _py_types
from typing import Any, Union, get_args, get_origin

from weave_agent_sdk.loop.base import (
    BaseLoop,
    call_llm,
    format_memory_context,
    persist_stream_entry,
    persist_user_message,
    _emit_event,
    _is_streaming,
    _maybe_checkpoint,
)
from weave_agent_sdk.types import LoopResult, Message, ToolCall, ToolResult as ToolResultType

logger = logging.getLogger(__name__)

# 上下文窗口上限：messages 超过该条数时裁剪最旧的对话消息。
# 防止长迭代（多轮 tool 调用）下上下文线性膨胀，超出模型上下文窗口
# 导致后段迭代 token 溢出 / 生成退化。
_MAX_CONTEXT_MESSAGES = 20


class IterativeLoop(BaseLoop):
    """Agent 持续迭代，调用 Tool 直到满足停止条件。

    停止条件（任一满足即停）:
    - max_iterations 硬上限
    - tool_call name="finish" — Agent 主动结束
    - no_tool_calls — LLM 不再调工具
    - text_pattern — 输出包含特定标记
    """

    async def run(self, agent: Any, user_input: str) -> LoopResult:
        t_start = time.perf_counter()
        await self.on_start(agent, user_input)

        config = agent._config.loop
        max_iter = config.max_iterations
        stop_conditions = config.stop_conditions or []

        # 消息窗口上限：默认回退模块级 _MAX_CONTEXT_MESSAGES（20）。若配置了
        # loop.messages_window（正整数）则以配置值为准——与 memory_context_limit
        # 同属"上下文预算"旋钮，可在 weave.yaml 配置而无需改代码（review round-2
        # issue 4）。此处局部遮蔽模块常量，使下方裁剪逻辑的 _MAX_CONTEXT_MESSAGES
        # 引用统一使用配置值（同时保持既有源码断言不变）。
        # 窗口下限为 3：小于 3 时裁剪逻辑退化（窗口-2 为 0/负，切片失效使
        # messages 每轮无界增长，恰与窗口保护初衷相反），回退默认 20
        # （review round-3 issue 2）。
        configured_window = getattr(config, "messages_window", None)
        _MAX_CONTEXT_MESSAGES = (
            configured_window
            if isinstance(configured_window, int) and configured_window >= 3
            else globals()["_MAX_CONTEXT_MESSAGES"]
        )

        # 重注入 / removed_messages 的条数预算：以 memory_context_limit 为上限
        # （默认 20）。非数值 / 非正配置（如测试 Mock）时回退默认 20。
        reinject_limit = getattr(config, "memory_context_limit", 20)
        if not isinstance(reinject_limit, int) or reinject_limit <= 0:
            reinject_limit = 20

        messages: list[Message] = [
            Message(role="system", content=agent._system_prompt),
            Message(role="user", content=user_input),
        ]

        # 跟踪实际写入次数
        # tool_writes: {tool_name: call_count} — 记录每个 tool 被调用的次数
        # stream_writes / state_writes: {namespace: write_count}
        # TODO: stream/state 的写入计数由 after_think 钩子记录到 agent._memory_writes，
        #       本方法在结束时汇总；如需精确追踪可继续在 MemoryManager 层增强。
        tool_writes: dict[str, int] = {}
        stream_writes: dict[str, int] = {}
        state_writes: dict[str, int] = {}
        final_output = ""
        # 记录最后一条非空 assistant 文本：当循环因 max_iterations 耗尽而结束
        # （未触发任何停止条件）时，messages[-1] 可能是 tool 消息（工具结果
        # JSON，非用户可读），此时回退到最后一条非空 assistant 文本作为输出。
        last_assistant_content = ""
        # 本次运行中因窗口裁剪而从 messages 移除的"早期运行消息"（仅含
        # assistant / tool，system+user 恒保留）。before_think 的排除逻辑把
        # 本次运行已写入的全部条目从 memory 注入中剔除——若被裁掉的条目不
        # 补回，长 tool 链的早期工具结果会既不在 messages 也不从 memory 补回，
        # 模型永久丢失该上下文（静默正确性损失）。
        # 有界：只保留最近 reinject_limit 条（重注入也只用最近 limit 条），
        # 防止长 tool 链下随迭代线性累积（每条含大 content 字符串）导致内存
        # 无界增长（review round-1 issue 5）。
        removed_messages: list[Message] = []
        # user 输入是否已持久化到 stream Memory：延迟到首次成功获得 assistant
        # 回复后才提交（见循环内），避免运行被超时/取消打断时遗留"有 user 无
        # assistant"的悬空历史。
        user_persisted = False
        # 循环是否因 max_iterations 耗尽而结束（未触发任何停止条件）：
        # 用于最终输出判断——耗尽时统一标记"未收敛"，避免静默回退到数轮前的
        # 陈旧 assistant 文本或 tool 结果 JSON（对下游具误导性，
        # review round-14 issue 5）。
        exhausted = True
        iteration = 0

        for iteration in range(1, max_iter + 1):
            # 可观测性：iteration span 开始
            _trace(agent, "iteration_start", {"iteration": iteration})
            # 裁剪历史消息：保留 system prompt + 当前 user 原始输入 + 最近
            # _MAX_CONTEXT_MESSAGES-2 条，防止长迭代下 messages 线性膨胀（多轮
            # tool 调用时每轮追加 assistant + 多条 tool 消息）。裁剪只影响注入
            # LLM 的上下文窗口，不影响 Memory 中的持久化历史。
            #
            # 当前 user 输入必须保留：before_think 的排除逻辑只注入"本次运行
            # 之前"的历史（stream[:-run_writes]），不会补回被裁掉的当前 user
            # 输入。若仅保留 system + 最近 N 条，长迭代（约 10 轮以上）下模型
            # 会丢失当前请求上下文，影响输出质量。
            #
            # 工具调用组完整性：裁剪必须以"完整的 tool 调用组"为单位。若窗口起点
            # 落在 tool 消息上（其前置 assistant(tool_calls) 已被裁掉），会产生孤立
            # tool 消息导致 LLM API 拒绝请求（OpenAI: role='tool' 必须跟在 assistant
            # tool_calls 之后；Anthropic: tool_result 引用不存在的 tool_use_id）。
            # 因此先按条数裁剪窗口，再向前跳过窗口起点处不完整调用组的残余 tool 消息。
            if len(messages) > _MAX_CONTEXT_MESSAGES:
                trimmed = [messages[0], messages[1]] + messages[-(_MAX_CONTEXT_MESSAGES - 2):]
                start = len(messages) - (_MAX_CONTEXT_MESSAGES - 2)
                while start < len(messages) and messages[start].role == "tool":
                    start += 1
                if start > len(messages) - (_MAX_CONTEXT_MESSAGES - 2):
                    trimmed = [messages[0], messages[1]] + messages[start:]
                # 记录被裁剪掉的"本次运行"消息（system+user 恒保留，被裁的只
                # 可能是 assistant / tool 消息），供 before_think 之后重新注入
                # memory context（见下方），避免长 tool 链早期工具结果被静默硬截断。
                removed_messages.extend(messages[2:start])
                # 预算裁剪：removed_messages 只增不减，长 tool 链下随迭代线性
                # 累积（每条含大 content 字符串），内存无界增长。重注入只取最近
                # reinject_limit 条，因此只需保留最近 reinject_limit 条，即可在
                # 不改变重注入语义的前提下把内存约束为常数预算
                # （review round-1 issue 5）。
                if len(removed_messages) > reinject_limit:
                    removed_messages = removed_messages[-reinject_limit:]
                messages = trimmed

            # 必须先复位 system prompt，再注入 memory context
            # 避免上一轮有 memory_ctx 而本轮无时，残留旧 context
            messages[0].content = agent._system_prompt
            # 每轮都使用真实 user_input 作为 knowledge 语义搜索查询词，
            # 而非字面量 "continue"（后者会让语义检索退化为对无关词的 LIKE 匹配）
            memory_ctx = await self.before_think(agent, user_input)
            # 重新注入被裁剪掉的本次运行消息：before_think 的排除逻辑
            # （stream[:-run_writes]）把"本次运行已写入的全部条目"都从 memory
            # 注入中剔除——它假定这些内容仍在 messages 里。窗口裁剪后早期条目
            # 已不在 messages，若不补回，长 tool 链的早期工具结果会既不在
            # messages 也不从 memory 补回。这里把被裁掉的条目重新附加到
            # memory_ctx["stream"]，保证模型仍能看到完整工具链上下文。
            if removed_messages:
                ctx_stream = list(memory_ctx.get("stream") or [])
                # 被裁剪条目的重新注入需受预算约束：不限制的话，长 tool 链下
                # system prompt 单调膨胀（每条 tool 结果 JSON 可能很长），部分
                # 抵消 _MAX_CONTEXT_MESSAGES 窗口保护初衷。以 memory_context_limit
                # 为上限，仅重注入最近 limit 条；并保留 assistant 条目的
                # tool_calls 摘要，避免模型看到 tool 结果却不知对应调用参数
                # （review round-14 issue 4）。
                ctx_stream.extend(_format_reinjected_messages(removed_messages, reinject_limit))
                memory_ctx["stream"] = ctx_stream
            if memory_ctx:
                ctx_text = format_memory_context(memory_ctx)
                messages[0].content += "\n\n" + ctx_text
            # 可观测性：memory_inject span（记录注入 prompt 的 memory 原文）
            _trace(agent, "memory_inject", _serialize_memory_ctx(memory_ctx))

            # LLM 调用（流式模式下 emit token 事件）
            tool_schemas = _build_tool_schemas(agent) if agent._tool_map else None
            response = await call_llm(agent, messages, tool_schemas)

            # 延迟提交 user 消息（仅首次成功获得 assistant 回复时落盘一次）：
            # 与 base.after_think 持久化的 assistant 回复互补。若运行在首次
            # LLM 调用前被超时/取消打断（LLM 挂起被 cancel），user 消息不落盘，
            # 避免遗留"有 user 无 assistant"的悬空消息污染后续记忆注入。
            if not user_persisted:
                await persist_user_message(agent, user_input)
                user_persisted = True

            # 记录 assistant message，含 tool_calls 上下文
            messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))
            await self.after_think(agent, response)
            # 仅记录非空 assistant 文本：max_iterations 耗尽（未触发停止条件）
            # 且末轮 assistant 只发 tool_calls 无文本时，回退到"最近一条非空
            # assistant 文本"，避免 output 落到 messages[-1] 的 tool 结果 JSON。
            if response.content:
                last_assistant_content = response.content

            # 检查停止条件（text_pattern 语义停止 + no_tool_calls 默认行为）。
            # 注意：text_pattern 必须先判断——若 LLM 返回了 tool_calls 且输出
            # 命中 text_pattern，应"命中即停"；若放在 or 右侧会被 tool_calls
            # 短路，text_pattern 永不生效。
            if _matches_text_pattern(response.content, stop_conditions) or not response.tool_calls:
                final_output = response.content
                exhausted = False
                _trace(agent, "iteration_end", {"stop_reason": "no_tool_calls"})
                break

            # 执行 Tool 调用
            finished = False
            for tc in response.tool_calls:
                if _is_finish_tool(tc.name, stop_conditions):
                    # finish tool 语义是"结束迭代"，summary 只是附带输出：
                    # 即使 summary 为空字符串也必须终止外层循环——若依赖
                    # `if final_output:` 的空值判断会被短路，导致继续调用
                    # LLM 甚至重复触发 finish（review round-12 issue 4）。
                    final_output = tc.arguments.get("summary") or response.content
                    if not final_output:
                        # summary 与 assistant 文本都为空时，给出明确提示而非
                        # 静默返回空字符串（finish 语义是"结束迭代"，summary
                        # 缺失不应产生空输出，review round-15 issue 5）。
                        final_output = f"(finished by {tc.name} tool without a summary)"
                    finished = True
                    break  # finish tool

                if _is_streaming(agent):
                    await _emit_event(agent, "tool_call", {
                        "name": tc.name, "arguments": tc.arguments,
                    })
                tool_t_start = time.monotonic()
                tool_result = await _execute_tool(agent, tc)
                # 可观测性：tool span
                _trace(agent, "tool", {
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "result": tool_result.result,
                    "error": tool_result.error,
                    "elapsed_ms": int((time.monotonic() - tool_t_start) * 1000),
                })
                if _is_streaming(agent):
                    await _emit_event(agent, "tool_result", {
                        "name": tc.name,
                        "result": tool_result.result,
                        "error": tool_result.error is not None,
                    })
                messages.append(Message(
                    role="tool",
                    content=_format_tool_result(tool_result),
                    name=tc.name,
                    tool_call_id=tc.id,
                ))

                # 将 tool 执行结果持久化到 stream Memory（对话历史的一部分），
                # 保证跨运行记忆上下文包含 tool 轮次
                await persist_stream_entry(agent, {
                    "role": "tool",
                    "content": _format_tool_result(tool_result),
                    "name": tc.name,
                    "tool_call_id": tc.id,
                })

                # 记录到 tool_writes（实际写入计数）
                tool_writes[tc.name] = tool_writes.get(tc.name, 0) + 1

                # 状态回滚：checkpoint.enabled 且 trigger=after_each_tool 时，
                # 每次 tool 执行后自动打快照（见 docs/issues/012）
                await _maybe_checkpoint(agent)

            if finished or final_output:
                exhausted = False
                _trace(agent, "iteration_end", {"stop_reason": "finish"})
                break

        t_end = time.perf_counter()
        if exhausted:
            _trace(agent, "iteration_end", {"stop_reason": "max_iterations"})

        # 汇总 after_think 记录的 stream/state 写入计数（真实写入，非零值存根）
        memory_writes = getattr(agent, "__dict__", {}).get("_memory_writes") or {}
        stream_writes.update(memory_writes.get("stream", {}))
        state_writes.update(memory_writes.get("state", {}))

        memory_updated: dict[str, dict[str, int]] = {}
        if stream_writes:
            memory_updated["stream"] = stream_writes
        if state_writes:
            memory_updated["state"] = state_writes
        if tool_writes:
            # tools 字段结构为 {tool_name: call_count}，非 namespace 粒度
            memory_updated["tools"] = tool_writes

        # 确定最终输出：
        # 1. 循环因 max_iterations 耗尽而结束（未触发任何停止条件）时，统一标记
        #    "已达 max_iterations 未收敛"——即使存在数轮前的非空 assistant 文本，
        #    也不静默回退（那是对下游具误导性的"陈旧文本"，看似该轮输出实为前几轮）；
        # 2. 命中停止条件（no_tool_calls / text_pattern / finish）时使用 final_output
        #    （模型文本，或 finish 携带的 summary）；
        # 3. 极端兜底仍保留：末条为 tool 消息且无任何可用文本时，同样给出明确的
        #    "未收敛"提示，而非输出工具结果 JSON。
        if exhausted:
            output = f"(reached max_iterations={max_iter} without a final response)"
        elif not (final_output or last_assistant_content) and messages[-1].role == "tool":
            output = f"(reached max_iterations={max_iter} without a final response)"
        else:
            output = final_output or last_assistant_content or messages[-1].content

        result = LoopResult(
            output=output,
            elapsed_ms=int((t_end - t_start) * 1000),
            iterations=iteration,
            memory_updated=memory_updated,
        )

        await self.on_end(agent, result)
        return result


# ── Helpers ──────────────────────────────────────────────

def _trace(agent: Any, event_type: str, data: dict[str, Any] | None = None) -> None:
    """记录可观测性 span；agent 未启用 trace 时 no-op（零开销）。

    经 __dict__ 检查 _trace_enabled 以容错 MagicMock / SimpleNamespace 等
    测试替身（getattr 会对 MagicMock 自动创建属性）。
    """
    if not getattr(agent, "__dict__", {}).get("_trace_enabled", False):
        return
    agent._trace(event_type, data)


def _serialize_memory_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
    """把 before_think 返回的 memory context 转成可序列化结构（trace 用）。"""
    result: dict[str, Any] = {}
    if ctx.get("stream"):
        result["stream"] = list(ctx["stream"])
    if ctx.get("state"):
        result["state"] = dict(ctx["state"])
    if ctx.get("knowledge"):
        result["knowledge"] = [
            {"id": r.id, "content": r.content, "score": r.score, "metadata": r.metadata}
            for r in ctx["knowledge"]
        ]
    return result


def _matches_text_pattern(content: str, stop_conditions: list[dict]) -> bool:
    """检查输出是否命中任一 text_pattern 语义停止条件。

    stop_conditions 中 type == "text_pattern" 且带 pattern 的条目，
    使用 re.search 匹配 response.content，命中即停止迭代。
    """
    if not content or not stop_conditions:
        return False
    for cond in stop_conditions:
        if cond.get("type") != "text_pattern":
            continue
        pattern = cond.get("pattern")
        if pattern and re.search(pattern, content):
            return True
    return False


def _build_tool_schemas(agent: Any) -> list[dict[str, Any]]:
    """构建 Tool JSON Schema 列表。

    工具名 / 描述 / schema 优先取显式注册值（_tool_meta），未显式指定时
    回退 fn.__name__ / fn.__doc__ / 类型注解自动推断。遍历 _tool_map（name
    → fn）而非 _tools，使显式 name 覆盖正确反映到 schema 的 name 字段。
    """
    schemas = []
    # 经 __dict__ 读取以容错 MagicMock / SimpleNamespace 等测试替身：
    # getattr(agent, "_tool_meta", {}) 对 MagicMock 会返回自动创建的 MagicMock
    # （truthy），而非默认空 dict。这里确保拿到的要么是真实 dict 要么是空。
    tool_meta = getattr(agent, "__dict__", {}).get("_tool_meta", {}) or {}
    for tool_name, fn in agent._tool_map.items():
        meta = tool_meta.get(tool_name, {})
        # 显式传入完整 schema 时直接使用（仅确保 name 正确），跳过自动推断
        explicit_schema = meta.get("schema")
        if explicit_schema is not None:
            s = dict(explicit_schema)
            s.setdefault("name", tool_name)
            schemas.append(s)
            continue
        # description 显式指定则用，否则回退 docstring
        description = meta.get("description")
        if description is None:
            description = (fn.__doc__ or "").strip()
        # 自动推断 parameters
        sig = inspect.signature(fn)
        params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for name, param in sig.parameters.items():
            if name in ("self", "cls"):
                continue
            param_type = _resolve_param_type(param.annotation)
            params["properties"][name] = {"type": param_type}
            if param.default is inspect.Parameter.empty:
                params["required"].append(name)
        if not params["required"]:
            del params["required"]
        schemas.append({
            "name": tool_name,
            "description": description,
            "parameters": params,
        })
    return schemas


def _resolve_param_type(anno: Any) -> str:
    """将 Python 类型注解映射为 JSON Schema 类型。

    精确匹配 type_map 只覆盖基础类型；对泛型 / 可选注解（list[str]、
    dict[str, Any]、Optional[int]、int | None 等）会退化到默认 "string"，
    LLM 收到错误的参数类型声明，可能传字符串而非数组/对象，且无
    items/properties 子结构（review round-4 issue 3）。这里展开：
    1. 解包 Optional / Union[...]（PEP 604 `int | None` 或 typing.Optional），
       取非 None 分支作为实际类型；
    2. 解包泛型别名（types.GenericAlias），取其 __origin__（list / dict）
       再映射到 array / object。
    """
    type_map = {
        int: "integer", float: "number", bool: "boolean",
        str: "string", list: "array", dict: "object",
    }
    origin = get_origin(anno)
    # 解包 Optional[int] / int | None / Union[int, None]
    if origin is not None and (origin is Union or origin is _py_types.UnionType):
        args = [a for a in get_args(anno) if a is not type(None)]
        if args:
            anno = args[0]
            origin = get_origin(anno)
    # 解包泛型别名 list[str] / dict[str, Any]
    if origin is not None and origin in type_map:
        return type_map[origin]
    if isinstance(anno, _py_types.GenericAlias):
        anno = anno.__origin__
    return type_map.get(anno, "string")


def _format_reinjected_messages(removed_messages: list[Message], limit: int) -> list[dict[str, Any]]:
    """将被裁剪掉的本次运行消息格式化为可重注入的 stream 条目。

    - 仅保留最近 limit 条（预算约束），防止 system prompt 单调膨胀
      （review round-14 issue 4）；
    - assistant 条目在 content 末尾附带 tool_calls 摘要（工具名 + 参数），
      避免模型看到 tool 结果 JSON 却不知对应调用参数。
    """
    reinjected: list[dict[str, Any]] = []
    for m in removed_messages[-limit:]:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            calls = ", ".join(
                f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False, default=str)})"
                for tc in m.tool_calls
            )
            if entry["content"]:
                entry["content"] = entry["content"] + f"\n[tool_calls: {calls}]"
            else:
                entry["content"] = f"[tool_calls: {calls}]"
        reinjected.append(entry)
    return reinjected


async def _execute_tool(agent: Any, tc: ToolCall) -> ToolResultType:
    """执行一个 tool，标准化异常。

    使用 asyncio.wait_for 设置超时保护，防止耗时 tool 阻塞整个 loop。
    超时时间从 loop 配置的 tool_timeout 读取，默认 30 秒。

    tool 执行期间按设计零事件产出（只有执行完成后的 tool_result 事件），
    设置 agent._tool_in_flight 标记，使消费端空闲超时（stream()/WS）将健康
    慢速 tool 视为"活动"而非"挂起"——避免 stream_timeout 配置下、一个健康
    但耗时较长的 tool 执行被误判为挂起而取消整个运行
    （review round-15 issue 2）。

    超时语义说明：异步（async）tool 分支直接 await 协程，wait_for 超时可被
    真正取消；同步 tool 分支经 asyncio.to_thread 在线程池线程中执行，Python
    无法强制终止线程——wait_for 超时只取消 to_thread 包装协程，底层线程仍会
    继续跑到自然结束（可能产生副作用并占用线程池 worker）。这是 Python 线程
    模型的固有限制，超时时记录告警以提升可见性；长耗时 / 有副作用的 tool
    建议实现为 async 函数（review round-2 issue 2）。
    """
    tool_fn = agent._tool_map.get(tc.name)
    if not tool_fn:
        return ToolResultType(
            tool_call_id=tc.id,
            name=tc.name,
            error=f"Unknown tool: {tc.name}",
        )

    timeout = getattr(agent._config.loop, "tool_timeout", 30.0)
    if not isinstance(timeout, (int, float)):
        # 配置了非数值 tool_timeout（字符串 / 测试 Mock 等）时回退默认值，
        # 避免 asyncio.wait_for 收到非法 timeout 导致协程永不 await / 泄漏
        timeout = 30.0
    agent.__dict__["_tool_in_flight"] = True
    try:
        if asyncio.iscoroutinefunction(tool_fn):
            result = await asyncio.wait_for(
                tool_fn(**tc.arguments), timeout=timeout
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(tool_fn, **tc.arguments), timeout=timeout
            )
        return ToolResultType(tool_call_id=tc.id, name=tc.name, result=result)
    except asyncio.TimeoutError:
        if not asyncio.iscoroutinefunction(tool_fn):
            # 同步 tool 运行在 asyncio.to_thread 的线程中：Python 无法强制
            # 终止线程，wait_for 超时只取消 to_thread 包装协程，底层线程仍会
            # 继续执行到自然结束。记录告警提升可见性（review round-2 issue 2）；
            # 反复超时的同步 tool 会在后台累积线程并可能重复副作用。
            logger.warning(
                "Sync tool '%s' timed out after %ss but its thread keeps running "
                "in the background (Python threads cannot be forcibly killed); "
                "prefer async tools for long-running or side-effectful work.",
                tc.name, timeout,
            )
        return ToolResultType(
            tool_call_id=tc.id,
            name=tc.name,
            error=f"TimeoutError: Tool '{tc.name}' timed out after {timeout}s",
        )
    except Exception as e:
        return ToolResultType(
            tool_call_id=tc.id,
            name=tc.name,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        agent.__dict__.pop("_tool_in_flight", None)


def _is_finish_tool(name: str, stop_conditions: list[dict]) -> bool:
    for cond in stop_conditions:
        if cond.get("type") == "tool_call" and cond.get("name") == name:
            return True
    return False


def _format_tool_result(tr: ToolResultType) -> str:
    if tr.error:
        return f"Error: {tr.error}"
    return json.dumps(tr.result, ensure_ascii=False, default=str)
