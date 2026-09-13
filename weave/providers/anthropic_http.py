"""AnthropicHTTPProvider —— Anthropic **原生协议**的哑原子。

和 `OpenAIHTTPProvider` 的差别（协议层面，不是风格问题）：

| | OpenAI 兼容 | Anthropic 原生 |
|---|---|---|
| 端点 | `POST /v1/chat/completions` | `POST /v1/messages` |
| system | 作为一条 message | **顶层 `system` 参数** |
| tool 结果 | `role="tool"` 消息 | `user` 消息里的 `tool_result` **块** |
| tool 调用 | `tool_calls[].function.arguments`（JSON 字符串） | `content[].tool_use.input`（**已是对象**） |
| 必填 | — | **`max_tokens` 必填** |
| 认证 | `Authorization: Bearer` | **`x-api-key` + `anthropic-version`** |
| 流式 | `choices[0].delta` | 事件流（`content_block_delta` / `input_json_delta` / `message_delta`…） |

哑原子纪律与 OpenAI 版一致：**一次调用、失败即抛、不重试、不计时、不解码**。
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from weave.core.envelopes import CallRequest
from weave.core.errors import ProviderError, classify_http_error
from weave.core.interfaces import LLMProvider
from weave.core.types import LLMResponse, Message, StreamChunk, ToolCall, ToolSchema, payload_content
from weave.providers.sse import iter_sse_events
from weave.providers.transport import (
    HTTPResponse,
    StreamHTTPError,
    Transport,
    UrllibTransport,
    retry_after_from_headers,
)

__all__ = [
    "AnthropicHTTPProvider",
    "build_anthropic_payload",
    "messages_to_anthropic",
    "join_system",
    "tools_to_anthropic",
    "parse_anthropic_message",
    "anthropic_event_to_chunks",
    "error_from_anthropic_response",
]

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_KEY_ENVS = ("WEAVE_API_KEY", "ANTHROPIC_API_KEY")

#: Anthropic 不认识的 OpenAI 专有参数（发了会被拒或忽略）
_UNSUPPORTED = (
    "frequency_penalty", "presence_penalty", "logprobs", "top_logprobs",
    "stream_options", "response_format", "seed", "n", "logit_bias", "user",
)
_STOP_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "pause_turn": "stop",
    "refusal": "stop",
}


# ── 纯函数：请求构造 ────────────────────────────────────


def messages_to_anthropic(
    messages: Sequence[Message],
) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """`Message[]` → `(system, messages)`。

    四处协议差异都在这里处理：
    1. `system` 抽出来当顶层参数（多条 str 用空行拼；**任一条是原样块数组时整体用块数组**，
       这样长 system 上打 `cache_control` 缓存断点也表达得出来）；
    2. `assistant.tool_calls` → `tool_use` 内容块；
    3. `role="tool"` → `user` 消息里的 `tool_result` 块，且**相邻的合并进同一条 user 消息**
       （Anthropic 要求 tool_result 跟在 tool_use 之后，不能拆成多条 user）；
    4. `content` 是原样形态（dict / list[dict]）时**逐字透传**，weave 不看 key。
    """
    raw_system: list[Any] = []
    out: list[dict[str, Any]] = []

    def push(message: dict[str, Any]) -> None:
        # 同角色相邻消息合并（Anthropic 不喜欢连续同角色）
        if out and out[-1]["role"] == message["role"]:
            previous = out[-1]["content"]
            merged = (previous if isinstance(previous, list) else
                      ([{"type": "text", "text": previous}] if previous else []))
            addition = (message["content"] if isinstance(message["content"], list) else
                        ([{"type": "text", "text": message["content"]}] if message["content"] else []))
            out[-1]["content"] = [*merged, *addition]
            return
        out.append(message)

    for message in messages:
        content = payload_content(message)      # 原样形态在这里归一；歧义在这里报错
        if message.role == "system":
            if content:
                raw_system.append(content)
            continue
        if message.role == "tool":
            push({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": content,          # str，或原样块数组（工具返回图片等）
                }],
            })
            continue
        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in message.tool_calls:
                arguments = call.arguments
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {"value": arguments}
                blocks.append({
                    "type": "tool_use",
                    "id": call.id or "",
                    "name": call.name,
                    "input": arguments if isinstance(arguments, dict) else {"value": arguments},
                })
            push({"role": "assistant", "content": blocks})
            continue
        push({"role": message.role, "content": content})

    return join_system(raw_system), out


def join_system(parts: Sequence[Any]) -> str | list[dict[str, Any]] | None:
    """多条 system 合一条顶层参数：全是 str → 空行拼接（老行为）；出现原样块 → 块数组。"""
    if not parts:
        return None
    if all(isinstance(part, str) for part in parts):
        return "\n\n".join(parts)
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
        else:
            blocks.extend(part)
    return blocks


def tools_to_anthropic(schemas: Sequence[ToolSchema]) -> list[dict[str, Any]] | None:
    if not schemas:
        return None
    return [
        {
            "name": schema.name,
            "description": schema.description,
            "input_schema": schema.parameters or {"type": "object", "properties": {}},
        }
        for schema in schemas
    ]


def _tool_choice(value: Any) -> dict[str, Any] | None:
    if value in (None, "auto"):
        return {"type": "auto"} if value == "auto" else None
    if value == "required":
        return {"type": "any"}
    if isinstance(value, dict) and value.get("name"):
        return {"type": "tool", "name": value["name"]}
    return None


def build_anthropic_payload(
    *,
    model: str,
    request: CallRequest,
    extra_params: dict[str, Any] | None = None,
    param_filter: bool = True,
    stream: bool = False,
    default_max_tokens: int = 4096,
) -> dict[str, Any]:
    """CallRequest → Anthropic 请求体（纯函数，可离线断言）。"""
    system, messages = messages_to_anthropic(request.messages)
    opts = dict(request.opts or {})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        # Anthropic 必填：没给就用默认
        "max_tokens": int(opts.pop("max_tokens", default_max_tokens)),
    }
    if system:
        payload["system"] = system

    if param_filter:
        for key in _UNSUPPORTED:
            opts.pop(key, None)
    stop = opts.pop("stop", None)
    if stop is not None:
        payload["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    choice = _tool_choice(opts.pop("tool_choice", None))
    if choice is not None:
        payload["tool_choice"] = choice

    payload.update(opts)                     # temperature / top_p / metadata / thinking …
    if extra_params:
        payload.update(extra_params)
    tools = tools_to_anthropic(request.schemas)
    if tools:
        payload["tools"] = tools
    if stream:
        payload["stream"] = True
    return payload


# ── 纯函数：响应解析 ────────────────────────────────────


def parse_anthropic_message(body: str) -> LLMResponse:
    """非流式响应 → LLMResponse（`tool_use.input` 已是对象，不做解码）。

    **不丢块**：text/thinking/tool_use 之外的块（多模态、`server_tool_use` …）原样留在
    `LLMResponse.raw_blocks` 里，回填成下一轮的 `Message.content` 即可无损续接。
    """
    data = json.loads(body or "{}")
    blocks = data.get("content") or []
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    thinking = "".join(
        b.get("thinking") or "" for b in blocks if b.get("type") in ("thinking", "redacted_thinking")
    )
    tool_calls = [
        ToolCall(id=b.get("id") or "", name=b.get("name") or "", arguments=b.get("input") or {})
        for b in blocks if b.get("type") == "tool_use"
    ]
    stop_reason = data.get("stop_reason")
    kept = [dict(b) for b in blocks if isinstance(b, dict)]
    return LLMResponse(
        content=text,
        reasoning=thinking or None,
        tool_calls=tool_calls or None,
        model=data.get("model") or "",
        usage=data.get("usage") or {},        # 字段已是 input_tokens / output_tokens，对象层会归一
        finish_reason=_STOP_REASON.get(stop_reason, stop_reason or "stop"),
        raw={"id": data.get("id"), "type": data.get("type")},
        raw_blocks=kept or None,
    )


def error_from_anthropic_response(response: HTTPResponse) -> ProviderError:
    """Anthropic 错误体形如 `{"type":"error","error":{"type","message"}}`。"""
    message = ""
    try:
        data = json.loads(response.body or "{}")
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = " | ".join(str(error.get(k)) for k in ("type", "message") if error.get(k))
        elif isinstance(error, str):
            message = error
        elif isinstance(data, dict) and data.get("message"):
            message = str(data["message"])
    except json.JSONDecodeError:
        message = (response.body or "").strip()[:500]
    error = classify_http_error(response.status, message or f"HTTP {response.status}")
    retry_after = retry_after_from_headers(response.headers)
    if retry_after is not None:
        error.retry_after = retry_after               # type: ignore[attr-defined]
    error.detail = {"status": response.status, "body": response.body[:2000]}  # type: ignore[attr-defined]
    return error


# ── 流式事件 → StreamChunk ──────────────────────────────


class _ToolBlockIndex:
    """Anthropic 的 content block 索引混着 text/thinking/tool_use；
    我们的 StreamChunk 要求 tool_call 的 index 是**密集**的 0..n-1，这里做映射。"""

    def __init__(self) -> None:
        self._mapping: dict[int, int] = {}

    def resolve(self, block_index: int) -> int:
        if block_index not in self._mapping:
            self._mapping[block_index] = len(self._mapping)
        return self._mapping[block_index]


def anthropic_event_to_chunks(
    event: str, data: dict[str, Any], blocks: _ToolBlockIndex | None = None
) -> list[StreamChunk]:
    """一个 Anthropic SSE 事件 → 若干 StreamChunk。"""
    blocks = blocks or _ToolBlockIndex()
    kind = data.get("type") or event
    model = (data.get("message") or {}).get("model") or data.get("model") or ""
    out: list[StreamChunk] = []

    if kind == "message_start":
        usage = (data.get("message") or {}).get("usage") or {}
        if usage.get("input_tokens") is not None:
            out.append(StreamChunk(kind="usage", usage={"input": usage["input_tokens"]}, model=model))
    elif kind == "content_block_start":
        block = data.get("content_block") or {}
        if block.get("type") == "tool_use":
            out.append(StreamChunk(
                kind="tool_call_delta",
                index=blocks.resolve(int(data.get("index") or 0)),
                tool_call_id=block.get("id") or "",
                tool_call_name=block.get("name") or "",
                model=model,
            ))
    elif kind == "content_block_delta":
        delta = data.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta" and delta.get("text"):
            out.append(StreamChunk(kind="token", text=delta["text"], model=model))
        elif delta_type == "thinking_delta" and delta.get("thinking"):
            out.append(StreamChunk(kind="reasoning", reasoning=delta["thinking"], model=model))
        elif delta_type == "input_json_delta" and delta.get("partial_json"):
            out.append(StreamChunk(
                kind="tool_call_delta",
                index=blocks.resolve(int(data.get("index") or 0)),
                arguments_delta=delta["partial_json"],
                model=model,
            ))
    elif kind == "message_delta":
        usage = data.get("usage") or {}
        if usage.get("output_tokens") is not None:
            out.append(StreamChunk(kind="usage", usage={"output": usage["output_tokens"]}, model=model))
        stop_reason = (data.get("delta") or {}).get("stop_reason")
        if stop_reason:
            out.append(StreamChunk(
                kind="finish", finish_reason=_STOP_REASON.get(stop_reason, stop_reason), model=model
            ))
        elif usage:
            out.append(StreamChunk(kind="finish", model=model))
    elif kind == "error":
        error = data.get("error") or {}
        raise classify_http_error(500, f"{error.get('type')}: {error.get('message')}")
    return out


# ── 哑原子 ──────────────────────────────────────────────


class AnthropicHTTPProvider(LLMProvider):
    """经 HTTP 调用 Anthropic 原生 Messages API（含兼容端点）。"""

    #: ── 适配层自述（对象层据此做"错家形状"预检；不认识任何厂商细节）──
    protocol = "anthropic"
    #: 明确属于**别家**的块类型（只列对方独有的，未知块一律放行）
    foreign_block_types = frozenset({
        "image_url", "input_audio", "input_text", "input_image", "output_text",
    })
    #: 本厂商正确形状的示例（只用于报错信息）
    block_shape_hint = '{"type":"image","source":{"type":"base64","media_type":…,"data":…}}'

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | Callable[[], str | None] | None = None,
        api_key_env: Sequence[str] = DEFAULT_KEY_ENVS,
        headers: dict[str, str] | None = None,
        transport: Transport | None = None,
        path: str = "/v1/messages",
        timeout: float | None = None,
        extra_params: dict[str, Any] | None = None,
        param_filter: bool = True,
        api_version: str = ANTHROPIC_VERSION,
        default_max_tokens: int = 4096,
    ) -> None:
        if not model:
            raise ValueError("AnthropicHTTPProvider 需要 model（如 claude-sonnet-4-20250514）")
        self.model = model
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self._key_envs = tuple(api_key_env)
        self._headers = dict(headers or {})
        self.transport: Transport = transport or UrllibTransport()
        self.path = path if path.startswith("/") else f"/{path}"
        self.timeout = timeout                   # 传输层安全网；语义超时在对象层
        self.extra_params = dict(extra_params or {})
        self.param_filter = param_filter
        self.api_version = api_version
        self.default_max_tokens = default_max_tokens

    # ── 装配细节 ─────────────────────────────────────

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"

    def resolve_api_key(self) -> str | None:
        key = self._api_key() if callable(self._api_key) else self._api_key
        if key:
            return key
        for name in self._key_envs:
            value = os.environ.get(name)
            if value:
                return value
        return None

    def headers_for(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.api_version,
            "accept": "text/event-stream" if stream else "application/json",
            **self._headers,
        }
        key = self.resolve_api_key()
        if key:
            headers["x-api-key"] = key
        return headers

    # ── LLMProvider（哑：失败即抛） ───────────────────

    async def complete(self, request: CallRequest) -> LLMResponse:
        payload = build_anthropic_payload(
            model=self.model, request=request, extra_params=self.extra_params,
            param_filter=self.param_filter, default_max_tokens=self.default_max_tokens,
        )
        response = await self.transport.post_json(
            self.url, headers=self.headers_for(stream=False), payload=payload,
            timeout=self.timeout,
        )
        if not response.ok:
            raise error_from_anthropic_response(response)
        return parse_anthropic_message(response.body)

    def stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        return self._stream(request)

    async def _stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        payload = build_anthropic_payload(
            model=self.model, request=request, extra_params=self.extra_params,
            param_filter=self.param_filter, stream=True,
            default_max_tokens=self.default_max_tokens,
        )
        fragments = self.transport.post_sse(
            self.url, headers=self.headers_for(stream=True), payload=payload,
            timeout=self.timeout,
        )
        blocks = _ToolBlockIndex()
        try:
            async for event in iter_sse_events(fragments):
                try:
                    data = json.loads(event.data)
                except json.JSONDecodeError:
                    continue                     # 非 JSON 的 data 行跳过，不猜
                for chunk in anthropic_event_to_chunks(event.event, data, blocks):
                    yield chunk
        except StreamHTTPError as exc:
            raise error_from_anthropic_response(HTTPResponse(
                status=exc.status, body=exc.body, headers=exc.headers,
            )) from exc

    async def aclose(self) -> None:
        closer = getattr(self.transport, "aclose", None)
        if closer is not None:
            result = closer()
            if hasattr(result, "__await__"):
                await result
