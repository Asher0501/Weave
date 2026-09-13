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
from weave.core.types import LLMResponse, Message, StreamChunk, ToolCall, ToolSchema


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
