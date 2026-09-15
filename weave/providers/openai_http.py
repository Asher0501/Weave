"""OpenAIHTTPProvider —— **哑**原子：一次 HTTP 调用，失败即抛。

与旧 `OpenAICompatibleProvider` 的区别（这是"分工铁律"的落点）：
- **不重试、不计时、不分类**——那些是 `weave.llm` 对象的职责；
- **不解码**工具调用语法：`tool_calls[].function.arguments` 原样保留（字符串），
  由 `weave.llm.decode` 统一读（D13）；
- 零第三方依赖：HTTP 走可注入的 `Transport`（默认 stdlib `UrllibTransport`），
  因此协议适配可以**离线验收**（清单 A2/A3/B1–B3/F1/F2/E3）。
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from weave.core.envelopes import CallRequest
from weave.core.errors import ProviderError, classify_http_error
from weave.core.interfaces import LLMProvider
from weave.core.types import (
    ImageBlock,
    LLMResponse,
    Message,
    StreamChunk,
    TextBlock,
    ToolCall,
    ToolSchema,
    payload_content,
)
from weave.providers.sse import iter_sse_events
from weave.providers.transport import (
    HTTPResponse,
    StreamHTTPError,
    Transport,
    UrllibTransport,
    retry_after_from_headers,
)

__all__ = [
    "OpenAIHTTPProvider",
    "build_payload",
    "content_to_openai",
    "messages_to_payload",
    "tools_to_payload",
    "filter_sampling_params",
    "content_text",
    "parse_completion",
    "delta_to_chunks",
    "error_from_response",
]

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_KEY_ENVS = ("WEAVE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")

#: 这些模型不接受采样参数（DeepSeek reasoner 类），发了会被拒或忽略
_REASONER_PREFIXES = ("deepseek-reasoner", "deepseek-r1", "o1", "o3", "o4")
_SAMPLING_PARAMS = (
    "temperature", "top_p", "presence_penalty", "frequency_penalty", "logprobs",
    "top_logprobs",
)


# ── 纯函数：请求构造（A1/A2/A3） ────────────────────────


def content_to_openai(content: Any) -> str | list[dict[str, Any]]:
    """规范形态（`payload_content()` 的输出）→ OpenAI 内容槽位的值。

    - `str` → 原样；
    - **中立块** → 翻译：`TextBlock` → `text` 块；`ImageBlock` → `image_url` 块
      （base64 会被拼成 `data:{media_type};base64,{data}` 的 data URI——**这是 OpenAI 的编码习惯，
      只在这一层出现**，调用方看不到）；
    - **原样 dict** → 逐字透传。
    """
    if isinstance(content, str) or not content:
        return content
    if isinstance(content[0], dict):        # 原样块：不解释、不改写
        return content
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        else:
            url = block.url or f"data:{block.media_type};base64,{block.data}"
            out.append({"type": "image_url", "image_url": {"url": url}})
    return out


def messages_to_payload(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Message → 厂商 messages。

    - `assistant.tool_calls` 按 OpenAI 形状回传；`tool` 消息必带 `tool_call_id`（A2）
    - `Message` 结构里没有 reasoning 字段 → 推理内容天然不回传（A4）
    - `content` 是原样形态（dict / list[dict]）时**逐字透传**：weave 不看 key，
      所以 `image_url` / 厂商新块都能直接用（代价见 Message 文档）；
      `Block` / `list[Block]`（中立块）会被**翻译**成 OpenAI 形状。
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {"role": message.role,
                                "content": content_to_openai(payload_content(message))}
        if message.role == "assistant" and message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments
                        if isinstance(call.arguments, str)
                        else json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        elif message.role == "tool":
            entry["tool_call_id"] = message.tool_call_id or ""
        if message.name:
            entry["name"] = message.name
        out.append(entry)
    return out


def tools_to_payload(schemas: Sequence[ToolSchema]) -> list[dict[str, Any]] | None:
    if not schemas:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters,
            },
        }
        for schema in schemas
    ]


def filter_sampling_params(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按模型能力过滤参数（A3）：reasoner 类不接受采样参数。"""
    if not any(model.startswith(prefix) for prefix in _REASONER_PREFIXES):
        return payload
    filtered = dict(payload)
    for key in _SAMPLING_PARAMS:
        filtered.pop(key, None)
    return filtered


