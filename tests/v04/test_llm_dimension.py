"""LLM 交互维度测试（完备清单 docs/weave-llm-dimension.md §3 的机械验收）。

全部离线：用一个可编排的假 provider 注入，不联网、不依赖 openai SDK。
覆盖：请求构造(A) · 认证适配(B) · 超时(C) · 重放与退避(D) · 分类(E) ·
流式(F) · 解码(G) · 计量(H) · 可观测性(I) · 失败通道(J) · 生命周期(K) · 可替换(L)
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import weave
from weave.core.envelopes import CallRequest, TypedFailure
from weave.core.errors import (
    AuthError,
    ParseError,
    ProviderRejectedError,
    RateLimitError,
    ServerError,
)
from weave.core.interfaces import LLMProvider
from weave.core.types import LLMResponse, Message, StreamChunk, ToolCall, ToolSchema
from weave.llm import LLMCallError, LLMClient, llm
from weave.llm.decode import AutoDecoder, decode_tool_calls, has_dsml
from weave.llm.streaming import StreamAccumulator
from weave.llm.usage import normalize_usage
from weave.providers.fake import FakeProvider, FakeTurn
from weave.providers.transport import HTTPResponse


class _RecordingTransport:
    """记下请求并返回一段假响应的传输层（验证 weave.llm(model=...) 的装配细节）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post_json(self, url, *, headers, payload, timeout=None) -> HTTPResponse:
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return HTTPResponse(200, json.dumps({
            "model": "m",
            "choices": [{"message": {"content": "来自假传输层"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }))

    def post_sse(self, url, *, headers, payload, timeout=None):    # pragma: no cover
        raise NotImplementedError


def run(coro):
    return asyncio.run(coro)


def msg(text: str = "hi") -> list[Message]:
    return [Message(role="user", content=text)]


# ── 测试替身：可编排的哑原子 ────────────────────────────


class ScriptedProvider(LLMProvider):
    """按脚本依次返回"响应或异常"，并记录收到的请求。"""

    def __init__(self, outcomes: list[Any], *, model: str = "scripted") -> None:
        self._outcomes = list(outcomes)
        self._model = model
        self.requests: list[CallRequest] = []
        self.closed = 0

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, request: CallRequest) -> LLMResponse:
        self.requests.append(request)
        outcome = self._outcomes.pop(0) if self._outcomes else LLMResponse(content="default")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed += 1


class SlowProvider(LLMProvider):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def complete(self, request: CallRequest) -> LLMResponse:
        await asyncio.sleep(self.delay)
        return LLMResponse(content="late")


class StreamProvider(LLMProvider):
    """按脚本产出 chunk；脚本项可以是异常（模拟中途失败）。"""

    def __init__(self, script: list[Any], *, fail_open: BaseException | None = None) -> None:
        self._script = list(script)
        self._fail_open = fail_open

    def stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        script, fail_open = self._script, self._fail_open

        async def gen():
            if fail_open is not None:
                raise fail_open
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return gen()

    async def complete(self, request: CallRequest) -> LLMResponse:      # pragma: no cover
        raise NotImplementedError


# ── A. 请求构造 ─────────────────────────────────────────


def test_a_request_carries_messages_tools_and_opts():
    provider = ScriptedProvider([LLMResponse(content="ok")])
    client = llm(provider, retry=0, max_tokens=128, temperature=0.1)   # default_opts
    run(client.call(msg("你好"), tools=[ToolSchema(name="read_file")], top_p=0.9))

    sent = provider.requests[0]
    assert sent.messages[0].content == "你好"
    assert [s.name for s in sent.schemas] == ["read_file"]
    assert sent.opts == {"max_tokens": 128, "temperature": 0.1, "top_p": 0.9}


def test_a_reasoning_never_sent_back():
    """A4：推理内容绝不回传——它只出现在返回值里，不进入下一次请求的 messages。"""
    provider = ScriptedProvider([LLMResponse(content="ok", reasoning="secret")])
    client = llm(provider, retry=0)
    response = run(client.call(msg()))
    assert response.reasoning == "secret"
    assert all("secret" not in (m.content or "") for m in provider.requests[0].messages)


# ── B. 装配：默认 provider 与注入 ───────────────────────


def test_b_provider_injection_still_works():
    injected = FakeProvider(turns=[FakeTurn(content="注入的实现")])
    client = llm(injected, retry=0, max_tokens=64)
    response = run(client.call(msg()))
    assert response.content == "注入的实现"      # L3：换实现，对象层零改动


def test_b_model_sugar_builds_default_provider():
    """最常见用法：只给 model，provider 由 weave 自己装配（调用者也就不必知道它）。"""
    client = llm(model="deepseek-chat", transport=_RecordingTransport(), retry=0)
    response = run(client.call(msg("你好")))
    assert response.content == "来自假传输层"
    assert type(client.provider).__name__ == "OpenAIHTTPProvider"   # 默认装配的那一个


def test_b_model_sugar_passes_provider_details_through():
    transport = _RecordingTransport()
    client = llm(model="m1", base_url="https://gw.internal/v1", api_key="sk-x",
                 headers={"X-Tenant": "t1"}, transport=transport, retry=0,
                 timeout=5, http_timeout=3)
    run(client.call(msg()))
    seen = transport.calls[0]
    assert seen["url"] == "https://gw.internal/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-x"
    assert seen["headers"]["X-Tenant"] == "t1"
    assert client.policy.timeout == 5            # 对象层的语义超时
    assert client.provider.timeout == 3          # 传输层安全网


def test_b_ambiguous_or_missing_provider_raises():
    with pytest.raises(TypeError, match="只能二选一"):
        llm(FakeProvider(), model="m")
    with pytest.raises(TypeError, match="需要 model="):
        llm()
    with pytest.raises(TypeError, match="不是 LLMProvider"):
        llm(object())


# ── C. 超时 ─────────────────────────────────────────────


def test_c_timeout_is_classified_and_reported():
    client = llm(SlowProvider(0.2), timeout=0.01, retry=0)
    with pytest.raises(LLMCallError) as caught:
        run(client.call(msg()))
    assert caught.value.failure.kind == "timeout"
    assert caught.value.failure.retryable is True


# ── D. 重放与退避 ───────────────────────────────────────


def test_d_retries_then_succeeds_and_reports_attempts():
    provider = ScriptedProvider([
        ServerError("500"),
        RateLimitError("429"),
        LLMResponse(content="终于成功"),
    ])
    client = llm(provider, retry=3, backoff=0.0, jitter=0.0)
    response = run(client.call(msg()))
    assert response.content == "终于成功"
    assert response.attempts == 3
    assert len(provider.requests) == 3


def test_d_non_retryable_is_not_replayed():
    provider = ScriptedProvider([AuthError("401"), LLMResponse(content="不该到这")])
    client = llm(provider, retry=3, backoff=0.0)
    with pytest.raises(LLMCallError) as caught:
        run(client.call(msg()))
    assert caught.value.failure.kind == "auth"
    assert len(provider.requests) == 1


def test_d_retry_after_is_respected_over_backoff():
    """D3：厂商给了 Retry-After 就听它的（而不是按自己的退避）。"""
    error = RateLimitError("429")
    error.retry_after = 0.02                       # 原子填，本维度读
    provider = ScriptedProvider([error, LLMResponse(content="ok")])
    client = llm(provider, retry=2, backoff=5.0)   # 自有退避 5s
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        response = loop.run_until_complete(client.call(msg()))
        elapsed = loop.time() - started
    finally:
        loop.close()
    assert response.content == "ok"
    assert elapsed < 1.0, f"应按 Retry-After(0.02s) 等，而不是退避 5s（实际 {elapsed:.2f}s）"


def test_d_total_timeout_budget_stops_retrying():
    error = RateLimitError("429")
    error.retry_after = 30.0
    provider = ScriptedProvider([error, LLMResponse(content="不该到这")])
    client = llm(provider, retry=5, total_timeout=1.0)
    with pytest.raises(LLMCallError) as caught:
        run(client.call(msg()))
    assert caught.value.failure.kind == "rate_limit"
    assert len(provider.requests) == 1, "退避会突破总预算时必须放弃重放"


# ── E. 错误分类 ─────────────────────────────────────────


@pytest.mark.parametrize(("error", "kind", "retryable"), [
    (RateLimitError("429"), "rate_limit", True),
    (ServerError("500"), "server", True),
    (AuthError("401"), "auth", False),
    (ProviderRejectedError("402"), "rejected", False),
    (ParseError("bad"), "parse_error", False),
    (ValueError("unknown"), "server", True),
])
def test_e_classification_table(error, kind, retryable):
    client = llm(ScriptedProvider([error]), retry=0)
    with pytest.raises(LLMCallError) as caught:
        run(client.call(msg()))
    failure = caught.value.failure
    assert (failure.kind, failure.retryable) == (kind, retryable)
    assert failure.origin.startswith("provider:")


# ── F. 流式 ─────────────────────────────────────────────


def test_f_streaming_aggregates_content_reasoning_and_tool_calls():
    provider = StreamProvider([
        StreamChunk(kind="reasoning", reasoning="想一下"),
        StreamChunk(kind="token", text="结果"),
        StreamChunk(kind="tool_call_delta", index=0, tool_call_id="c1", tool_call_name="read_"),
        StreamChunk(kind="tool_call_delta", index=0, tool_call_name="file",
                    arguments_delta='{"path"'),
        StreamChunk(kind="tool_call_delta", index=0, arguments_delta=': "a.txt"}'),
        StreamChunk(kind="usage", usage={"prompt_tokens": 7, "completion_tokens": 3}),
        StreamChunk(kind="finish", finish_reason="tool_calls"),
    ])
    seen: list[StreamChunk] = []
    response = run(llm(provider, retry=0).call_streaming(
        msg(), tools=[ToolSchema(name="read_file")], on_chunk=seen.append))

    assert response.content == "结果"
    assert response.reasoning == "想一下"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"input": 7, "output": 3, "total": 10}
    assert response.tool_calls == [ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})]
    assert len(seen) == 7, "on_chunk 应逐块拿到增量"


