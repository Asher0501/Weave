"""LLMClient —— LLM 交互维度对外的那一个对象。

**一个输入**：`messages`（+ 可选 `tools` / 本次动作参数）
**一个输出**：归一后的 `LLMResponse`

它负责把这四件事做对：可靠性（reliability）· 解码（decode）· 计量（usage）· 上报（observer）。
它**不负责**：循环、上下文、执行、模型选择（见 docs/weave-llm-dimension.md §4 非目标）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from weave.core.envelopes import CallRequest, TypedFailure, classify_exception
from weave.core.errors import WeaveError
from weave.core.interfaces import LLMProvider
from weave.core.types import (
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    ToolSchema,
    payload_content,
)
from weave.llm.decode import AutoDecoder, DecodeResult, decode_tool_calls
from weave.llm.reliability import ReliabilityPolicy, emit_event, run_with_reliability
from weave.llm.streaming import StreamAccumulator
from weave.llm.usage import merge_usage, normalize_usage

__all__ = ["LLMClient", "LLMCallError"]


class LLMCallError(WeaveError):
    """一次动作彻底失败（重放耗尽 / 结构损坏到无法继续）。

    异常携带 `failure: TypedFailure`——应用既能 `except` 也能拿到分类细节。
    """

    def __init__(self, failure: TypedFailure):
        super().__init__(f"[{failure.kind}] {failure.message}")
        self.failure = failure


class LLMClient:
    """一次 LLM 交互（可并发使用；配置只在构造器与实例属性）。

    对象层**不认识任何厂商**：厂商知识全在适配器（provider）里。适配器可选地声明
    `protocol` / `foreign_block_types` / `block_shape_hint` 三个类属性，对象层据此
    在**发请求之前**做一次"错家形状"预检（见 `_check_vendor_shape`）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        policy: ReliabilityPolicy | None = None,
        observer: Callable[[str, dict[str, Any]], Any] | None = None,
        decoder: AutoDecoder | None = None,
        strict_decode: bool = True,
        default_opts: dict[str, Any] | None = None,
        shape_check: bool = True,
    ) -> None:
        self.provider = provider
        self.policy = policy or ReliabilityPolicy()
        self.observer = observer
        self.decoder = decoder or AutoDecoder()
        self.strict_decode = strict_decode
        self.default_opts = dict(default_opts or {})
        self.shape_check = shape_check
        #: 对象级累计用量（H3；应用可读，不参与语义）
        self.usage: dict[str, int] = {}
        self.calls = 0
        self.failures = 0
        self._closed = False

    # ── 对外：一次动作 ───────────────────────────────

    async def call(
        self,
        messages: Sequence[Message] | Message,
        tools: Sequence[ToolSchema] | None = None,
        **opts: Any,
    ) -> LLMResponse:
        """一次非流式动作：成功返回归一后的响应；彻底失败抛 `LLMCallError`。"""
        request = self._request(messages, tools, opts)
        await self._notify("llm.request", {
            "model": getattr(self.provider, "model", ""),
            "messages": len(request.messages),
            "tools": [schema.name for schema in request.schemas],
            "opts": dict(request.opts),
        })
        attempt = await run_with_reliability(
            lambda: self.provider.complete(request),
            self.policy,
            origin=f"provider:{type(self.provider).__name__}",
            on_event=self.observer,
        )
        if not attempt.ok:
            self.failures += 1
            raise LLMCallError(attempt.failure)
        return await self._finalize(attempt.value, request, attempt)

    async def call_streaming(
        self,
        messages: Sequence[Message] | Message,
        tools: Sequence[ToolSchema] | None = None,
        *,
        on_chunk: Callable[[StreamChunk], Any] | None = None,
        **opts: Any,
    ) -> LLMResponse:
        """流式执行、内部聚合，仍然"一个输入一个输出"。

        - 建立流失败 → 按可靠性策略重放（F6 的"开头失败"）
        - 流**中途**失败 → **不重放**（重放会重复内容），保留已产出部分并标记 failure
        - `on_chunk` 用于界面逐字呈现（可以是同步或异步函数）
        """
        request = self._request(messages, tools, opts)
        await self._notify("llm.request", {
            "model": getattr(self.provider, "model", ""),
            "messages": len(request.messages),
            "tools": [schema.name for schema in request.schemas],
            "stream": True,
        })
        accumulator = StreamAccumulator()
        attempt = await run_with_reliability(
            lambda: self._open_stream(request),
            self.policy,
            origin=f"provider:{type(self.provider).__name__}",
            on_event=self.observer,
        )
        if not attempt.ok:
            self.failures += 1
            raise LLMCallError(attempt.failure)

        iterator, first = attempt.value
        accumulator.feed(first)
        await self._call_on_chunk(on_chunk, first)
        try:
            async for chunk in iterator:
                accumulator.feed(chunk)
                await self._call_on_chunk(on_chunk, chunk)
        except Exception as exc:            # noqa: BLE001 - 中途失败：保留部分成果
            failure = classify_exception(exc, origin=f"provider:{type(self.provider).__name__}")
            self.failures += 1
            await self._notify("llm.failure", {
                "kind": failure.kind, "message": failure.message, "mid_stream": True,
            })
            partial = accumulator.snapshot()
            if partial.content or partial.tool_calls:
                partial.failure = failure
                partial.finish_reason = "error"
                partial.attempts = attempt.attempts
                partial.elapsed_ms = attempt.elapsed_ms
                return partial
            raise LLMCallError(failure)

        response = accumulator.result()
        response.attempts = attempt.attempts
        response.elapsed_ms = attempt.elapsed_ms
        await self._notify("llm.stream_end", {
            "usage": response.usage, "finish_reason": response.finish_reason,
        })
        return await self._finalize(response, request, attempt, already_decoded=False)

    async def stream(
        self,
        messages: Sequence[Message] | Message,
        tools: Sequence[ToolSchema] | None = None,
        **opts: Any,
    ) -> AsyncIterator[StreamChunk]:
        """逐块产出**归一后的增量**（给需要边到边渲染的界面）。

        注意：本方法只负责"把 chunk 交出去"，**不做聚合**。
        需要最终结构化结果（含 tool_calls）请用 `call_streaming()`。
        """
        request = self._request(messages, tools, opts)
        attempt = await run_with_reliability(
            lambda: self._open_stream(request),
            self.policy,
            origin=f"provider:{type(self.provider).__name__}",
            on_event=self.observer,
        )
        if not attempt.ok:
            self.failures += 1
            raise LLMCallError(attempt.failure)
        iterator, first = attempt.value
        yield first
        async for chunk in iterator:
            yield chunk

    async def aclose(self) -> None:
        """释放底层资源（**可重复调用**：只有第一次真正关）。"""
        if self._closed:
            return
        self._closed = True
        for name in ("aclose", "close"):
            closer = getattr(self.provider, name, None)
            if closer is None:
                continue
            result = closer()
            if hasattr(result, "__await__"):
                await result
            return

    # ── 内部 ─────────────────────────────────────────

    async def _open_stream(self, request: CallRequest):
        """打开流并取到第一个 chunk（只有这一步参与重放）。"""
        iterator = self.provider.stream(request).__aiter__()
        first = await iterator.__anext__()
        return iterator, first

    @staticmethod
    async def _call_on_chunk(
        on_chunk: Callable[[StreamChunk], Any] | None, chunk: StreamChunk
    ) -> None:
        if on_chunk is None:
            return
        try:
            result = on_chunk(chunk)
            if hasattr(result, "__await__"):
                await result
        except Exception:            # noqa: BLE001 - 界面回调不得影响主流程
            pass

    async def _finalize(
        self,
        response: LLMResponse,
        request: CallRequest,
        attempt: Any,
        *,
        already_decoded: bool = False,
    ) -> LLMResponse:
        """统一收尾：计量归一 → 累计 → 解码归一 → 上报（H1–H3, G1–G6, I2/I3）。"""
        response.attempts = attempt.attempts
        response.elapsed_ms = attempt.elapsed_ms
        response.usage = normalize_usage(response.usage)
        self.usage = merge_usage(self.usage, response.usage)
        self.calls += 1

        result: DecodeResult = decode_tool_calls(response, request.schemas, decoder=self.decoder)
        if result.failure is not None and self.strict_decode:
            self.failures += 1
            raise LLMCallError(result.failure)
        if result.failure is not None:
            # 宽松模式：丢弃解不出来的调用并留痕（应用显式选择了"跳过"）
            response.tool_calls = None
            response.raw = {**(response.raw or {}),
                            "decode_error": result.failure.kind,
                            "decode_message": result.failure.message}
        if result.invocations:
            response.tool_calls = [
                ToolCall(
                    id=invocation.id or f"call_{index}",
                    name=invocation.name,
                    arguments=invocation.arguments,
                )
                for index, invocation in enumerate(result.invocations)
            ]
            if result.used:
                response.raw = {**(response.raw or {}), "decoder": result.used}

        await self._notify("llm.response", {
            "model": response.model,
            "attempts": response.attempts,
            "elapsed_ms": response.elapsed_ms,
            "usage": response.usage,
            "finish_reason": response.finish_reason,
            "tool_calls": [call.name for call in response.tool_calls or []],
            "decoder": (response.raw or {}).get("decoder", ""),
        })
        return response

    def _request(
        self,
        messages: Sequence[Message] | Message,
        tools: Sequence[ToolSchema] | None,
        opts: dict[str, Any],
    ) -> CallRequest:
        if isinstance(messages, Message):
            messages = [messages]
        for message in messages:
            # 只校验、不改写：用法错误（原样块里混进非 dict、原样块与 tool_calls 的歧义、
            # 别家形状的块）必须在进入可靠性重放之前抛出——否则会被 classify_exception
            # 归成可重试的 `server`，白白退避重放 3 次再把编程错误报成厂商故障。
            content = payload_content(message)
            # 只查「厂商原样块」（中立块 Block 由适配层翻译，不是厂商形状）
            if self.shape_check and isinstance(content, list) and content \
                    and isinstance(content[0], dict):
                self._check_vendor_shape(content)
        merged = {**self.default_opts, **opts}
        return CallRequest(
            messages=list(messages),
            schemas=list(tools or []),
            opts=merged,
        )

    def _check_vendor_shape(self, blocks: list[dict[str, Any]]) -> None:
        """错家形状预检：**厂商知识来自适配器，对象层不认识任何厂商**。

        适配器可选声明三个类属性：

        - `protocol`：本适配器的厂商名（只用于报错信息）；
        - `foreign_block_types`：明确属于**别家**的块类型，遇到即拒；
        - `block_shape_hint`：本厂商正确形状的示例（只用于报错信息）。

        两条纪律：

        1. **只拦别家已知形状**，未知块一律放行——否则预检会变成挡厂商新特性的墙，
           而"能接厂商未来新增的块"正是放开原样形态的全部意义；
        2. 可用 `weave.llm(..., shape_check=False)` 整个关掉（确实有网关吃混合形状）。

        注入的 provider 不声明 `foreign_block_types` 时，本预检自动跳过。
        """
        foreign = getattr(self.provider, "foreign_block_types", None)
        if not foreign:
            return
        protocol = getattr(self.provider, "protocol", "") or type(self.provider).__name__
        hint = getattr(self.provider, "block_shape_hint", "")
        for block in blocks:
            kind = block.get("type")
            if kind in foreign:
                raise TypeError(
                    f"Message.content 里出现了别家形状的块 {kind!r}，但当前适配器是 {protocol}"
                    f"（配置来自 weave.llm(protocol=...)）。"
                    + (f"{protocol} 的写法示例：{hint}。" if hint else "")
                    + "换成本厂商的块形状，或把 protocol 改成对应协议；"
                    "若该网关确实接受混合形状，可传 shape_check=False 关掉本检查。"
                )

    async def _notify(self, event: str, data: dict[str, Any]) -> None:
        """上报（I1–I4）：只有一条通道——注入的回调；回调异常不得影响主流程。"""
        await emit_event(self.observer, event, data)
