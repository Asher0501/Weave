"""真实厂商冒烟：一次非流式 + 一次流式，把「原始响应」与「归一结果」并排打印。

为什么要它：weave 的协议层是用注入 transport 做的离线验证；**从未被真实端点跑过**。
这个脚本用最小 token 打两次真实请求，用来闭环验证：
  - 请求形状厂商是否接受（认证头 / 必需字段 / 参数过滤）
  - 非流式响应能不能被正确归一（content / tool_calls / usage / finish_reason）
  - 流式 SSE 能不能被正确聚合（半行、事件流、末尾 usage）

凭证解析顺序（**只打印来源，绝不打印 key 本身**）：
  1. 环境变量：`--api-key-env`（默认按协议取 WEAVE_API_KEY / ANTHROPIC_API_KEY /
     ANTHROPIC_AUTH_TOKEN / OPENAI_API_KEY / DEEPSEEK_API_KEY）
  2. `~/.claude/settings.json`：递归找上述任一字段（Claude Code 的配置惯例）

用法：
    python scripts/smoke_real_llm.py --protocol anthropic \\
        --base-url https://api.deepseek.com/anthropic --model deepseek-v4-pro \\
        --prompt "只回四个字：冒烟成功" --max-tokens 32
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weave.core.types import Message                     # noqa: E402
from weave.providers.transport import HTTPResponse, UrllibTransport   # noqa: E402
from weave.providers.transport import retry_after_from_headers        # noqa: E402

ENV_CANDIDATES = (
    "WEAVE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
)
CLAUDE_SETTINGS = pathlib.Path.home() / ".claude" / "settings.json"


class RecordingTransport(UrllibTransport):
    """包一层，记录最后一次原始响应体（用于"原始 vs 归一"对照）。不记录头（可能含 key）。"""

    def __init__(self) -> None:
        super().__init__()
        self.last_body: str | None = None
        self.last_status: int | None = None
        self.chunks = 0

    async def post_json(self, url, *, headers, payload, timeout=None) -> HTTPResponse:
        response = await super().post_json(url, headers=headers, payload=payload, timeout=timeout)
        self.last_body, self.last_status = response.body, response.status
        return response

    def post_sse(self, url, *, headers, payload, timeout=None):
        inner = super().post_sse(url, headers=headers, payload=payload, timeout=timeout)
        counter = self

        async def gen():
            async for fragment in inner:
                counter.chunks += 1
                yield fragment

        return gen()


def _dig(obj, key):
    """在嵌套 JSON 里递归找字段。"""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str) and obj[key]:
            return obj[key]
        for value in obj.values():
            found = _dig(value, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _dig(item, key)
            if found:
                return found
    return None


def resolve_api_key() -> tuple[str | None, str]:
    for name in ENV_CANDIDATES:
        value = os.environ.get(name)
        if value:
            return value, f"环境变量 {name}"
    if CLAUDE_SETTINGS.exists():
        try:
            data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, "（settings.json 读取失败）"
        for name in ENV_CANDIDATES:
            value = _dig(data, name)
            if value:
                return value, f"{CLAUDE_SETTINGS} 的 {name}"
    return None, "（找不到）"


def resolve_from_settings(field: str) -> str | None:
    if not CLAUDE_SETTINGS.exists():
        return None
    try:
        data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _dig(data, field)


def show(title: str, value) -> None:
    print(f"  {title:<22}{value}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", default="anthropic", choices=["anthropic", "openai"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--prompt", default="只回四个字：冒烟成功")
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--retry", type=int, default=2)
    parser.add_argument("--no-stream", action="store_true", help="跳过流式那一次")
    parser.add_argument("--tools", action="store_true", help="声明一个演示工具，验证 tool_use → 解码归一")
    args = parser.parse_args()

    api_key, key_source = resolve_api_key()
    model = args.model or resolve_from_settings("ANTHROPIC_MODEL") or "deepseek-chat"
    base_url = args.base_url or resolve_from_settings("ANTHROPIC_BASE_URL")
    if args.protocol == "openai":
        base_url = base_url or resolve_from_settings("OPENAI_BASE_URL")

    print("=" * 78)
    print("真实厂商冒烟（两次最小请求：非流式 + 流式）")
    print("=" * 78)
    show("protocol", args.protocol)
    show("model", model)
    show("base_url", base_url or "（该协议的默认端点）")
    show("api_key", f"已找到（来源：{key_source}）" if api_key else f"**没找到** {key_source}")
    print()

    if not api_key:
        print("没有凭证，无法冒烟。设 WEAVE_API_KEY（或 ANTHROPIC_API_KEY）后重试。")
        return 2

    import weave

    transport = RecordingTransport()
    client = weave.llm(
        model=model,
        protocol=args.protocol,
        base_url=base_url,
        api_key=api_key,
        transport=transport,
        max_tokens=args.max_tokens,
        timeout=30,
        total_timeout=60,
        retry=args.retry,
        observer=lambda event, data: print(f"    [event] {event} {json.dumps(data, ensure_ascii=False)[:120]}"),
    )

    messages = [Message(role="user", content=args.prompt)]
    tools = None
    if args.tools:
        from weave.core.types import ToolSchema

        tools = [ToolSchema(
            name="get_weather",
            description="查询某个城市的当前天气",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        )]

    # ── 1. 非流式 ─────────────────────────────────────────
    print("-" * 78)
    print("① 非流式 call()" + ("（带工具声明）" if tools else ""))
    started = time.perf_counter()
    try:
        response = await client.call(messages, tools=tools)
    except Exception as exc:                    # noqa: BLE001
        print(f"  ✗ 失败：{type(exc).__name__}: {exc}")
        failure = getattr(exc, "failure", None)
        if failure:
            print(f"    kind={failure.kind} retryable={failure.retryable} origin={failure.origin}")
        return 1
    elapsed_ms = (time.perf_counter() - started) * 1000

    raw_body = transport.last_body or ""
    print(f"  原始响应（HTTP {transport.last_status}，{len(raw_body)} 字节）：")
    print("    " + raw_body[:600].replace("\n", "\n    "))
    print("  归一结果：")
    show("content", repr(response.content[:200]))
    show("finish_reason", response.finish_reason)
    show("model", response.model)
    show("usage", response.usage)
    show("attempts", response.attempts)
    show("elapsed_ms", f"{elapsed_ms:.0f}（对象自报 {response.elapsed_ms:.0f}）")
    show("tool_calls", len(response.tool_calls or []))
    show("failure", response.failure)
    print(f"  对象级累计 usage：{client.usage}")

    # ── 2. 流式 ───────────────────────────────────────────
    if not args.no_stream:
        print("-" * 78)
        print("② 流式 call_streaming()（内部聚合）")
        chunks: list[str] = []
        try:
            streamed = await client.call_streaming(
                [Message(role="user", content=args.prompt)],
                on_chunk=lambda chunk: chunks.append(chunk.kind),
            )
        except Exception as exc:                # noqa: BLE001
            print(f"  ✗ 失败：{type(exc).__name__}: {exc}")
            return 1
        print(f"  收到 {transport.chunks} 个网络分片 → {len(chunks)} 个归一 chunk")
        print(f"  chunk 种类：{ {k: chunks.count(k) for k in sorted(set(chunks))} }")
        print("  聚合结果：")
        show("content", repr(streamed.content[:200]))
        show("finish_reason", streamed.finish_reason)
        show("usage", streamed.usage)
        show("attempts", streamed.attempts)
        show("failure", streamed.failure)

    await client.aclose()
    print("=" * 78)
    print("冒烟结束：两次请求都走通了真实端点")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
