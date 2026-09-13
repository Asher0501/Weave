"""Anthropic 原生协议的适配测试（离线：注入 transport）。

重点验三处协议差异，它们跟 OpenAI 兼容协议不是"参数不同"而是"形状不同"：
1. system 抽成顶层参数；`tool` 消息 → `user` 里的 `tool_result` 块（且相邻合并）
2. `max_tokens` 必填；认证用 `x-api-key` + `anthropic-version`
3. 流式走事件流（`content_block_delta` / `input_json_delta` / `message_delta`）
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from weave.core.envelopes import CallRequest
from weave.core.errors import AuthError, RateLimitError, ServerError
from weave.core.interfaces import LLMProvider
from weave.core.types import Message, StreamChunk, ToolCall, ToolSchema
from weave.llm import LLMCallError, llm
from weave.providers.anthropic_http import (
    AnthropicHTTPProvider,
    anthropic_event_to_chunks,
    build_anthropic_payload,
    messages_to_anthropic,
    parse_anthropic_message,
    tools_to_anthropic,
)
from weave.providers.transport import HTTPResponse


def run(coro):
    return asyncio.run(coro)


class FakeTransport:
    def __init__(self, *, response: HTTPResponse | None = None,
                 fragments: list[Any] | None = None) -> None:
        self.response = response
        self.fragments = fragments or []
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def post_json(self, url, *, headers, payload, timeout=None) -> HTTPResponse:
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        assert self.response is not None
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


def provider(transport: FakeTransport, **kwargs) -> AnthropicHTTPProvider:
    return AnthropicHTTPProvider(
        model=kwargs.pop("model", "claude-sonnet-4-20250514"),
        api_key=kwargs.pop("api_key", "sk-ant-test"),
        transport=transport,
        **kwargs,
    )


def message_body(*, text: str = "排期完成", tool_calls: list[dict] | None = None,
                 usage: dict | None = None, stop: str = "end_turn") -> str:
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_calls or []:
        blocks.append({"type": "tool_use", **call})
    return json.dumps({
        "id": "msg_1",
        "type": "message",
        "model": "claude-sonnet-4-20250514",
        "content": blocks,
        "stop_reason": stop,
        "usage": usage or {"input_tokens": 11, "output_tokens": 7},
    })


# ── 1. 请求形状（system / tool_result / max_tokens / 认证） ──


def test_system_becomes_top_level_parameter():
    system, messages = messages_to_anthropic([
        Message(role="system", content="你是排期助手"),
        Message(role="system", content="输出 JSON"),
        Message(role="user", content="帮我排期"),
    ])
    assert system == "你是排期助手\n\n输出 JSON"
    assert messages == [{"role": "user", "content": "帮我排期"}]


def test_tool_result_becomes_user_block_and_merges():
    _, messages = messages_to_anthropic([
        Message(role="user", content="查一下"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", name="list_projects", arguments={}),
            ToolCall(id="t2", name="get_graph", arguments={"id": "p1"}),
        ]),
        Message(role="tool", content='{"ok":1}', name="list_projects", tool_call_id="t1"),
        Message(role="tool", content='{"ok":2}', name="get_graph", tool_call_id="t2"),
    ])
    assistant = messages[1]
    assert [b["type"] for b in assistant["content"]] == ["tool_use", "tool_use"]
    assert assistant["content"][1]["input"] == {"id": "p1"}, "tool_use.input 必须是对象"
    assert len(messages) == 3, "两条 tool 结果必须合并成同一条 user 消息"
    results = messages[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"]


def test_adjacent_same_role_messages_are_merged():
    _, messages = messages_to_anthropic([
        Message(role="user", content="第一句"),
        Message(role="user", content="第二句"),
    ])
    assert len(messages) == 1
    assert [b["text"] for b in messages[0]["content"]] == ["第一句", "第二句"]


def test_max_tokens_is_required_and_filled():
    request = CallRequest(messages=[Message(role="user", content="q")])
    payload = build_anthropic_payload(model="m", request=request)
    assert payload["max_tokens"] == 4096, "Anthropic 的 max_tokens 必填，没给要用默认"
    custom = build_anthropic_payload(
        model="m", request=CallRequest(messages=[Message(role="user", content="q")],
                                       opts={"max_tokens": 128}))
    assert custom["max_tokens"] == 128


def test_openai_only_params_are_filtered_and_stop_mapped():
    request = CallRequest(
        messages=[Message(role="user", content="q")],
        opts={
            "temperature": 0.7, "frequency_penalty": 0.5, "presence_penalty": 0.5,
            "logprobs": True, "stream_options": {"include_usage": True},
            "stop": ["###"], "tool_choice": "required", "seed": 1,
        },
    )
    payload = build_anthropic_payload(model="m", request=request)
    assert payload["temperature"] == 0.7
    assert payload["stop_sequences"] == ["###"]
    assert payload["tool_choice"] == {"type": "any"}
    for gone in ("frequency_penalty", "presence_penalty", "logprobs",
                 "stream_options", "seed", "stop", "tool_choice"):
        if gone == "tool_choice":
            continue
        assert gone not in payload, f"{gone} 是 OpenAI 专有参数，不该发给 Anthropic"


def test_tools_use_input_schema_and_headers_use_x_api_key():
    transport = FakeTransport(response=HTTPResponse(200, message_body()))
    client = provider(transport)
    payload = build_anthropic_payload(
        model="m",
        request=CallRequest(messages=[Message(role="user", content="q")],
                            schemas=[ToolSchema(name="list_projects", description="列项目",
                                                parameters={"type": "object", "properties": {}})]),
    )
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    headers = client.headers_for(stream=False)
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"]
    assert "authorization" not in {k.lower() for k in headers}, "Anthropic 不用 Bearer"
    assert client.url.endswith("/v1/messages")


# ── 2. 响应解析 ─────────────────────────────────────────


def test_parse_message_extracts_text_thinking_and_tool_use():
    body = json.dumps({
        "id": "msg_2", "model": "claude-x",
        "content": [
            {"type": "thinking", "thinking": "先看依赖"},
            {"type": "text", "text": "排好了"},
            {"type": "tool_use", "id": "t1", "name": "add_task", "input": {"name": "联调"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 9},
    })
    response = parse_anthropic_message(body)
    assert response.content == "排好了"
    assert response.reasoning == "先看依赖"
    assert response.tool_calls == [ToolCall(id="t1", name="add_task", arguments={"name": "联调"})]
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"input_tokens": 5, "output_tokens": 9}


@pytest.mark.parametrize(("stop", "expected"), [
    ("end_turn", "stop"), ("tool_use", "tool_calls"),
    ("max_tokens", "length"), ("stop_sequence", "stop"),
])
def test_stop_reason_mapping(stop, expected):
    assert parse_anthropic_message(message_body(stop=stop)).finish_reason == expected


def test_error_body_shape_is_classified():
    body = json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                  "message": "invalid x-api-key"}})
    transport = FakeTransport(response=HTTPResponse(401, body))
    with pytest.raises(AuthError) as caught:
        run(provider(transport).complete(CallRequest(messages=[Message(role="user", content="q")])))
    assert "invalid x-api-key" in str(caught.value)


def test_rate_limit_retry_after_is_carried():
    body = json.dumps({"type": "error", "error": {"type": "rate_limit_error",
                                                  "message": "slow down"}})
    transport = FakeTransport(response=HTTPResponse(429, body, {"retry-after": "3"}))
    with pytest.raises(RateLimitError) as caught:
        run(provider(transport).complete(CallRequest(messages=[Message(role="user", content="q")])))
    assert getattr(caught.value, "retry_after") == 3.0


def test_overloaded_529_is_server_error():
    transport = FakeTransport(response=HTTPResponse(
        529, json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                    "message": "Overloaded"}})))
    with pytest.raises(ServerError):
        run(provider(transport).complete(CallRequest(messages=[Message(role="user", content="q")])))


def test_provider_is_dumb_llmprovider():
    client = provider(FakeTransport(response=HTTPResponse(200, message_body())))
    assert isinstance(client, LLMProvider)
    assert not hasattr(client, "retry") and not hasattr(client, "policy")


# ── 3. 流式事件 → StreamChunk ───────────────────────────


def test_event_mapping_text_thinking_tool_and_usage():
    blocks = None
    from weave.providers.anthropic_http import _ToolBlockIndex

    blocks = _ToolBlockIndex()
    start = anthropic_event_to_chunks("message_start", {
        "type": "message_start",
        "message": {"model": "claude-x", "usage": {"input_tokens": 3}},
    }, blocks)
    assert start[0].kind == "usage" and start[0].usage == {"input": 3}

    tool_block = anthropic_event_to_chunks("content_block_start", {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "t1", "name": "add_task"},
    }, blocks)
    assert tool_block[0].kind == "tool_call_delta"
    assert (tool_block[0].index, tool_block[0].tool_call_id, tool_block[0].tool_call_name) == (0, "t1", "add_task")

    text = anthropic_event_to_chunks("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "排"},
    }, blocks)
    assert text[0].kind == "token" and text[0].text == "排"

    thinking = anthropic_event_to_chunks("content_block_delta", {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "想"},
    }, blocks)
    assert thinking[0].kind == "reasoning"

    partial = anthropic_event_to_chunks("content_block_delta", {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"name"'},
    }, blocks)
    assert partial[0].kind == "tool_call_delta" and partial[0].arguments_delta == '{"name"'
    assert partial[0].index == 0, "tool_call 的 index 必须是密集的 0..n-1"

    delta = anthropic_event_to_chunks("message_delta", {
        "type": "message_delta", "delta": {"stop_reason": "tool_use"},
        "usage": {"output_tokens": 5},
    }, blocks)
    assert [c.kind for c in delta] == ["usage", "finish"]
    assert delta[0].usage == {"output": 5}
    assert delta[1].finish_reason == "tool_calls"


def test_end_to_end_streaming_through_object():
    """与 OpenAI 版同样的验收：分片被切开也要拼对（半行 + tool_calls 增量）。"""
    events = [
        "event: message_start\ndata: " + json.dumps({
            "type": "message_start",
            "message": {"model": "claude-x", "usage": {"input_tokens": 4}}}) + "\n\n",
        "event: content_block_delta\ndata: " + json.dumps({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "排期"}}) + "\n\n",
        "event: content_block_start\ndata: " + json.dumps({
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "add_task"}}) + "\n\n",
        "event: content_block_delta\ndata: " + json.dumps({
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"name":"联'}}) + "\n\n",
        "event: content_block_delta\ndata: " + json.dumps({
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '调"}'}}) + "\n\n",
        "event: message_delta\ndata: " + json.dumps({
            "type": "message_delta", "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 6}}) + "\n\n",
        "event: message_stop\ndata: " + json.dumps({"type": "message_stop"}) + "\n\n",
    ]
    chopped: list[str] = []
    for event in events:                      # 人为切碎，验证半行缓冲
        mid = max(1, len(event) // 2)
        chopped.extend([event[:mid], event[mid:]])

    transport = FakeTransport(fragments=chopped)
    client = llm(provider(transport), retry=0)
    response = run(client.call_streaming([Message(role="user", content="帮我排期")]))

    assert response.content == "排期"
    assert response.tool_calls == [ToolCall(id="t1", name="add_task", arguments={"name": "联调"})]
    assert response.usage == {"input": 4, "output": 6, "total": 10}
    assert response.finish_reason == "tool_calls"
    assert transport.stream_calls[0]["payload"]["stream"] is True
    assert transport.stream_calls[0]["headers"]["x-api-key"] == "sk-ant-test"


def test_non_streaming_through_object():
    transport = FakeTransport(response=HTTPResponse(200, message_body(
        text="", tool_calls=[{"id": "t9", "name": "add_task", "input": {"name": "压测"}}],
        stop="tool_use")))
    client = llm(provider(transport), retry=0)
    response = run(client.call([Message(role="user", content="q")]))
    assert response.tool_calls == [ToolCall(id="t9", name="add_task", arguments={"name": "压测"})]
    assert response.usage == {"input": 11, "output": 7, "total": 18}


# ── 4. 装配入口（protocol="anthropic"） ─────────────────


def test_llm_protocol_anthropic_builds_the_right_provider():
    transport = FakeTransport(response=HTTPResponse(200, message_body(text="来自 Anthropic 形状")))
    client = llm(model="claude-sonnet-4-20250514", protocol="anthropic",
                 transport=transport, api_key="sk-ant-x", retry=0)
    assert type(client.provider).__name__ == "AnthropicHTTPProvider"
    response = run(client.call([Message(role="user", content="q")]))
    assert response.content == "来自 Anthropic 形状"
    sent = transport.calls[0]
    assert sent["url"].endswith("/v1/messages")
    assert sent["payload"]["max_tokens"] == 4096


def test_llm_rejects_unknown_protocol():
    with pytest.raises(TypeError, match="未知 protocol"):
        llm(model="m", protocol="gemini")
