"""把 README「它屏蔽了什么」里的四个例子真实跑一遍并打印（离线，不联网、不需要 key）。

    python scripts/show_abstraction_examples.py

这四个例子都是**纯函数 + 已有实现**，所以输出可复现——README 里贴的就是它的输出。
"""
from __future__ import annotations

import asyncio
import codecs
import json

from weave.core.envelopes import classify_exception
from weave.core.types import Message, ToolCall
from weave.llm.usage import normalize_usage
from weave.providers.anthropic_http import error_from_anthropic_response, messages_to_anthropic
from weave.providers.openai_http import error_from_response, messages_to_payload
from weave.providers.sse import iter_sse_events
from weave.providers.transport import HTTPResponse


def example_1_same_message_two_protocols() -> None:
    print("=== 例 1：同一条 Message，两个协议各自的请求体 ===")
    messages = [
        Message(role="system", content="简洁"),
        Message(role="assistant",
                tool_calls=[ToolCall(id="t1", name="get_weather", arguments={"city": "北京"})]),
        Message(role="tool", content='{"temp":24}', name="get_weather", tool_call_id="t1"),
    ]
    print("OpenAI    :", json.dumps(messages_to_payload(messages), ensure_ascii=False))
    system, converted = messages_to_anthropic(messages)
    print("Anthropic : system   =", json.dumps(system, ensure_ascii=False))
    print("            messages =", json.dumps(converted, ensure_ascii=False))


def example_2_usage_normalization() -> None:
    print("\n=== 例 2：usage 十几种写法 → 一个口径 ===")
    anthropic = {"input_tokens": 12, "output_tokens": 34,
                 "cache_read_input_tokens": 1024, "cache_creation_input_tokens": 256}
    openai = {"prompt_tokens": 12, "completion_tokens": 34,
              "prompt_tokens_details": {"cached_tokens": 1024},
              "completion_tokens_details": {"reasoning_tokens": 9}}
    print("Anthropic 原始 :", json.dumps(anthropic, ensure_ascii=False))
    print("OpenAI    原始 :", json.dumps(openai, ensure_ascii=False))
    print("Anthropic 归一 :", json.dumps(normalize_usage(anthropic), ensure_ascii=False))
    print("OpenAI    归一 :", json.dumps(normalize_usage(openai), ensure_ascii=False))


def example_3_error_normalization() -> None:
    print("\n=== 例 3：错误体两种嵌套 → 同一类失败 ===")
    openai_body = json.dumps({"error": {"message": "Rate limit reached",
                                        "type": "rate_limit_error"}})
    anthropic_body = json.dumps({"type": "error",
                                 "error": {"type": "rate_limit_error",
                                           "message": "tokens exceeded"}})
    openai_exc = error_from_response(
        HTTPResponse(status=429, body=openai_body, headers={"retry-after": "3"}))
    anthropic_exc = error_from_anthropic_response(
        HTTPResponse(status=429, body=anthropic_body, headers={}))
    for label, exc in (("OpenAI", openai_exc), ("Anthropic", anthropic_exc)):
        failure = classify_exception(exc)
        print(f"{label:10} → kind={failure.kind!r} retryable={failure.retryable} "
              f"retry_after={getattr(exc, 'retry_after', None)} message={failure.message[:40]!r}")


async def example_4_fragment_boundaries() -> None:
    print("\n=== 例 4：分片边界（SSE 行被切开 / 汉字断在分片中间）===")
    raw = "键".encode("utf-8")
    naive = [raw[:1].decode("utf-8", "replace"), raw[1:].decode("utf-8", "replace")]
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    incremental = [decoder.decode(raw[:1]), decoder.decode(raw[1:], final=True)]
    print(f"'键' 的 {len(raw)} 个字节被切成 1+{len(raw) - 1}：")
    print("  朴素逐块 decode :", naive, "→ 拼起来 =", repr("".join(naive)))
    print("  增量解码        :", incremental, "→ 拼起来 =", repr("".join(incremental)))

    fragments = ['data: {"choices":[{"delta":{"content":"关', '键路径"}}]}\n\ndata: [DONE]\n\n']

    async def gen():
        for fragment in fragments:
            yield fragment

    print("一个 data: 行被切成两个分片：")
    async for event in iter_sse_events(gen()):
        print("  仍拿到完整事件:", event.data)


def main() -> int:
    example_1_same_message_two_protocols()
    example_2_usage_normalization()
    example_3_error_normalization()
    asyncio.run(example_4_fragment_boundaries())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
