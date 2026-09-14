"""信封测试（weave.core.envelopes）：进去的请求、出来的响应、失败的信封。

信封只有一套，所以这些不变量必须钉死：
- `TypedFailure.kind` ↔ `retryable` 永远一致（失败分类不能自相矛盾）；
- 异常 → 失败信封的映射表完整；
- 请求/响应/增量的形状稳定（对象与 provider 都依赖它）。
"""
from __future__ import annotations

import pytest

from weave.core.envelopes import (
    NON_RETRYABLE_KINDS,
    RETRYABLE_KINDS,
    CallRequest,
    Invocation,
    TypedFailure,
    classify_exception,
)
from weave.core.errors import (
    AuthError,
    BadRequestError,
    NetworkError,
    ParseError,
    ProviderRejectedError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
)
from weave.core.types import (
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    ToolSchema,
    payload_content,
)
from weave.providers.anthropic_http import messages_to_anthropic
from weave.providers.openai_http import messages_to_payload


# ── CallRequest ─────────────────────────────────────────


def test_call_request_defaults_and_fields():
    request = CallRequest(
        messages=[Message(role="user", content="hi")],
        schemas=[ToolSchema(name="read_file")],
        opts={"max_tokens": 128},
    )
    assert request.schemas[0].name == "read_file"
    assert request.opts["max_tokens"] == 128
    assert CallRequest().messages == [] and CallRequest().opts == {}


def test_message_and_tool_call_shapes():
    call = ToolCall(id="c1", name="read_file", arguments='{"path":"a.txt"}')
    message = Message(role="assistant", content="", tool_calls=[call])
    tool_msg = Message(role="tool", content="ok", name="read_file", tool_call_id="c1")

    assert message.tool_calls[0].name == "read_file"
    assert tool_msg.tool_call_id == "c1"
    # arguments 保持厂商原样（字符串），解码是对象的职责
    assert call.arguments == '{"path":"a.txt"}'


def test_llm_response_normalization_fields_have_defaults():
    response = LLMResponse(content="x")
    assert response.attempts == 1 and response.elapsed_ms == 0.0
    assert response.failure is None and response.raw is None
    assert response.finish_reason == "stop"


def test_stream_chunk_kinds_and_delta_fields():
    token = StreamChunk(kind="token", text="你")
    delta = StreamChunk(kind="tool_call_delta", index=0, tool_call_id="c1",
                        tool_call_name="read_file", arguments_delta='{"a"')
    usage = StreamChunk(kind="usage", usage={"prompt_tokens": 1})
    assert token.text == "你" and delta.index == 0 and usage.usage == {"prompt_tokens": 1}


# ── Invocation（解码产物） ──────────────────────────────


def test_invocation_defaults():
    inv = Invocation(name="t")
    assert inv.arguments == {} and inv.id is None and inv.raw is None


# ── TypedFailure：kind 与 retryable 必须一致 ─────────────


def test_kind_sets_are_disjoint():
    assert RETRYABLE_KINDS.isdisjoint(NON_RETRYABLE_KINDS)


def test_typed_failure_rejects_unknown_kind():
    with pytest.raises(ValueError):
        TypedFailure(kind="made_up")


def test_typed_failure_rejects_contradicting_retryable():
    with pytest.raises(ValueError):
        TypedFailure(kind="rate_limit", retryable=False)
    with pytest.raises(ValueError):
        TypedFailure(kind="auth", retryable=True)


def test_typed_failure_carries_origin_and_detail():
    failure = TypedFailure(kind="server", retryable=True, message="boom",
                           origin="provider:fake", detail={"status": 503})
    assert failure.origin == "provider:fake" and failure.detail["status"] == 503


# ── 异常 → 失败信封 ─────────────────────────────────────


@pytest.mark.parametrize(("exc", "kind"), [
    (RateLimitError("429"), "rate_limit"),
    (ServerError("500"), "server"),
    (NetworkError("conn reset"), "network"),
    (ProviderTimeoutError("slow"), "timeout"),
    (TimeoutError(), "timeout"),
    (AuthError("401"), "auth"),
    (BadRequestError("400"), "bad_request"),
    (ProviderRejectedError("402"), "rejected"),
    (ParseError("bad output"), "parse_error"),
    (ValueError("unknown"), "server"),
])
def test_classify_exception_mapping(exc, kind):
    failure = classify_exception(exc, origin="test")
    assert failure.kind == kind
    assert failure.retryable == (kind in RETRYABLE_KINDS)
    assert failure.origin == "test"


def test_classify_preserves_message():
    failure = classify_exception(RateLimitError("慢一点"))
    assert "慢一点" in failure.message


# ── 回填构造器：工具往返的"纯搬运"只有一处实现 ──────────


def test_response_as_message_carries_content_and_tool_calls():
    call = ToolCall(id="c1", name="get_weather", arguments={"city": "北京"})
    response = LLMResponse(content="我来查一下", tool_calls=[call], finish_reason="tool_calls")
    message = response.as_message()
    assert message.role == "assistant"
    assert message.content == "我来查一下"
    assert message.tool_calls == [call]


def test_response_as_message_with_empty_content_stays_a_valid_semantic_message():
    call = ToolCall(id="c1", name="t", arguments={})
    message = LLMResponse(tool_calls=[call]).as_message()
    assert message.content == ""
    assert payload_content(message) == ""          # str 形态 → 不触发"原样块 + tool_calls"歧义


def test_tool_result_takes_name_and_id_from_the_call():
    call = ToolCall(id="toolu_1", name="get_weather", arguments={})
    message = Message.tool_result(call, "晴，24℃")
    assert (message.role, message.content) == ("tool", "晴，24℃")
    assert message.name == "get_weather"
    assert message.tool_call_id == "toolu_1"


def test_tool_result_serializes_non_string_output_as_json():
    call = ToolCall(id="c1", name="t", arguments={})
    assert Message.tool_result(call, {"城市": "北京", "温度": 24}).content == '{"城市": "北京", "温度": 24}'
    assert Message.tool_result(call, [1, 2]).content == "[1, 2]"
    assert Message.tool_result(call).content == ""        # 默认输出为空字符串（str 原样）


def test_tool_result_rejects_non_serializable_output_loudly():
    call = ToolCall(id="c1", name="t", arguments={})
    with pytest.raises(TypeError):
        Message.tool_result(call, object())


def test_filled_back_messages_match_ids_in_both_protocols():
    """两个构造器产出的消息，在两个协议里 id 都对得上——这就是它们存在的理由。"""
    call = ToolCall(id="toolu_1", name="get_weather", arguments={"city": "北京"})
    response = LLMResponse(tool_calls=[call], finish_reason="tool_calls")
    messages = [
        Message(role="user", content="北京天气"),
        response.as_message(),
        Message.tool_result(call, {"temp": 24}),
    ]

    _, anthropic = messages_to_anthropic(messages)
    assert anthropic[1]["content"][0]["type"] == "tool_use"
    assert anthropic[1]["content"][0]["id"] == anthropic[2]["content"][0]["tool_use_id"]

    openai = messages_to_payload(messages)
    assert openai[1]["tool_calls"][0]["id"] == openai[2]["tool_call_id"] == "toolu_1"
