"""内容块的三种形态：`str` / `Block`（中立）/ `dict`（厂商原样）。

- `str`：适配层按厂商要求包成文本块，跨厂商可移植；
- `Block`（`TextBlock` / `ImageBlock`）：**你写语义、适配层生成形状** —— 输入统一的关键；
- `dict`：厂商原样块，逐字透传、weave 不解释 key（代价是换 provider 不可移植）。

响应侧保留厂商原始块（`LLMResponse.raw_blocks`），让 thinking / 多模态块能无损回填下一轮。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from weave.core.types import (
    ImageBlock,
    LLMResponse,
    Message,
    TextBlock,
    ToolCall,
    payload_content,
)
from weave.llm import llm
from weave.providers.anthropic_http import (
    AnthropicHTTPProvider,
    messages_to_anthropic,
    parse_anthropic_message,
)
from weave.providers.fake import FakeProvider, FakeTurn
from weave.providers.openai_http import (
    OpenAIHTTPProvider,
    messages_to_payload,
    parse_completion,
)
from weave.providers.transport import HTTPResponse


def run(coro):
    return asyncio.run(coro)


IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAB"}}
CACHE = {"type": "text", "text": "很长的系统提示", "cache_control": {"type": "ephemeral"}}
WEIRD = {"type": "vendor_future_thing", "任意 key": {"嵌套": [1, 2, 3]}}


# ── 1. 两种形态的唯一定义处 ──────────────────────────────


def test_str_content_stays_on_the_semantic_path():
    assert payload_content(Message(role="user", content="你好")) == "你好"


def test_dict_content_is_wrapped_as_a_single_block():
    assert payload_content(Message(role="user", content=IMAGE)) == [IMAGE]


def test_list_content_passes_through_without_inspecting_keys():
    assert payload_content(Message(role="user", content=[WEIRD])) == [WEIRD]


def test_passthrough_copies_instead_of_aliasing():
    original = [dict(IMAGE)]
    out = payload_content(Message(role="user", content=original))
    assert out is not original and out[0] is not original[0]
    original[0]["type"] = "被调用方改了"
    assert out[0]["type"] == "image"


def test_non_dict_blocks_are_rejected_loudly():
    with pytest.raises(TypeError):
        payload_content(Message(role="user", content=["纯文本不是块"]))
    with pytest.raises(TypeError):
        payload_content(Message(role="user", content=123))


def test_raw_content_plus_tool_calls_is_ambiguous_and_rejected():
    message = Message(role="assistant", content=[IMAGE],
                      tool_calls=[ToolCall(id="c1", name="t", arguments={})])
    with pytest.raises(TypeError) as err:
        payload_content(message)
    assert "tool_calls" in str(err.value)


# ── 2. OpenAI 兼容端点 ──────────────────────────────────


def test_openai_passes_blocks_through_verbatim():
    blocks = [{"type": "image_url", "image_url": {"url": "https://x/y.png", "detail": "high"}}]
    payload = messages_to_payload([Message(role="user", content=blocks)])
    assert payload == [{"role": "user", "content": blocks}]


def test_openai_tool_message_can_carry_blocks():
    blocks = [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]
    payload = messages_to_payload([
        Message(role="tool", content=blocks, tool_call_id="c1", name="shot")
    ])
    assert payload[0]["content"] == blocks
    assert payload[0]["tool_call_id"] == "c1"


def test_openai_str_path_behavior_is_unchanged():
    message = Message(role="assistant", content="我来调用",
                      tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})])
    entry = messages_to_payload([message])[0]
    assert entry["content"] == "我来调用"
    # 老行为：dict 入参被序列化成 JSON 字符串（解码归一在对象层）
    assert entry["tool_calls"][0]["function"]["arguments"] == '{"a": 1, "b": 2}'


# ── 3. Anthropic 原生端点 ───────────────────────────────


def test_anthropic_user_blocks_pass_through_verbatim():
    system, out = messages_to_anthropic([Message(role="user", content=[IMAGE])])
    assert system is None
    assert out == [{"role": "user", "content": [IMAGE]}]


def test_anthropic_multiple_str_system_keeps_old_behavior():
    system, _ = messages_to_anthropic([
        Message(role="system", content="规则一"),
        Message(role="system", content="规则二"),
    ])
    assert system == "规则一\n\n规则二"


def test_anthropic_raw_system_becomes_block_array_so_cache_control_is_reachable():
    system, _ = messages_to_anthropic([
        Message(role="system", content=[CACHE]),
        Message(role="system", content="补充规则"),
    ])
    assert system == [CACHE, {"type": "text", "text": "补充规则"}]


def test_anthropic_tool_result_can_carry_blocks():
    _, out = messages_to_anthropic([
        Message(role="tool", content=[IMAGE], tool_call_id="toolu_1")
    ])
    assert out == [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [IMAGE]}],
    }]


def test_anthropic_adjacent_tool_results_still_merge_into_one_user_message():
    _, out = messages_to_anthropic([
        Message(role="tool", content="结果一", tool_call_id="t1"),
        Message(role="tool", content="结果二", tool_call_id="t2"),
    ])
    assert len(out) == 1
    assert [block["tool_use_id"] for block in out[0]["content"]] == ["t1", "t2"]


def test_anthropic_assistant_blocks_pass_through_verbatim():
    blocks = [{"type": "thinking", "thinking": "先算", "signature": "sig"},
              {"type": "text", "text": "答案是 3"}]
    _, out = messages_to_anthropic([Message(role="assistant", content=blocks)])
    assert out == [{"role": "assistant", "content": blocks}]


# ── 4. 响应侧闭环（这次改动的重点） ─────────────────────


RESPONSE_BODY = json.dumps({
    "id": "msg_1", "type": "message", "model": "claude-x",
    "content": [
        {"type": "thinking", "thinking": "先算一下", "signature": "sig"},
        {"type": "text", "text": "结果是 3"},
        {"type": "tool_use", "id": "toolu_1", "name": "add", "input": {"a": 1, "b": 2}},
        {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 10, "output_tokens": 5},
})


def test_anthropic_response_keeps_every_block_including_unknown_ones():
    resp = parse_anthropic_message(RESPONSE_BODY)
    assert resp.content == "结果是 3"
    assert resp.reasoning == "先算一下"
    assert [call.name for call in resp.tool_calls] == ["add"]
    assert resp.finish_reason == "tool_calls"
    assert [block["type"] for block in resp.raw_blocks] == [
        "thinking", "text", "tool_use", "server_tool_use",
    ]


def test_thinking_blocks_can_be_sent_back_unchanged():
    """extended thinking + 工具调用要求 thinking 块随 tool_use 一起回传——这就是那条闭环。"""
    resp = parse_anthropic_message(RESPONSE_BODY)
    _, out = messages_to_anthropic([Message(role="assistant", content=resp.raw_blocks)])
    assert out[0]["content"] == resp.raw_blocks


def test_openai_array_content_is_flattened_but_kept():
    body = json.dumps({
        "model": "m",
        "choices": [{
            "message": {"content": [{"type": "text", "text": "A"},
                                    {"type": "image_url", "image_url": {"url": "u"}}]},
            "finish_reason": "stop",
        }],
    })
    resp = parse_completion(body)
    assert resp.content == "A" and isinstance(resp.content, str)
    assert [block["type"] for block in resp.raw_blocks] == ["text", "image_url"]


def test_openai_string_content_leaves_no_blocks():
    body = json.dumps({"model": "m",
                       "choices": [{"message": {"content": "普通文本"}, "finish_reason": "stop"}]})
    resp = parse_completion(body)
    assert resp.content == "普通文本"
    assert resp.raw_blocks is None


# ── 5. 对象层行为 ───────────────────────────────────────


def test_object_layer_does_not_touch_raw_content():
    provider = FakeProvider(turns=[FakeTurn(content="ok")])
    blocks = [dict(IMAGE)]
    client = llm(provider=provider)
    run(client.call([Message(role="user", content=blocks)]))
    assert provider.calls[0].messages[0].content == blocks


def test_misuse_fails_before_any_request_and_is_never_retried():
    provider = FakeProvider(turns=[FakeTurn(content="ok")])
    client = llm(provider=provider, retry=3)
    message = Message(role="assistant", content=[IMAGE],
                      tool_calls=[ToolCall(id="c1", name="t", arguments={})])
    with pytest.raises(TypeError):
        run(client.call([message]))
    assert provider.calls == []          # 一个请求都没发，也没被当成 server 失败重放


def test_streaming_path_accepts_raw_content_too():
    provider = FakeProvider(turns=[FakeTurn(content="流式")])
    client = llm(provider=provider)
    resp = run(client.call_streaming([Message(role="user", content=[IMAGE])]))
    assert provider.calls[0].messages[0].content == [IMAGE]
    assert resp.content == "流式"


# ── 6. 契约面没长（这是刻意的决定，锁住它） ─────────────


def test_llmresponse_block_field_defaults_to_none():
    assert LLMResponse(content="x").raw_blocks is None


def test_block_vocabulary_is_closed_and_deliberate():
    """契约面（词汇表）有意扩过一次：从「零新增」改成「只加一个**封闭**的内容块集合」。

    这条锁的是**扩张的方式**：`Block` 就是 `TextBlock | ImageBlock` 这一个封闭联合，
    没有顺手长出一堆块类型；内部归一函数仍不进契约面。
    """
    import weave.core.envelopes as E
    import weave.core.types as T

    assert T.Block == (T.TextBlock | T.ImageBlock)
    assert (E.TextBlock, E.ImageBlock) == (T.TextBlock, T.ImageBlock)   # 两处同一对象（再导出）
    assert not hasattr(E, "payload_content"), "内部归一函数不进契约面"
    assert isinstance(T.TextBlock("x"), T.Block) and isinstance(T.ImageBlock(url="u"), T.Block)


# ── 7. 适配层的"错家形状"预检（配置决定用哪家的字段） ────

OPENAI_IMAGE = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
ANTHROPIC_IMAGE = {"type": "image",
                   "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}
ANTHROPIC_RESPONSE = json.dumps({
    "id": "m1", "model": "claude-x",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
})


class RecordingTransport:
    """只记录请求、返回预设响应；不联网。"""

    def __init__(self, body: str = '{"choices":[{"message":{"content":"ok"},'
                                   '"finish_reason":"stop"}]}') -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []

    async def post_json(self, url, *, headers, payload, timeout=None) -> HTTPResponse:
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        return HTTPResponse(status=200, body=self.body, headers={})


def anthropic_client(*, shape_check: bool = True):
    transport = RecordingTransport(ANTHROPIC_RESPONSE)
    provider = AnthropicHTTPProvider(model="m", base_url="https://x", api_key="k",
                                     transport=transport)
    return llm(provider=provider, shape_check=shape_check), transport


def openai_client(*, shape_check: bool = True):
    transport = RecordingTransport()
    provider = OpenAIHTTPProvider(model="m", base_url="https://x", api_key="k",
                                  transport=transport)
    return llm(provider=provider, shape_check=shape_check), transport


def test_anthropic_adapter_rejects_openai_shaped_block():
    client, transport = anthropic_client()
    with pytest.raises(TypeError) as err:
        run(client.call([Message(role="user", content=[OPENAI_IMAGE])]))
    message = str(err.value)
    assert "image_url" in message and "anthropic" in message
    assert "shape_check=False" in message          # 报错要给出可照做的出路
    assert transport.calls == []                   # 预检在发请求之前


def test_openai_adapter_rejects_anthropic_shaped_blocks():
    client, transport = openai_client()
    for block in (ANTHROPIC_IMAGE,
                  {"type": "tool_result", "tool_use_id": "t1", "content": "结果"},
                  {"type": "thinking", "thinking": "想一想"}):
        with pytest.raises(TypeError) as err:
            run(client.call([Message(role="user", content=[block])]))
        assert block["type"] in str(err.value) and "openai" in str(err.value)
    assert transport.calls == []


def test_correct_shape_reaches_the_wire():
    client, transport = anthropic_client()
    run(client.call([Message(role="user", content=[ANTHROPIC_IMAGE])]))
    assert transport.calls[0]["payload"]["messages"][0]["content"] == [ANTHROPIC_IMAGE]


def test_unknown_block_types_are_never_blocked():
    """不能变成挡厂商新特性的墙——这是放开原样形态的全部意义。"""
    for factory in (anthropic_client, openai_client):
        client, transport = factory()
        run(client.call([Message(role="user", content=[{"type": "brand_new_2027_thing"}])]))
        assert len(transport.calls) == 1


def test_shape_check_can_be_disabled():
    client, transport = anthropic_client(shape_check=False)
    run(client.call([Message(role="user", content=[OPENAI_IMAGE])]))
    assert len(transport.calls) == 1


def test_shape_check_runs_before_retries_and_never_wastes_them():
    class StrictFake(FakeProvider):
        protocol = "fake"
        foreign_block_types = frozenset({"tool_result"})
        block_shape_hint = '{"type":"text","text":…}'

    provider = StrictFake(turns=[FakeTurn(content="ok")])
    client = llm(provider=provider, retry=3)
    with pytest.raises(TypeError):
        run(client.call([Message(role="user",
                                 content=[{"type": "tool_result", "tool_use_id": "t"}])]))
    assert provider.calls == []                    # 一个请求都没发，也没进重放


def test_provider_without_declaration_is_not_checked():
    provider = FakeProvider(turns=[FakeTurn(content="ok")])
    client = llm(provider=provider)
    run(client.call([Message(role="user", content=[OPENAI_IMAGE])]))
    assert len(provider.calls) == 1


# ── 8. 中立块（Block）：输入统一的落点 ──────────────────


def test_image_block_validates_at_construction():
    with pytest.raises(ValueError):                       # 既没 url 也没 data
        ImageBlock()
    with pytest.raises(ValueError):                       # 两个都给了
        ImageBlock(url="https://x/y.png", data="AA", media_type="image/png")
    with pytest.raises(ValueError):                       # base64 但没 media_type
        ImageBlock(data="AA")
    with pytest.raises(ValueError):                       # data 里带了 data URI 前缀
        ImageBlock(data="data:image/png;base64,AA", media_type="image/png")
    assert ImageBlock(url="https://x/y.png").url == "https://x/y.png"
    assert ImageBlock(data="AA", media_type="image/png").media_type == "image/png"


def test_blocks_are_frozen_values():
    with pytest.raises(Exception):
        TextBlock("x").text = "y"                         # frozen：构造后不可改
    assert TextBlock("x") == TextBlock("x")


def test_payload_content_accepts_both_new_forms():
    single = payload_content(Message(role="user", content=TextBlock("看图")))
    assert single == [TextBlock("看图")]
    blocks = [TextBlock("看图"), ImageBlock(url="https://x/y.png")]
    assert payload_content(Message(role="user", content=blocks)) == blocks
    assert payload_content(Message(role="user", content="纯文本")) == "纯文本"


def test_mixing_neutral_blocks_with_vendor_dicts_is_rejected():
    with pytest.raises(TypeError) as err:
        payload_content(Message(role="user", content=[TextBlock("x"), OPENAI_IMAGE]))
    assert "混用" in str(err.value)


def test_neutral_blocks_may_coexist_with_tool_calls():
    call = ToolCall(id="c1", name="t", arguments={})
    content = payload_content(Message(role="assistant", content=[TextBlock("我来调用")],
                                      tool_calls=[call]))
    assert content == [TextBlock("我来调用")]        # Block 是封闭集合 → 不与 tool_calls 冲突


def _semantic(parts: list[dict]) -> list[tuple]:
    """把两家厂商的内容数组拍成语义三元组，用来断言「输入统一」真的等价。"""
    out: list[tuple] = []
    for part in parts:
        kind = part.get("type")
        if kind == "text":
            out.append(("text", part["text"]))
        elif kind == "image_url":                        # OpenAI 形状
            url = part["image_url"]["url"]
            if url.startswith("data:"):
                head, _, encoded = url.partition(",")
                out.append(("image", "base64", head[5:].split(";")[0], encoded))
            else:
                out.append(("image", "url", url))
        elif kind == "image":                            # Anthropic 形状
            source = part["source"]
            if source["type"] == "url":
                out.append(("image", "url", source["url"]))
            else:
                out.append(("image", "base64", source["media_type"], source["data"]))
    return out


def test_same_blocks_produce_semantically_equal_payloads_in_both_protocols():
    """输入统一的可验收标准：一份 Block 输入 → 两家 payload 语义等价（形状不同）。"""
    for image in (ImageBlock(url="https://x/y.png"),
                  ImageBlock(data="QUJD", media_type="image/png")):
        messages = [Message(role="user", content=[TextBlock("看图"), image])]

        _, anthropic = messages_to_anthropic(messages)
        openai = messages_to_payload(messages)
        assert _semantic(anthropic[0]["content"]) == _semantic(openai[0]["content"])
        assert _semantic(openai[0]["content"]) == (
            [("text", "看图")] + [("image", "url", "https://x/y.png")] if image.url
            else [("text", "看图"), ("image", "base64", "image/png", "QUJD")]
        )


def test_base64_encoding_stays_out_of_the_caller_view():
    """`data:…;base64,` 是 OpenAI 的编码习惯，只出现在它的适配层里。"""
    messages = [Message(role="user", content=[ImageBlock(data="QUJD", media_type="image/png")])]
    openai = messages_to_payload(messages)[0]["content"][0]
    _, anthropic = messages_to_anthropic(messages)
    assert openai["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert anthropic[0]["content"][0]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "QUJD"}


def test_anthropic_assistant_blocks_and_tool_calls_share_one_content_array():
    call = ToolCall(id="t1", name="get_weather", arguments={"city": "北京"})
    message = Message(role="assistant", content=[TextBlock("我来查")], tool_calls=[call])
    _, out = messages_to_anthropic([message])
    assert out[0]["content"] == [
        {"type": "text", "text": "我来查"},
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {"city": "北京"}},
    ]


def test_tool_result_can_carry_a_neutral_image_block():
    message = Message(role="tool", content=[ImageBlock(url="https://x/y.png")],
                      tool_call_id="t1", name="shot")
    _, anthropic = messages_to_anthropic([message])
    assert anthropic[0]["content"][0]["content"] == [
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]
    openai = messages_to_payload([message])
    assert openai[0]["content"] == [{"type": "image_url",
                                     "image_url": {"url": "https://x/y.png"}}]


def test_system_blocks_become_anthropic_block_array():
    _, _ = messages_to_anthropic([Message(role="user", content="x")])
    system, _ = messages_to_anthropic([Message(role="system", content=[TextBlock("长提示")])])
    assert system == [{"type": "text", "text": "长提示"}]


def test_neutral_blocks_survive_the_object_layer_and_shape_check():
    client, transport = anthropic_client()               # Anthropic 实例
    run(client.call([Message(role="user", content=[ImageBlock(url="https://x/y.png")])]))
    wire = transport.calls[0]["payload"]["messages"][0]["content"]
    assert wire == [{"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]


def test_vendor_dicts_are_still_rejected_on_the_wrong_adapter():
    client, transport = anthropic_client()
    with pytest.raises(TypeError):                       # OpenAI 形状的 dict 仍会被拦
        run(client.call([Message(role="user", content=[OPENAI_IMAGE])]))
    assert transport.calls == []


def test_raw_vendor_blocks_still_pass_through_verbatim():
    block = {"type": "text", "text": "带 cache_control", "cache_control": {"type": "ephemeral"}}
    system, _ = messages_to_anthropic([Message(role="system", content=[block])])
    assert system == [block]                             # 一个 key 都没被改