def build_payload(
    *,
    model: str,
    request: CallRequest,
    extra_params: dict[str, Any] | None = None,
    param_filter: bool = True,
    stream: bool = False,
    include_usage: bool = True,
) -> dict[str, Any]:
    """CallRequest → 厂商请求体（纯函数，可离线断言）。"""
    opts = dict(request.opts or {})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages_to_payload(request.messages),
        **{k: v for k, v in opts.items() if k not in ("stream",)},
    }
    tools = tools_to_payload(request.schemas)
    if tools:
        payload["tools"] = tools
    if extra_params:
        payload.update(extra_params)
    if stream:
        payload["stream"] = True
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
    return filter_sampling_params(model, payload) if param_filter else payload


# ── 纯函数：响应解析（E3 + 保留原生 tool_calls） ─────────


def vendor_error_message(body: str) -> str:
    """从厂商错误体里取人类可读信息（E3）。"""
    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        return (body or "").strip()[:500]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            parts = [str(error.get(key)) for key in ("message", "type", "code") if error.get(key)]
            if parts:
                return " | ".join(parts)
        if isinstance(error, str):
            return error
        for key in ("message", "detail", "msg"):
            if data.get(key):
                return str(data[key])
    return json.dumps(data, ensure_ascii=False)[:500]


def error_from_response(response: HTTPResponse) -> ProviderError:
    """HTTP 响应 → 类型化错误（并把 Retry-After 带上，供对象层尊重它）。"""
    message = vendor_error_message(response.body) or f"HTTP {response.status}"
    error = classify_http_error(response.status, message)
    retry_after = retry_after_from_headers(response.headers)
    if retry_after is not None:
        error.retry_after = retry_after            # type: ignore[attr-defined]
    error.detail = {"status": response.status, "body": response.body[:2000]}  # type: ignore[attr-defined]
    return error