def test_f_mid_stream_failure_keeps_partial_and_marks_failure():
    provider = StreamProvider([
        StreamChunk(kind="token", text="已经写了一半"),
        ServerError("connection reset"),
    ])
    response = run(llm(provider, retry=3, backoff=0.0).call_streaming(msg()))
    assert response.content == "已经写了一半", "中途失败不得丢掉已产出的内容"
    assert response.finish_reason == "error"
    assert isinstance(response.failure, TypedFailure) and response.failure.kind == "server"


def test_f_open_failure_is_retried():
    provider = StreamProvider([], fail_open=ServerError("500"))
    with pytest.raises(LLMCallError) as caught:
        run(llm(provider, retry=0, backoff=0.0).call_streaming(msg()))
    assert caught.value.failure.kind == "server"


def test_f_cancellation_propagates():
    async def main():
        provider = SlowStreamProvider()
        client = llm(provider, retry=2, backoff=0.0, timeout=None)
        task = asyncio.create_task(client.call_streaming(msg()))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())


class SlowStreamProvider(LLMProvider):
    def stream(self, request):
        async def gen():
            yield StreamChunk(kind="token", text="x")
            await asyncio.sleep(5)
            yield StreamChunk(kind="finish")

        return gen()

    async def complete(self, request):        # pragma: no cover
        raise NotImplementedError


