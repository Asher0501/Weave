"""哑 provider 的协议适配测试（清单 A2/A3/B1–B3/E3/F1/F2/F8 + 与对象的联调）。

全部离线：注入 `FakeTransport`，不联网、不依赖 openai SDK。
重点是把**跨分片**这类只能在真实网络里才会暴露的问题，用注入的分片脚本钉死。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from weave.core.envelopes import CallRequest, TypedFailure
from weave.core.errors import AuthError, ProviderRejectedError, RateLimitError, ServerError
from weave.core.interfaces import LLMProvider
from weave.core.types import Message, ToolCall, ToolSchema
from weave.llm import LLMCallError, llm
from weave.providers.openai_http import (
    OpenAIHTTPProvider,
    build_payload,
    delta_to_chunks,
    error_from_response,
    filter_sampling_params,
    messages_to_payload,
    parse_completion,
)
from weave.providers.sse import SSEBuffer, iter_sse_events
from weave.providers.transport import HTTPResponse, StreamHTTPError, retry_after_from_headers


def run(coro):
    return asyncio.run(coro)


def sse(*payloads: str, done: bool = True, keepalive: bool = True) -> list[str]:
    """拼一段 SSE 文本（返回"分片"列表，默认为每行一片）。"""
    out: list[str] = []
    if keepalive:
        out.append(": ping\n\n")
    for payload in payloads:
        out.append(f"data: {payload}\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return out


def chat_body(*, content: str = "hi", tool_calls: list[dict] | None = None,
              usage: dict | None = None, finish: str = "stop") -> str:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return json.dumps({
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 3, "completion_tokens": 2},
    })


class FakeTransport:
    """脚本化传输层：记录请求，返回预置响应/分片。"""

    def __init__(self, *, response: HTTPResponse | None = None,
                 fragments: list[Any] | None = None) -> None:
        self.response = response
        self.fragments = fragments or []
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def post_json(self, url, *, headers, payload, timeout=None) -> HTTPResponse:
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        assert self.response is not None, "本测试未准备非流式响应"
        return self.response

    def post_sse(self, url, *, headers, payload, timeout=None) -> AsyncIterator[str]:
        self.stream_calls.append({"url": url, "headers": headers, "payload": payload})
        fragments = list(self.fragments)

        async def gen():
            for fragment in fragments:
                if isinstance(fragment, BaseException):
                    raise fragment
                yield fragment

        return gen()


def provider(transport: FakeTransport, **kwargs) -> OpenAIHTTPProvider:
    return OpenAIHTTPProvider(
        model=kwargs.pop("model", "test-model"),
        api_key=kwargs.pop("api_key", "sk-test"),
        transport=transport,
        **kwargs,
    )


# ── A. 请求构造 ─────────────────────────────────────────


def test_a2_assistant_tool_calls_and_tool_message_shapes():
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="问题"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"}),
        ]),
        Message(role="tool", content='{"ok":true}', name="read_file", tool_call_id="c1"),
    ]
    payload = messages_to_payload(messages)
    assert payload[0] == {"role": "system", "content": "sys"}
    assistant = payload[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}
    assert payload[3]["tool_call_id"] == "c1" and payload[3]["name"] == "read_file"


def test_a2_arguments_string_passes_through_unchanged():
    """已经是字符串的 arguments 不重复编码（哑原子不做解释）。"""
    messages = [Message(role="assistant", content="", tool_calls=[
        ToolCall(id="c1", name="t", arguments='{"a":1}'),
    ])]
    entry = messages_to_payload(messages)[0]
    assert entry["tool_calls"][0]["function"]["arguments"] == '{"a":1}'


def test_a3_reasoner_drops_sampling_params():
    payload = {"model": "deepseek-reasoner", "temperature": 0.7, "top_p": 0.9, "max_tokens": 10}
    filtered = filter_sampling_params("deepseek-reasoner", payload)
    assert "temperature" not in filtered and "top_p" not in filtered
    assert filtered["max_tokens"] == 10
    assert filter_sampling_params("deepseek-chat", payload) == payload


def test_a3_build_payload_includes_tools_and_opts():
    request = CallRequest(
        messages=[Message(role="user", content="q")],
        schemas=[ToolSchema(name="read_file", description="读文件", parameters={"type": "object"})],
        opts={"max_tokens": 64, "temperature": 0.2, "tool_choice": "auto"},
    )
    payload = build_payload(model="deepseek-chat", request=request)
    assert payload["model"] == "deepseek-chat"
    assert payload["max_tokens"] == 64 and payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "read_file"


def test_a4_reasoning_is_not_part_of_messages():
    """A4：Message 结构里没有 reasoning 字段 → 推理内容天然不回传。"""
    from weave.core.types import LLMResponse

    response = LLMResponse(content="ok", reasoning="内部推理")
    assert "reasoning" not in repr(messages_to_payload([Message(role="user", content="x")]))
    assert response.reasoning == "内部推理"


# ── B. 认证与端点 ───────────────────────────────────────


def test_b1_api_key_from_argument_then_env():
    transport = FakeTransport(response=HTTPResponse(200, chat_body()))
    explicit = provider(transport).headers_for(stream=False)
    assert explicit["Authorization"] == "Bearer sk-test"

    for name in ("WEAVE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ.pop(name, None)
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    try:
        from_env = OpenAIHTTPProvider(model="m", transport=transport, api_key=None)
        assert from_env.resolve_api_key() == "sk-from-env"
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_b2_base_url_headers_and_url():
    transport = FakeTransport(response=HTTPResponse(200, chat_body()))
    client = provider(transport, base_url="https://gw.internal/v1/", headers={"X-Tenant": "t1"})
    assert client.url == "https://gw.internal/v1/chat/completions"
    headers = client.headers_for(stream=True)
    assert headers["X-Tenant"] == "t1" and headers["Accept"] == "text/event-stream"


def test_b3_api_key_callable_is_resolved_per_request():
    keys = iter(["sk-1", "sk-2"])
    transport = FakeTransport(response=HTTPResponse(200, chat_body()))
    client = OpenAIHTTPProvider(model="m", api_key=lambda: next(keys), transport=transport)
    first = run(client.complete(CallRequest(messages=[Message(role="user", content="a")])))
    second = run(client.complete(CallRequest(messages=[Message(role="user", content="b")])))
    assert first.content == second.content == "hi"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer sk-1"
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer sk-2", "key 应每次请求重新解析"


# ── E. 错误分类（含 D3 的 Retry-After 传递） ─────────────


@pytest.mark.parametrize(("status", "expected"), [
    (429, RateLimitError),
    (500, ServerError),
    (503, ServerError),
    (401, AuthError),
    (403, AuthError),
    (402, ProviderRejectedError),
])
def test_e_error_mapping(status, expected):
    transport = FakeTransport(response=HTTPResponse(
        status, json.dumps({"error": {"message": "厂商说：不行", "type": "bad"}})))
    with pytest.raises(expected) as caught:
        run(provider(transport).complete(CallRequest(messages=[Message(role="user", content="q")])))
    assert "厂商说：不行" in str(caught.value)


def test_e_retry_after_header_is_carried_on_the_error():
    transport = FakeTransport(response=HTTPResponse(
        429, json.dumps({"error": {"message": "slow down"}}), {"Retry-After": "2.5"}))
    with pytest.raises(RateLimitError) as caught:
        run(provider(transport).complete(CallRequest(messages=[Message(role="user", content="q")])))
    assert getattr(caught.value, "retry_after") == 2.5
    assert getattr(caught.value, "detail")["status"] == 429


def test_e_retry_after_accepts_http_date_and_garbage():
    assert retry_after_from_headers({"retry-after": "3"}) == 3.0
    assert retry_after_from_headers({"Retry-After": "soon"}) is None
    assert retry_after_from_headers({}) is None


def test_e_non_json_error_body_still_reports_something():
    transport = FakeTransport(response=HTTPResponse(500, "<html>bad gateway</html>"))
    with pytest.raises(ServerError) as caught:
        run(provider(transport).complete(CallRequest(messages=[Message(role="user", content="q")])))
    assert "bad gateway" in str(caught.value)


# ── 响应解析（原生 tool_calls 原样保留 = D13 边界） ──────


def test_parse_completion_keeps_arguments_raw():
    body = chat_body(content="", finish="tool_calls", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
    }])
    response = parse_completion(body)
    assert response.tool_calls[0].arguments == '{"path":"a.txt"}', "原子不得解码（D13）"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_provider_is_a_dumb_llmprovider():
    transport = FakeTransport(response=HTTPResponse(200, chat_body()))
    client = provider(transport)
    assert isinstance(client, LLMProvider)
    assert not hasattr(client, "retry") and not hasattr(client, "policy")


# ── F. SSE：半行缓冲与事件组装 ──────────────────────────


def test_f2_sse_buffer_handles_split_lines():
    buffer = SSEBuffer()
    assert buffer.feed("data: {\"a\"") == []
    assert buffer.feed(":1}\n\ndata: [DO") == ['data: {"a":1}', ""]
    assert buffer.feed("NE]\n") == ["data: [DONE]"]
    assert buffer.flush() == []


def test_f2_sse_buffer_handles_crlf_and_final_partial_line():
    buffer = SSEBuffer()
    assert buffer.feed("data: x\r\ndata: y") == ["data: x"]
    assert buffer.flush() == ["data: y"]


def test_f1_keepalive_comments_and_done_are_ignored():
    async def main():
        fragments = [": ping\n\n", "data: {\"n\":1}\n\n", ": ping\n", "\n", "data: [DONE]\n\n",
                     "data: {\"n\":2}\n\n"]
        return [event.data async for event in iter_sse_events(_aiter(fragments))]

    assert run(main()) == ['{"n":1}']


def test_f1_multi_line_data_is_joined():
    async def main():
        fragments = ["data: line1\ndata: line2\n\n"]
        return [event.data async for event in iter_sse_events(_aiter(fragments))]

    assert run(main()) == ["line1\nline2"]


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


# ── F. 流式端到端（provider → 对象） ────────────────────


def stream_payloads() -> list[str]:
    return [
        json.dumps({"model": "test-model", "choices": [{"index": 0, "delta": {"content": "结"}}]}),
        json.dumps({"model": "test-model", "choices": [{"index": 0, "delta": {"content": "果"}}]}),
        json.dumps({"model": "test-model", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_", "arguments": ""}}]}}]}),
        json.dumps({"model": "test-model", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '{"path"'}}]}}]}),
        json.dumps({"model": "test-model", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "function": {"arguments": ': "a.txt"}'}}]}}]}),
        json.dumps({"model": "test-model", "choices": [{"index": 0, "delta": {},
                                                        "finish_reason": "tool_calls"}]}),
        json.dumps({"model": "test-model", "choices": [],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 4}}),
    ]


def test_f5_provider_delta_mapping():
    chunks = delta_to_chunks(json.loads(stream_payloads()[2]))
    assert chunks[0].kind == "tool_call_delta"
    assert chunks[0].index == 0 and chunks[0].tool_call_id == "c1"
    assert chunks[0].tool_call_name == "read_"
    usage_chunk = delta_to_chunks(json.loads(stream_payloads()[6]))
    assert usage_chunk[0].kind == "usage" and usage_chunk[0].usage["prompt_tokens"] == 9


def test_f_integration_streaming_through_object():
    """F1+F2+F3+F4+F5+F8 一起验证：分片被切开也要拼对。"""
    fragments = sse(*stream_payloads())
    # 人为把分片切得更碎（含切开一行、切开多字节字符的边界情况留给 transport 层）
    chopped: list[str] = []
    for fragment in fragments:
        mid = max(1, len(fragment) // 2)
        chopped.extend([fragment[:mid], fragment[mid:]])
    transport = FakeTransport(fragments=chopped)
    client = llm(provider(transport), retry=0)

    response = run(client.call_streaming([Message(role="user", content="q")],
                                         tools=[ToolSchema(name="read_file")]))
    assert response.content == "结果"
    assert response.tool_calls == [ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})]
    assert response.usage == {"input": 9, "output": 4, "total": 13}
    assert response.finish_reason == "tool_calls"
    assert transport.stream_calls[0]["payload"]["stream"] is True
    assert transport.stream_calls[0]["payload"]["stream_options"] == {"include_usage": True}


def test_f_integration_non_streaming_through_object():
    body = chat_body(content="回答", tool_calls=[{
        "id": "c9", "type": "function",
        "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
    }], finish="tool_calls")
    transport = FakeTransport(response=HTTPResponse(200, body))
    response = run(llm(provider(transport), retry=0).call(
        [Message(role="user", content="q")], tools=[ToolSchema(name="add")]))
    assert response.content == "回答"
    assert response.tool_calls == [ToolCall(id="c9", name="add", arguments={"a": 1, "b": 2})]
    assert response.usage == {"input": 3, "output": 2, "total": 5}
    assert transport.calls[0]["url"].endswith("/chat/completions")
    assert transport.calls[0]["payload"]["tools"][0]["function"]["name"] == "add"


def test_f_stream_rejection_mid_stream_is_typed():
    """流建立后被拒（401）→ 对象层看到的是 auth 失败，而不是裸异常。"""
    transport = FakeTransport(fragments=[StreamHTTPError(
        401, json.dumps({"error": {"message": "bad key"}}), {"Retry-After": "1"})])
    with pytest.raises(LLMCallError) as caught:
        run(llm(provider(transport), retry=0).call_streaming([Message(role="user", content="q")]))
    failure: TypedFailure = caught.value.failure
    assert failure.kind == "auth" and "bad key" in failure.message


def test_f_usage_only_chunk_is_tolerated():
    transport = FakeTransport(fragments=sse(json.dumps({
        "model": "m", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})))
    response = run(llm(provider(transport), retry=0).call_streaming(
        [Message(role="user", content="q")]))
    assert response.usage == {"input": 1, "output": 1, "total": 2}
    assert response.content == ""


# ── 真实重试链路：Retry-After 从 HTTP 头一路传到对象层 ────


def test_retry_after_flows_from_http_header_to_object_backoff():
    """D3 全链路：HTTP 429 + Retry-After 头 → 错误 → 对象按它等待而不是按自己的退避。"""
    class TwoStep(FakeTransport):
        def __init__(self) -> None:
            super().__init__(response=HTTPResponse(
                429, json.dumps({"error": {"message": "rate"}}), {"Retry-After": "0.02"}))
            self.count = 0

        async def post_json(self, url, *, headers, payload, timeout=None):
            self.count += 1
            if self.count == 1:
                return await super().post_json(url, headers=headers, payload=payload, timeout=timeout)
            return HTTPResponse(200, chat_body(content="第二次成功"))

    transport = TwoStep()
    client = llm(provider(transport), retry=2, backoff=5.0)   # 自有退避 5s
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        response = loop.run_until_complete(
            client.call([Message(role="user", content="q")]))
        elapsed = loop.time() - started
    finally:
        loop.close()
    assert response.content == "第二次成功" and response.attempts == 2
    assert elapsed < 1.0, f"应按 Retry-After(0.02s) 等，而不是退避 5s（实际 {elapsed:.2f}s）"
