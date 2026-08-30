"""Anthropic SDK 适配器。

注意：Anthropic 要求同一条 assistant 消息的所有 tool_use 结果
打包到**同一条** user 消息的多个 content block 中（非 OpenAI 的独立消息格式）。
格式转换由本 Adapter 内部消化，详见 docs/issues/004-llm-adapter-protocol.md。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from weave.llm.base import BaseLLM, LLMResponse
from weave.llm.errors import (
    WeaveLLMError,
    classify_http_error,
)
from weave.types import Message, ToolCall


def _merge_consecutive_tool_messages(messages: list[Message]) -> list[Message]:
    """将连续的 role="tool" 消息合并为一条 role="tool" 消息。

    这是 Anthropic 专有的预处理——同一轮 tool 调用的结果必须在同一条 user 消息中。
    OpenAI/DeepSeek 不需要此处理。
    """
    if not messages:
        return messages

    merged: list[Message] = []
    pending_tool: list[Message] = []

    for msg in messages:
        if msg.role == "tool":
            pending_tool.append(msg)
        else:
            # 非 tool 消息——先 flush 待处理的 tool 消息
            if pending_tool:
                merged.append(_merge_tool_group(pending_tool))
                pending_tool = []
            merged.append(msg)

    # 文件末尾的 tool 消息
    if pending_tool:
        merged.append(_merge_tool_group(pending_tool))

    return merged


def _merge_tool_group(tool_msgs: list[Message]) -> Message:
    """将一组 tool 消息合并为一条。"""
    import json

    if len(tool_msgs) == 1:
        return tool_msgs[0]

    tool_results = [
        {"tool_call_id": m.tool_call_id or "", "content": m.content}
        for m in tool_msgs
    ]
    return Message(
        role="tool",
        content=json.dumps(tool_results, ensure_ascii=False),
        tool_call_id="__merged__",
    )


def _unpack_tool_results(msg: Message) -> list[dict[str, str]]:
    """解包 tool 消息（可能是合并后的多 tool result）。

    返回 [{"tool_call_id": "...", "content": "..."}, ...]
    """
    import json

    if msg.tool_call_id == "__merged__":
        try:
            return json.loads(msg.content)
        except json.JSONDecodeError:
            return [{"tool_call_id": "", "content": msg.content}]
    return [{"tool_call_id": msg.tool_call_id or "", "content": msg.content}]


class AnthropicAdapter(BaseLLM):
    """通过 Anthropic Python SDK 调用 Claude 模型。

    模型名由调用方传入，代码中不硬编码（R3）。
    """

    def __init__(
        self,
        api_key: str | None,
        model: str = "",
        base_url: str | None = None,
        auth_token: str | None = None,
    ):
        if not model:
            raise ValueError(
                "model must be specified for AnthropicAdapter. "
                "Set model in weave.yaml or WEAVE_MODEL environment variable."
            )
        self._api_key = api_key
        self._auth_token = auth_token
        self._model = model
        self._base_url = base_url

    def _client_kwargs(self) -> dict[str, Any]:
        """构建 Anthropic 客户端参数：区分 api_key 与 auth_token（docs/issues/011）。"""
        kwargs: dict[str, Any] = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._auth_token:
            kwargs["auth_token"] = self._auth_token
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return kwargs

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package required. Install with: pip install anthropic"
            )

        client = anthropic.AsyncAnthropic(**self._client_kwargs())

        # 预处理：合并连续的 tool 消息（Anthropic 协议要求）
        messages = _merge_consecutive_tool_messages(messages)

        # 构建 Anthropic 消息格式
        system_prompt = ""
        anthropic_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                system_prompt += msg.content + "\n"
            elif msg.role == "tool":
                # 可能是合并后的多 tool result 消息
                tool_results = _unpack_tool_results(msg)
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tr["tool_call_id"], "content": tr["content"]}
                        for tr in tool_results
                    ]
                })
            elif msg.role in ("user", "assistant"):
                entry: dict[str, Any] = {"role": msg.role}
                # assistant 消息携带 tool_calls 时，按 Anthropic 格式重建 content blocks
                if msg.role == "assistant" and msg.tool_calls:
                    content_blocks: list[dict[str, Any]] = []
                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})
                    for tc in msg.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                    entry["content"] = content_blocks
                else:
                    entry["content"] = msg.content
                anthropic_messages.append(entry)

        tool_schemas = None
        if tools:
            tool_schemas = _convert_tools_to_anthropic(tools)

        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt.strip(),
                messages=anthropic_messages,
                tools=tool_schemas,
            )
        except anthropic.APIStatusError as e:
            raise classify_http_error(e.status_code, str(e))
        except anthropic.APIConnectionError as e:
            from weave.llm.errors import NetworkError
            raise NetworkError(str(e))
        except Exception as e:
            raise WeaveLLMError(f"Anthropic API error: {e}")

        return _parse_anthropic_response(response)

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        client = anthropic.AsyncAnthropic(**self._client_kwargs())

        # 预处理：合并连续的 tool 消息（Anthropic 协议要求）
        messages = _merge_consecutive_tool_messages(messages)

        # 构建 Anthropic 消息格式（与 chat() 一致，支持 system / tool / user / assistant）
        system_prompt = ""
        anthropic_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                system_prompt += msg.content + "\n"
            elif msg.role == "tool":
                # 可能是合并后的多 tool result 消息
                tool_results = _unpack_tool_results(msg)
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tr["tool_call_id"], "content": tr["content"]}
                        for tr in tool_results
                    ]
                })
            elif msg.role in ("user", "assistant"):
                entry: dict[str, Any] = {"role": msg.role}
                # assistant 消息携带 tool_calls 时，按 Anthropic 格式重建 content blocks
                if msg.role == "assistant" and msg.tool_calls:
                    content_blocks: list[dict[str, Any]] = []
                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})
                    for tc in msg.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                    entry["content"] = content_blocks
                else:
                    entry["content"] = msg.content
                anthropic_messages.append(entry)

        # 转换 tool schemas（与 chat() 一致）
        tool_schemas = None
        if tools:
            tool_schemas = _convert_tools_to_anthropic(tools)

        try:
            async with client.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt.strip(),
                messages=anthropic_messages,
                tools=tool_schemas,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIStatusError as e:
            raise classify_http_error(e.status_code, str(e))
        except anthropic.APIConnectionError as e:
            from weave.llm.errors import NetworkError
            raise NetworkError(str(e))
        except Exception as e:
            raise WeaveLLMError(f"Anthropic API error: {e}")


def _convert_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 OpenAI 风格的 tool JSON Schema 转为 Anthropic 格式。

    支持两种输入格式：
    - OpenAI 风格：{"function": {"name": ..., "description": ..., "parameters": ...}}
    - 扁平风格：{"name": ..., "description": ..., "parameters": ...}
    """
    result = []
    for tool in tools:
        fn = tool.get("function")
        if isinstance(fn, dict):
            # function 包裹格式（OpenAI 风格）
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            })
        else:
            # 扁平格式（Anthropic 风格或自定义），直接取顶层字段
            result.append({
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", tool.get("input_schema", {})),
            })
    return result


