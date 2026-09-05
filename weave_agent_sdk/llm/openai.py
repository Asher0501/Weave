"""OpenAI 兼容 SDK 适配器。"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from weave_agent_sdk.llm.base import BaseLLM, LLMResponse
from weave_agent_sdk.llm.errors import (
    WeaveLLMError,
    classify_http_error,
)
from weave_agent_sdk.types import Message, ToolCall


class OpenAIAdapter(BaseLLM):
    """通过 OpenAI Python SDK 调用模型（含 DeepSeek、通义千问等兼容 API）。

    模型名由调用方传入，代码中不硬编码（R3）。
    """

    def __init__(self, api_key: str, model: str = "", base_url: str | None = None):
        if not model:
            raise ValueError(
                "model must be specified for OpenAIAdapter. "
                "Set model in weave.yaml or WEAVE_MODEL environment variable."
            )
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package required. Install with: pip install openai"
            )

        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url

        client = openai.AsyncOpenAI(**client_kwargs)

        openai_messages: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.role == "assistant" and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            elif msg.role == "tool":
                entry["tool_call_id"] = msg.tool_call_id or ""
            if msg.name:
                entry["name"] = msg.name
            openai_messages.append(entry)

        tool_schemas = None
        if tools:
            tool_schemas = _convert_tools_to_openai(tools)

        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=openai_messages,
                tools=tool_schemas,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except openai.APIStatusError as e:
            raise classify_http_error(e.status_code, str(e))
        except openai.APIConnectionError as e:
            from weave_agent_sdk.llm.errors import NetworkError
            raise NetworkError(str(e))
        except Exception as e:
            raise WeaveLLMError(f"OpenAI API error: {e}")

        return _parse_openai_response(response)

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        try:
            import openai
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url

        client = openai.AsyncOpenAI(**client_kwargs)

        openai_messages: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.role == "assistant" and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            elif msg.role == "tool":
                entry["tool_call_id"] = msg.tool_call_id or ""
            if msg.name:
                entry["name"] = msg.name
            openai_messages.append(entry)

        # 转换 tool schemas（与 chat() 一致）
        tool_schemas = None
        if tools:
            tool_schemas = _convert_tools_to_openai(tools)

        try:
            stream = await client.chat.completions.create(
                model=self._model,
                messages=openai_messages,
                tools=tool_schemas,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
        except openai.APIStatusError as e:
            raise classify_http_error(e.status_code, str(e))
        except openai.APIConnectionError as e:
            from weave_agent_sdk.llm.errors import NetworkError
            raise NetworkError(str(e))
        except Exception as e:
            raise WeaveLLMError(f"OpenAI API error: {e}")


def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确保 tool schema 是 OpenAI 格式（{type: "function", function: {name, description, parameters}}）。"""
    result = []
    for tool in tools:
        if "function" in tool:
            result.append({"type": "function", "function": tool["function"]})
        else:
            result.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", tool.get("input_schema", {})),
                }
            })
    return result


def _parse_openai_response(response: Any) -> LLMResponse:
    """解析 OpenAI API 响应为 LLMResponse。

    优先使用原生 tool_calls；为空时 fallback 到 DSML 文本解析
    （DeepSeek V4 Pro 的 tool call 可能以 DSML 格式嵌在 content 中）。
    """
    choice = response.choices[0] if response.choices else None
    if not choice:
        return LLMResponse(content="", model=response.model, finish_reason="stop")

    message = choice.message
    text_content = message.content or ""

    tool_calls: list[ToolCall] = []
    if message.tool_calls:
        for tc in message.tool_calls:
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments) if tc.function.arguments else {},
            ))

    # DSML fallback：原生 tool_calls 为空但 content 包含 DSML 标记时，
    # 从文本中解析 tool call 并清除 DSML 标记（DeepSeek V4 Pro）
    if not tool_calls and text_content:
        from weave_agent_sdk.llm.dsml_parser import has_dsml, parse_dsml_tool_calls
        if has_dsml(text_content):
            tool_calls, text_content = parse_dsml_tool_calls(text_content)

    usage = {}
    if hasattr(response, "usage") and response.usage:
        usage = {
            "input": response.usage.prompt_tokens or 0,
            "output": response.usage.completion_tokens or 0,
        }

    return LLMResponse(
        content=text_content,
        tool_calls=tool_calls or None,
        model=response.model or "",
        usage=usage,
        finish_reason=choice.finish_reason or "stop",
    )
