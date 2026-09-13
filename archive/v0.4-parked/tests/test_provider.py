"""Provider 层测试：可靠性语义（T2）+ OpenAI 兼容纯函数 + FakeProvider。"""
import asyncio

import pytest

from weave.core.errors import (
    AuthError,
    ProviderTimeoutError,
    RateLimitError,
    RetryExhaustedError,
    ServerError,
)
from weave.core.types import Message, ToolCall, ToolSchema
from weave.providers._reliability import RetryPolicy, call_with_reliability
from weave.providers.fake import FakeProvider, FakeTurn
from weave.providers.openai_compat import (
    _build_request_kwargs,
    _is_reasoner,
    messages_to_openai,
)


def test_retry_recovers_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("slow down")
        return "done"

    result = asyncio.run(
        call_with_reliability(
            RetryPolicy(max_attempts=5, backoff=0.05), flaky
        )
    )
    assert result == "done"
    assert calls["n"] == 3


def test_retry_exhausted():
    async def always_fail():
        raise ServerError("boom")

    with pytest.raises(RetryExhaustedError):
        asyncio.run(
            call_with_reliability(
                RetryPolicy(max_attempts=2, backoff=0.05), always_fail
            )
        )


def test_non_retryable_raises_immediately():
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise AuthError("no")

    with pytest.raises(AuthError):
        asyncio.run(
            call_with_reliability(
                RetryPolicy(max_attempts=5, backoff=0.05), bad
            )
        )
    assert calls["n"] == 1


def test_overall_timeout_is_hard_cap():
    async def hang():
        await asyncio.sleep(5)
        return "late"

    with pytest.raises(ProviderTimeoutError):
        asyncio.run(
            call_with_reliability(
                RetryPolicy(max_attempts=3, backoff=0.05, overall_timeout=0.2),
                hang,
            )
        )


def test_messages_to_openai_with_tools():
    msgs = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1})],
        ),
        Message(role="tool", content="3", name="add", tool_call_id="c1"),
    ]
    wire = messages_to_openai(msgs)
    assert wire[0] == {"role": "user", "content": "hi"}
    assert wire[1]["tool_calls"][0]["function"]["name"] == "add"
    assert wire[2]["tool_call_id"] == "c1"
    # Message 模型无 reasoning 字段 → 回传消息不可能携带推理内容
    assert all("reasoning" not in m for m in wire)


def test_reasoner_param_filter():
    schema = ToolSchema(name="add", description="加", parameters={"type": "object"})
    kwargs = _build_request_kwargs(
        model="deepseek-reasoner",
        messages=[Message(role="user", content="x")],
        tools=[schema],
        max_tokens=1024,
        temperature=0.7,
        extra={},
    )
    assert "temperature" not in kwargs
    assert kwargs["tools"][0]["function"]["name"] == "add"
    assert _is_reasoner("deepseek-reasoner")
    assert not _is_reasoner("deepseek-chat")


def test_fake_provider_scripted():
    prov = FakeProvider(
        turns=[
            FakeTurn(tool_calls=[ToolCall(id="1", name="add", arguments={"a": 1, "b": 2})]),
            FakeTurn(content="3"),
        ]
    )
    r1 = asyncio.run(prov.complete([Message(role="user", content="算")]))
    assert r1.tool_calls and r1.tool_calls[0].name == "add"
    r2 = asyncio.run(prov.complete([Message(role="user", content="结果?")]))
    assert r2.content == "3"
    assert len(prov.calls) == 2
