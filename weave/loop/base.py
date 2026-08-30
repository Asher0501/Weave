"""BaseLoop 抽象接口。"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from weave.llm.base import LLMResponse
from weave.types import LoopResult

logger = logging.getLogger(__name__)


class BaseLoop(ABC):
    """循环策略的抽象接口。

    实现类: SimpleLoop, IterativeLoop, ScheduledLoop
    """

    @abstractmethod
    async def run(
        self,
        agent: Any,           # Weave 实例（避免循环导入）
        user_input: str,
    ) -> LoopResult:
        """执行循环，返回最终结果。"""
        ...

    # ── 生命周期钩子 ──────────────────────────────────────

    async def on_start(self, agent: Any, input: str) -> None:
        """Loop 开始前。"""
        pass

    async def before_think(self, agent: Any, current_input: str) -> dict[str, Any]:
        """每次 LLM 调用前，从 Memory 加载 context。

        Args:
            agent: Weave 实例
            current_input: 当前轮用户输入（用于 KnowledgeMemory 语义搜索）

        Returns:
            dict 直接注入 LLM system prompt 的 memory 段:
            {"stream": [...], "state": {...}, "knowledge": [...]}
        """
        ctx: dict[str, Any] = {}
        try:
            stream_ns = agent._memory.get_namespaces("stream")
            state_ns = agent._memory.get_namespaces("state")
            knowledge_ns = agent._memory.get_namespaces("knowledge")
        except Exception as e:
            logger.warning("before_think: failed to resolve memory namespaces: %s", e)
            return ctx

        if stream_ns:
            try:
                # 注入 system prompt 的 stream 历史条数上限：可通过
                # loop.memory_context_limit 配置（默认 20），防止长历史 / 多 tool
                # 运行后 system prompt 段撑爆模型上下文窗口。非数值 / 非正配置
                # （如测试 Mock）时回退默认 20。
                limit = getattr(agent._config.loop, "memory_context_limit", 20)
                if not isinstance(limit, int) or limit <= 0:
                    limit = 20
                # 排除本次运行已写入的 stream 条目：当前对话已完整存在于
                # messages 列表，若再注入 system prompt 会造成同一轮内容重复
                # （iterative 中随迭代次数线性膨胀）。只注入"本次运行之前"的历史。
                #
                # 按 namespace 分别裁剪：写入计数是 per-namespace 的（每次持久化
                # 对每个 namespace 各 +1）。若跨 namespace 求和后再统一裁剪，多
                # scope 下每个 namespace 都会被多裁掉其他 namespace 的写入条数，
                # 静默丢失本次运行之前的跨运行历史（review round-1 issue 2）。
                writes = (getattr(agent, "__dict__", {}).get("_memory_writes") or {}).get("stream") or {}
                stream = []
                for ns in stream_ns:
                    ns_run_writes = writes.get(ns, 0)
                    ns_entries = await agent._memory.stream.last(limit + ns_run_writes, [ns])
                    if ns_run_writes > 0:
                        ns_entries = ns_entries[:-ns_run_writes] if len(ns_entries) > ns_run_writes else []
                    stream.extend(ns_entries)
            except Exception as e:
                logger.warning("before_think: stream read failed: %s", e)
                stream = []
            if stream:
                ctx["stream"] = stream

        if state_ns:
            try:
                state = await agent._memory.state.get_all(state_ns)
            except Exception as e:
                logger.warning("before_think: state read failed: %s", e)
                state = {}
            if state:
                ctx["state"] = state

        if knowledge_ns:
            try:
                # knowledge 检索注入条数上限：可通过 loop.memory_knowledge_topk
                # 配置（默认 5），与 memory_context_limit / messages_window 同属
                # "上下文预算"旋钮。此前 top_k=5 硬编码在代码中，宿主无法在
                # weave.yaml 调节知识检索注入量（review round-8 issue 3）。
                # 非数值 / 非正配置（如测试 Mock）时回退默认 5。
                top_k = getattr(agent._config.loop, "memory_knowledge_topk", 5)
                if not isinstance(top_k, int) or top_k <= 0:
                    top_k = 5
                knowledge = await agent._memory.knowledge.search(current_input, knowledge_ns, top_k=top_k)
            except Exception as e:
                logger.warning("before_think: knowledge search failed: %s", e)
                knowledge = []
            if knowledge:
                ctx["knowledge"] = knowledge

        return ctx

    async def after_think(self, agent: Any, response: Any) -> None:
        """每次 LLM 调用后，将 assistant response 持久化到 stream Memory。

        写入计数记录到 agent._memory_writes（{access_type: {namespace: count}}），
        供 run() 汇总到 LoopResult.memory_updated。
        """
        await _persist_stream_entry(agent, {"role": "assistant", "content": response.content})

    async def on_end(self, agent: Any, result: LoopResult) -> None:
        """Loop 结束后。"""
        pass


# ── 共享工具函数 ──────────────────────────────────────────


def _is_streaming(agent: Any) -> bool:
    """判断当前是否处于流式模式（agent._streaming）。

    通过实例 __dict__ 读取，避免 MagicMock 自动创建属性导致误判。
    """
    return bool(getattr(agent, "__dict__", {}).get("_streaming", False))


async def _emit_event(agent: Any, event_type: str, data: dict[str, Any]) -> None:
    """向 EventBus 发射事件（仅当事件总线存在时）。"""
    bus = getattr(agent, "__dict__", {}).get("_event_bus")
    if bus is None:
        return
    try:
        await bus.emit(event_type, data)
    except Exception as e:
        logger.warning("Failed to emit %s event: %s", event_type, e)


def _llm_call_in_flight(agent: Any) -> bool:
    """判断当前是否存在"在途"的非流式 LLM 调用。

    非流式调用（含 tools）期间按设计零事件产出，消费端空闲超时
    （stream()/WS）据此将健康慢速调用视为"活动"而非"挂起"——避免
    tool 型流程的单次 >stream_timeout LLM 调用被误判为挂起而取消
    在途调用（review round-14 issue 1）。通过实例 __dict__ 读取，
    避免 MagicMock 自动创建属性导致误判。

    注意：在途标记不能无限期视为"活动"。若 LLM 调用真正挂起（HTTP 无
    响应、永不返回），标记会一直保持 True，消费端空闲超时将无限
    continue 而永不取消在途运行、消费者永久阻塞（review round-15
    issue 1）。因此为在途标记设"最长在途时长"上限：超过该时长仍无任何
    事件返回，视为可能挂起（而非健康慢速调用），允许消费端空闲超时
    触发取消。上限从 loop.llm_timeout 读取（默认 120s）。
    """
    if not bool(getattr(agent, "__dict__", {}).get("_llm_in_flight", False)):
        return False
    since = getattr(agent, "__dict__", {}).get("_llm_in_flight_since", None)
    max_dur = getattr(agent._config.loop, "llm_timeout", 120.0)
    if not isinstance(max_dur, (int, float)) or max_dur <= 0:
        max_dur = 120.0
    if since is not None and (time.monotonic() - since) > max_dur:
        return False
    return True


def _tool_call_in_flight(agent: Any) -> bool:
    """判断当前是否存在"在途"的 tool 执行。

    tool 执行（最长 tool_timeout=30s）期间按设计零事件产出，消费端空闲
    超时（stream()/WS）据此将健康慢速 tool 视为"活动"而非"挂起"——避免
    stream_timeout 配置下、一个健康但耗时较长的 tool 执行被误判为挂起而
    取消整个运行（review round-15 issue 2）。通过实例 __dict__ 读取，
    避免 MagicMock 自动创建属性导致误判。
    """
    return bool(getattr(agent, "__dict__", {}).get("_tool_in_flight", False))


async def _persist_stream_entry(agent: Any, entry: dict[str, Any]) -> None:
    """将一条对话记录（user / assistant / tool）追加到所有 stream namespace。

    同时记录写入计数到 agent._memory_writes["stream"]（真实写入，非虚构零值），
    供 run() 汇总到 LoopResult.memory_updated。
    写入失败不阻塞主流程，记录告警便于排查。
    """
    try:
        namespaces = agent._memory.get_namespaces("stream")
    except Exception as e:
        logger.warning("persist stream entry: failed to resolve stream namespaces: %s", e)
        return
    if not namespaces:
        return
    try:
        for ns in namespaces:
            await agent._memory.stream.append(entry, ns)
        counter = getattr(agent, "__dict__", {}).get("_memory_writes")
        if counter is None:
            counter = {}
            agent.__dict__["_memory_writes"] = counter
        stream_counts = counter.setdefault("stream", {})
        for ns in namespaces:
            stream_counts[ns] = stream_counts.get(ns, 0) + 1
    except Exception as e:
        # 非关键路径：Memory 写入失败不影响主流程，但记录告警便于排查
        logger.warning("persist stream entry: failed to write stream memory: %s", e)


async def persist_user_message(agent: Any, user_input: str) -> None:
    """将 user 输入持久化到 stream Memory，作为对话历史的一部分。

    与 after_think 持久化的 assistant 回复互补，保证跨运行记忆上下文完整
    （包含 user / assistant / tool 轮次）。

    注意：各 Loop 应在"成功获得 assistant 回复之后"再调用本函数（延迟提交），
    避免运行被超时/取消打断（LLM 挂起被 cancel）时遗留"有 user 无 assistant"
    的悬空消息污染后续记忆注入。
    """
    await _persist_stream_entry(agent, {"role": "user", "content": user_input})


async def persist_stream_entry(agent: Any, entry: dict[str, Any]) -> None:
    """将任意对话记录（如 tool 消息）持久化到 stream Memory。

    Args:
        entry: {"role": ..., "content": ..., ...} 形式的消息 dict。
    """
    await _persist_stream_entry(agent, entry)


async def call_llm(agent: Any, messages: list[Any], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
    """调用 LLM；在流式模式下 emit token 事件。

    - 无 tools 且处于流式模式（agent._streaming=True）：使用 chat_stream
      逐 token 产出并 emit，兑现"逐 token 产出"承诺。
    - 其余情况：使用 chat() 获取完整响应（含 tool_calls）；流式模式下
      以单条 token 事件输出完整文本（有 tools 时无法安全使用
      chat_stream，否则会丢失 tool_calls 上下文）。

    事件 schema 遵循 basic.md §5 / public-api.md §1.6：
        token — {"text": str, "index": int}

    非流式调用（含 tools）期间按设计零事件产出：设置 agent._llm_in_flight
    标记（并记录开始时刻，供 _llm_call_in_flight 做"最长在途时长"判断，
    review round-15 issue 1），使消费端空闲超时（stream()/WS）将健康慢速
    调用视为"活动"而非"挂起"——避免 tool 型流程的单次 >stream_timeout
    LLM 调用被误判为挂起而取消在途调用（review round-14 issue 1）。
    在途标记须覆盖到 token 事件 emit 完成之后再清除：若先清除再 emit，
    会留下"无在途标记且 token 尚未入队"的竞态窗口，该窗口内消费端空闲
    超时会误判为挂起而触发 error（review round-16 idle-timeout drift）。
    """
    streaming = _is_streaming(agent)
    max_tokens = agent._config.llm.max_tokens
    temperature = agent._config.llm.temperature

    if streaming and not tools:
        content_parts: list[str] = []
        index = 0
        async for token in agent._llm.chat_stream(
            messages=messages,
            tools=None,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            content_parts.append(token)
            await _emit_event(agent, "token", {"text": token, "index": index})
            index += 1
        return LLMResponse(content="".join(content_parts))

    agent.__dict__["_llm_in_flight"] = True
    agent.__dict__["_llm_in_flight_since"] = time.monotonic()
    try:
        response = await _chat_with_retry(agent, messages, tools, max_tokens, temperature)
        if streaming:
            # 有 tools 时无法安全使用 chat_stream（会丢失 tool_calls），
            # 以单条 token 事件输出完整文本，保证事件契约可用。
            # 在 finally 清除在途标记之前 emit：消费端空闲超时（stream()/WS）
            # 以"在途标记"判定是否继续等待，若先清除再 emit，会留下
            # "无在途标记且 token 尚未入队"的竞态窗口——该窗口内消费端超时
            # 会误判为挂起而触发 error（review round-16 idle-timeout drift）。
            await _emit_event(agent, "token", {"text": response.content, "index": 0})
    finally:
        agent.__dict__.pop("_llm_in_flight", None)
        agent.__dict__.pop("_llm_in_flight_since", None)
    return response


async def _chat_with_retry(
    agent: Any,
    messages: list[Any],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """带重试 + 硬超时的非流式 LLM 调用（非功能：可靠性 / 时效性）。

    接线在 call_llm；重试仅针对瞬态错误（网络 / 限流 / 服务端 5xx），
    认证 / 参数 / 上下文超长等错误不重试。参数由 loop 配置驱动（默认生效，
    宿主一般无需感知），排查问题时见 docs/internal-utilities.md。
    """
    from weave.llm.errors import NetworkError, RateLimitError, ServerError
    from weave.utils.retry import retry
    from weave.utils.timeout import timeout as _weave_timeout

    retry_attempts = getattr(agent._config.loop, "llm_retry_attempts", 3)
    if not isinstance(retry_attempts, int) or retry_attempts < 1:
        retry_attempts = 3
    retry_backoff = getattr(agent._config.loop, "llm_retry_backoff", 2.0)
    if not isinstance(retry_backoff, (int, float)) or retry_backoff <= 0:
        retry_backoff = 2.0
    call_timeout = getattr(agent._config.loop, "llm_call_timeout", 120.0)
    if not isinstance(call_timeout, (int, float)) or call_timeout <= 0:
        call_timeout = 120.0

    # timeout 覆盖整个重试序列的硬上限；retry 处理单次调用的瞬态失败
    async with _weave_timeout(call_timeout):
        return await retry(
            agent._llm.chat,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            max_attempts=retry_attempts,
            backoff=retry_backoff,
            retryable=(NetworkError, RateLimitError, ServerError),
        )


def format_memory_context(ctx: dict[str, Any]) -> str:
    """将 memory context 格式化为 prompt 文本。

    由 SimpleLoop 和 IterativeLoop 共用，避免代码重复。
    """
    import json

    parts = []

    if ctx.get("stream"):
        parts.append("## 历史对话\n" + "\n".join(
            f"{m.get('role', '?')}: {m.get('content', '')}" for m in ctx["stream"]
        ))

    if ctx.get("state"):
        parts.append("## 当前状态\n```json\n" + json.dumps(ctx["state"], ensure_ascii=False, indent=2) + "\n```")

    if ctx.get("knowledge"):
        parts.append("## 相关知识\n" + "\n".join(
            f"- {item.content}" for item in ctx["knowledge"]
        ))

    return "\n\n".join(parts)