def content_text(raw: Any) -> str:
    """OpenAI 的 `content` 可能是 str，也可能是内容块数组（多模态 / 工具返回）。

    数组形态里只取文本块拼成 `content`，**其余块不丢**——它们进 `LLMResponse.raw_blocks`。
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(
            part.get("text") or ""
            for part in raw
            if isinstance(part, dict) and part.get("type") in (None, "text", "output_text")
        )
    return ""


def parse_completion(body: str) -> LLMResponse:
    """非流式响应 → LLMResponse（**不解码**工具参数，原样保留字符串）。"""
    data = json.loads(body or "{}")
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    tool_calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        tool_calls.append(ToolCall(
            id=call.get("id") or "",
            name=function.get("name") or "",
            arguments=function.get("arguments"),      # 原样：字符串交给对象层解码（D13）
        ))

    raw_content = message.get("content")
    raw_blocks = (
        [dict(part) for part in raw_content if isinstance(part, dict)]
        if isinstance(raw_content, list) else None
    ) or None

    return LLMResponse(
        content=content_text(raw_content),
        reasoning=message.get("reasoning_content") or message.get("reasoning"),
        tool_calls=tool_calls or None,
        model=data.get("model") or "",
        usage=data.get("usage") or {},
        finish_reason=choice.get("finish_reason") or "stop",
        raw={"id": data.get("id"), "created": data.get("created")},
        raw_blocks=raw_blocks,
    )


def delta_to_chunks(data: dict[str, Any]) -> list[StreamChunk]:
    """一个 SSE data 事件 → 若干 StreamChunk（F1/F3/F4/F5/F8）。"""
    out: list[StreamChunk] = []
    model = data.get("model") or ""

    if data.get("usage"):
        out.append(StreamChunk(kind="usage", usage=data["usage"], model=model))

    for choice in data.get("choices") or []:
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            out.append(StreamChunk(kind="token", text=content, model=model))
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            out.append(StreamChunk(kind="reasoning", reasoning=reasoning, model=model))
        for call in delta.get("tool_calls") or []:
            function = call.get("function") or {}
            out.append(StreamChunk(
                kind="tool_call_delta",
                index=int(call.get("index") or 0),
                tool_call_id=call.get("id") or "",
                tool_call_name=function.get("name") or "",
                arguments_delta=function.get("arguments") or "",
                model=model,
            ))
        if choice.get("finish_reason"):
            out.append(StreamChunk(
                kind="finish", finish_reason=choice["finish_reason"], model=model
            ))
    return out


# ── 哑原子 ──────────────────────────────────────────────


class OpenAIHTTPProvider(LLMProvider):
    """经 HTTP 调用任何 OpenAI 兼容端点（OpenAI / DeepSeek / 各类网关）。"""

    #: ── 适配层自述（对象层据此做"错家形状"预检；不认识任何厂商细节）──
    protocol = "openai"
    #: 明确属于**别家**的块类型（只列对方独有的，未知块一律放行）
    foreign_block_types = frozenset({
        "image", "document", "tool_use", "tool_result", "thinking", "redacted_thinking",
        "server_tool_use", "web_search_tool_result", "mcp_tool_use", "mcp_tool_result",
        "search_result", "container_upload",
    })
    #: 本厂商正确形状的示例（只用于报错信息）
    block_shape_hint = '{"type":"image_url","image_url":{"url": …}}'

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | Callable[[], str | None] | None = None,
        api_key_env: Sequence[str] = DEFAULT_KEY_ENVS,
        headers: dict[str, str] | None = None,
        transport: Transport | None = None,
        path: str = "/chat/completions",
        timeout: float | None = None,
        extra_params: dict[str, Any] | None = None,
        param_filter: bool = True,
        include_usage: bool = True,
    ) -> None:
        if not model:
            raise ValueError("OpenAIHTTPProvider 需要 model（如 deepseek-chat）")
        self.model = model
        self.base_url = (base_url or os.environ.get("WEAVE_BASE_URL")
                         or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self._key_envs = tuple(api_key_env)
        self._headers = dict(headers or {})
        self.transport: Transport = transport or UrllibTransport()
        self.path = path if path.startswith("/") else f"/{path}"
        # 传输层超时只是"安全网"；语义超时由 weave.llm 对象负责（原子不计时）
        self.timeout = timeout
        self.extra_params = dict(extra_params or {})
        self.param_filter = param_filter
        self.include_usage = include_usage

    # ── 装配细节（B1/B2/B3） ─────────────────────────

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"

    def resolve_api_key(self) -> str | None:
        """每次请求都解析一次 key（B3：支持轮换/多 key/环境变量）。"""
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
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            **self._headers,
        }
        key = self.resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ── LLMProvider（哑：失败即抛） ───────────────────

    async def complete(self, request: CallRequest) -> LLMResponse:
        payload = build_payload(
            model=self.model, request=request, extra_params=self.extra_params,
            param_filter=self.param_filter,
        )
        response = await self.transport.post_json(
            self.url, headers=self.headers_for(stream=False), payload=payload,
            timeout=self.timeout,
        )
        if not response.ok:
            raise error_from_response(response)
        return parse_completion(response.body)

    def stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        return self._stream(request)

    async def _stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        payload = build_payload(
            model=self.model, request=request, extra_params=self.extra_params,
            param_filter=self.param_filter, stream=True, include_usage=self.include_usage,
        )
        fragments = self.transport.post_sse(
            self.url, headers=self.headers_for(stream=True), payload=payload,
            timeout=self.timeout,
        )
        try:
            async for event in iter_sse_events(fragments):
                if event.event not in ("message", ""):
                    continue
                try:
                    data = json.loads(event.data)
                except json.JSONDecodeError:
                    continue                   # 非 JSON 的 data 行直接跳过，不猜
                for chunk in delta_to_chunks(data):
                    yield chunk
        except StreamHTTPError as exc:         # 流式请求被拒：转成类型化错误（带 Retry-After）
            raise error_from_response(HTTPResponse(
                status=exc.status, body=exc.body, headers=exc.headers,
            )) from exc

    async def aclose(self) -> None:
        closer = getattr(self.transport, "aclose", None)
        if closer is not None:
            result = closer()
            if hasattr(result, "__await__"):
                await result