def _parse_anthropic_response(response: Any) -> LLMResponse:
    """解析 Anthropic API 响应为 LLMResponse。"""
    content_blocks = response.content if hasattr(response, "content") else []

    text_content = ""
    thinking_content = ""
    tool_calls: list[ToolCall] = []

    for block in content_blocks:
        block_type = getattr(block, "type", "text")
        if block_type == "text":
            text_content += getattr(block, "text", "")
        elif block_type in ("thinking", "reasoning"):
            # 推理模型的思考内容：正文为空时作为兜底输出，避免静默返回空（docs/issues/011）
            thinking_content += (
                getattr(block, "thinking", "")
                or getattr(block, "text", "")
                or getattr(block, "content", "")
            )
        elif block_type == "tool_use":
            tool_calls.append(ToolCall(
                id=getattr(block, "id", ""),
                name=getattr(block, "name", ""),
                arguments=getattr(block, "input", {}),
            ))

    # 推理模型可能只产出 thinking 而无 text（如被 max_tokens 截断）：回退用 thinking，
    # 保证输出非空（docs/issues/011）。
    if not text_content and thinking_content:
        text_content = thinking_content

    usage = {}
    if hasattr(response, "usage"):
        usage = {
            "input": getattr(response.usage, "input_tokens", 0),
            "output": getattr(response.usage, "output_tokens", 0),
        }

    finish_reason = "stop"
    if tool_calls:
        finish_reason = "tool_calls"
    if hasattr(response, "stop_reason"):
        if response.stop_reason == "max_tokens":
            finish_reason = "length"

    return LLMResponse(
        content=text_content,
        tool_calls=tool_calls or None,
        model=getattr(response, "model", ""),
        usage=usage,
        finish_reason=finish_reason,
    )