def test_f_chunk_callback_exception_does_not_break():
    provider = StreamProvider([StreamChunk(kind="token", text="ok"), StreamChunk(kind="finish")])

    def boom(_chunk):
        raise RuntimeError("界面回调炸了")

    response = run(llm(provider, retry=0).call_streaming(msg(), on_chunk=boom))
    assert response.content == "ok"


def test_stream_yields_raw_increments():
    provider = StreamProvider([StreamChunk(kind="token", text="a"), StreamChunk(kind="finish")])

    async def collect():
        return [c async for c in llm(provider, retry=0).stream(msg())]

    chunks = run(collect())
    assert [c.text for c in chunks if c.kind == "token"] == ["a"]


# ── G. 解码 ─────────────────────────────────────────────


def test_g_native_dict_and_string_arguments_are_normalized():
    client = llm(ScriptedProvider([
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="t", arguments={"a": 1})]),
    ]), retry=0)
    assert run(client.call(msg())).tool_calls[0].arguments == {"a": 1}

    client2 = llm(ScriptedProvider([
        LLMResponse(content="", tool_calls=[ToolCall(id="c2", name="t", arguments='{"b": 2}')]),
    ]), retry=0)
    assert run(client2.call(msg())).tool_calls[0].arguments == {"b": 2}


def test_g_dsml_text_is_decoded_with_generated_id():
    text = (
        "好的。<｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"search\">\n"
        "<｜DSML｜parameter name=\"query\" string=\"true\">Python</｜DSML｜parameter>\n"
        "<｜DSML｜parameter name=\"limit\" string=\"false\">5</｜DSML｜parameter>\n"
        "</｜DSML｜invoke>\n</｜DSML｜tool_calls>"
    )
    assert has_dsml(text)
    response = run(llm(ScriptedProvider([LLMResponse(content=text)]), retry=0).call(msg()))
    call = response.tool_calls[0]
    assert call.name == "search"
    assert call.arguments == {"query": "Python", "limit": 5}
    assert call.id.startswith("dsml_"), "没给 id 时要生成 id，应用才能配对 tool 消息"
    assert response.raw["decoder"] == "dsml"


def test_g_json_block_is_decoded():
    content = '```json\n{"tool_calls":[{"name":"add","arguments":{"a":1,"b":2}}]}\n```'
    response = run(llm(ScriptedProvider([LLMResponse(content=content)]), retry=0).call(msg()))
    assert response.tool_calls[0].name == "add"
    assert response.raw["decoder"] == "json"


def test_g_no_call_is_not_an_error():
    response = run(llm(ScriptedProvider([LLMResponse(content="就是一段普通回答")]), retry=0).call(msg()))
    assert response.tool_calls is None
    assert decode_tool_calls(response).invocations == []


def test_g_broken_arguments_raise_in_strict_mode():
    provider = ScriptedProvider([LLMResponse(content="", tool_calls=[
        ToolCall(id="c1", name="t", arguments="{not json"),
    ])])
    with pytest.raises(LLMCallError) as caught:
        run(llm(provider, retry=0).call(msg()))
    assert caught.value.failure.kind == "parse_error"


def test_g_broken_arguments_can_be_skipped_in_lenient_mode():
    provider = ScriptedProvider([LLMResponse(content="", tool_calls=[
        ToolCall(id="c1", name="t", arguments="{not json"),
    ])])
    response = run(llm(provider, retry=0, strict_decode=False).call(msg()))
    assert response.tool_calls is None            # 跳过该条，不抛


def test_g_decoders_are_pure():
    response = LLMResponse(content="没有调用")
    before = repr(response)
    AutoDecoder().decode(response)
    assert repr(response) == before


# ── H. 计量 ─────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [
    ({"prompt_tokens": 10, "completion_tokens": 4}, {"input": 10, "output": 4, "total": 14}),
    ({"input_tokens": 3, "output_tokens": 2, "total_tokens": 9}, {"input": 3, "output": 2, "total": 9}),
    ({"prompt_tokens": 1, "completion_tokens": 1,
      "completion_tokens_details": {"reasoning_tokens": 7}}, {"input": 1, "output": 1, "total": 2, "reasoning": 7}),
    ({}, {}),
    (None, {}),
    # ── 真实端点实测会遇到的 prompt cache 计数（Anthropic / OpenAI 兼容两种写法）──
    ({"input_tokens": 91, "output_tokens": 32,
      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1024},
     {"input": 91, "output": 32, "total": 123, "cache_read": 1024}),
    ({"prompt_tokens": 5, "completion_tokens": 2,
      "prompt_tokens_details": {"cached_tokens": 512}},
     {"input": 5, "output": 2, "total": 7, "cache_read": 512}),
    ({"prompt_tokens": 5, "completion_tokens": 2, "prompt_cache_hit_tokens": 256},
     {"input": 5, "output": 2, "total": 7, "cache_read": 256}),
    # 缓存计数为 0 时不塞噪音字段（"没有"和"0"一样处理）
    ({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0},
     {"input": 1, "output": 1, "total": 2}),
])
def test_h_usage_normalization(raw, expected):
    assert normalize_usage(raw) == expected


def test_h_object_accumulates_usage():
    provider = ScriptedProvider([
        LLMResponse(content="a", usage={"prompt_tokens": 5, "completion_tokens": 1}),
        LLMResponse(content="b", usage={"input_tokens": 2, "output_tokens": 3}),
    ])
    client = llm(provider, retry=0)
    run(client.call(msg()))
    run(client.call(msg()))
    assert client.usage == {"input": 7, "output": 4, "total": 11}
    assert client.calls == 2


def test_h_missing_usage_is_tolerated():
    response = run(llm(ScriptedProvider([LLMResponse(content="x")]), retry=0).call(msg()))
    assert response.usage == {}


# ── I. 可观测性 ─────────────────────────────────────────


def test_i_events_and_observer_receive_lifecycle():
    events: list[tuple[str, dict[str, Any]]] = []
    provider = ScriptedProvider([ServerError("500"), LLMResponse(content="ok", usage={"prompt_tokens": 1})])
    client = llm(provider, retry=2, backoff=0.0, observer=lambda e, d: events.append((e, d)))
    run(client.call(msg()))
    names = [name for name, _ in events]
    assert names[0] == "llm.request"
    assert "llm.retry" in names
    assert names[-1] == "llm.response"
    # 只给了 input：不臆造 total（"没有"和"0"是两回事）
    assert events[-1][1]["usage"] == {"input": 1}
    assert events[-1][1]["attempts"] == 2


def test_i_observer_failure_never_breaks_flow():
    def boom(_event, _data):
        raise RuntimeError("观测炸了")

    response = run(llm(ScriptedProvider([LLMResponse(content="ok")]), retry=0, observer=boom).call(msg()))
    assert response.content == "ok"


def test_i_async_observer_supported():
    seen: list[str] = []

    async def observer(event, _data):
        seen.append(event)

    run(llm(ScriptedProvider([LLMResponse(content="ok")]), retry=0, observer=observer).call(msg()))
    assert "llm.request" in seen and "llm.response" in seen




# ── J. 失败通道 ─────────────────────────────────────────


def test_j_error_carries_typed_failure():
    client = llm(ScriptedProvider([AuthError("401 unauthorized")]), retry=0)
    with pytest.raises(LLMCallError) as caught:
        run(client.call(msg()))
    failure = caught.value.failure
    assert failure.kind == "auth" and "401" in failure.message
    assert client.failures == 1
    assert isinstance(caught.value, weave.LLMCallError)


# ── K. 生命周期与并发 ───────────────────────────────────


def test_k_concurrent_calls_are_safe():
    async def main():
        provider = FakeProvider(turns=[
            __import__("weave.providers.fake", fromlist=["FakeTurn"]).FakeTurn(content=f"t{i}")
            for i in range(8)
        ])
        client = llm(provider, retry=0)
        results = await asyncio.gather(*(client.call(msg()) for _ in range(8)))
        assert sorted(r.content for r in results) == sorted(f"t{i}" for i in range(8))
        assert client.calls == 8

    run(main())


def test_k_aclose_is_idempotent():
    provider = ScriptedProvider([LLMResponse(content="ok")])
    client = llm(provider, retry=0)
    run(client.aclose())
    run(client.aclose())
    assert provider.closed == 1


# ── K2. 装配形态（无全局可变状态） ────────────────────────


def test_policy_defaults_and_overrides():
    default = llm(ScriptedProvider([]))
    assert default.policy.retries == 3 and default.policy.timeout == 30.0
    custom = llm(ScriptedProvider([]), retry=0, timeout=None, total_timeout=None)
    assert custom.policy.retries == 0 and custom.policy.timeout is None
    assert default.policy is not custom.policy, "两个对象不共享 policy 实例"


# ── 公开面 ──────────────────────────────────────────────


def test_public_surface_is_one_object():
    assert callable(weave.llm)
    client = llm(ScriptedProvider([]))
    for name in ("call", "call_streaming", "stream", "aclose"):
        assert callable(getattr(client, name))
    assert isinstance(client, LLMClient)
