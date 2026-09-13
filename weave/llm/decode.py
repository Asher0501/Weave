"""输出解码归一（清单 G1–G6）：把模型的回答**读对**。

为什么这属于 LLM 交互维度：它是"读懂模型说的话"，不是"执行动作"。
不同厂商/不同版本把工具调用放在不同地方——
- 原生 `tool_calls`（OpenAI 兼容）
- 文本里的 ```` ```json ```` 代码块
- 文本里的 DSML（`<｜DSML｜tool_calls>`，DeepSeek 某些版本）

上层（应用）看到的永远是同一个形状：`list[Invocation]`。
纯函数：不做 IO、不调模型、不改动输入。
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from weave.core.envelopes import Invocation, TypedFailure
from weave.core.errors import ParseError
from weave.core.types import LLMResponse, ToolSchema

__all__ = [
    "DecodeResult",
    "NativeDecoder",
    "JsonBlockDecoder",
    "DsmlDecoder",
    "AutoDecoder",
    "decode_tool_calls",
    "has_dsml",
]


@dataclass(slots=True)
class DecodeResult:
    """解码结果：调用列表 + 可选的解码失败（G4 不是失败，G5 才是）。"""

    invocations: list[Invocation] = field(default_factory=list)
    failure: TypedFailure | None = None
    used: str = ""          # 哪个解码器命中的，"native" / "json" / "dsml" / ""


# ── 原生 tool_calls ─────────────────────────────────────


class NativeDecoder:
    """厂商原生 `tool_calls` → Invocation（最常见路径）。"""

    name = "native"

    def decode(self, response: LLMResponse) -> list[Invocation]:
        out: list[Invocation] = []
        for call in response.tool_calls or []:
            args = call.arguments
            if isinstance(args, str):        # 少数厂商把 arguments 给成 JSON 字符串
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError as exc:      # G5：结构损坏 → parse_error
                    raise ParseError(
                        f"tool_call {call.name!r} 的 arguments 不是合法 JSON: {exc}"
                    ) from exc
            if not isinstance(args, dict):
                raise ParseError(f"tool_call {call.name!r} 的 arguments 不是对象")
            out.append(Invocation(name=call.name, arguments=args, id=call.id))
        return out


# ── ```json 代码块 ──────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class JsonBlockDecoder:
    """文本里的 JSON 代码块 → Invocation。

    接受三种形状：`{name, arguments}` / `{tool_calls: [...]}` / `[{...}, ...]`
    """

    name = "json"

    def decode(self, response: LLMResponse) -> list[Invocation]:
        out: list[Invocation] = []
        for block in _FENCE_RE.findall(response.content or ""):
            try:
                data = json.loads(block.strip())
            except json.JSONDecodeError:
                continue                     # 不是调用块，留给下一个解码器（G4）
            out.extend(self._to_invocations(data))
        return out

    @staticmethod
    def _to_invocations(data: Any) -> list[Invocation]:
        if isinstance(data, dict):
            items = data.get("tool_calls") if "tool_calls" in data else [data]
        elif isinstance(data, list):
            items = data
        else:
            return []
        out: list[Invocation] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            args = item.get("arguments", item.get("args", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {"value": args}
            if not isinstance(args, dict):
                args = {"value": args}
            out.append(Invocation(
                name=str(item["name"]),
                arguments=args,
                id=item.get("id"),
                raw=json.dumps(item, ensure_ascii=False),
            ))
        return out


# ── DSML ────────────────────────────────────────────────

_DSML_SEP = r"[｜|]"        # 全角或半角竖线
_TOOL_CALLS_BLOCK_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}tool_calls>\s*(.*?)\s*</{_DSML_SEP}DSML{_DSML_SEP}tool_calls>",
    re.DOTALL,
)
_INVOKE_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}invoke\s+name=\"([^\"]+)\">\s*(.*?)"
    rf"\s*</{_DSML_SEP}DSML{_DSML_SEP}invoke>",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}parameter\s+name=\"([^\"]+)\"\s+string=\"(true|false)\">"
    rf"\s*(.*?)\s*</{_DSML_SEP}DSML{_DSML_SEP}parameter>",
    re.DOTALL,
)


def has_dsml(text: str) -> bool:
    return bool(_TOOL_CALLS_BLOCK_RE.search(text or ""))


class DsmlDecoder:
    """DSML 文本 → Invocation（含全角/半角竖线）。"""

    name = "dsml"

    def decode(self, response: LLMResponse) -> list[Invocation]:
        block = _TOOL_CALLS_BLOCK_RE.search(response.content or "")
        if not block:
            return []
        out: list[Invocation] = []
        for match in _INVOKE_RE.finditer(block.group(1)):
            arguments: dict[str, Any] = {}
            for param in _PARAM_RE.finditer(match.group(2)):
                key, is_string, raw = param.group(1), param.group(2), param.group(3).strip()
                if is_string == "true":
                    arguments[key] = raw
                else:
                    try:
                        arguments[key] = json.loads(raw)
                    except json.JSONDecodeError:
                        arguments[key] = raw     # 非 JSON 时退回字符串，不丢参数
            out.append(Invocation(
                name=match.group(1),
                arguments=arguments,
                id=f"dsml_{uuid.uuid4().hex[:12]}",
                raw=match.group(0),
            ))
        return out


# ── 组合 ────────────────────────────────────────────────


class AutoDecoder:
    """依次尝试：原生 → ```json → DSML（G1/G2/G3）。

    `strict=True` 时，原生结构损坏（G5）直接报 `parse_error`；
    `strict=False` 时降级为"跳过这条"，继续尝试下一个解码器。
    """

    name = "auto"

    def __init__(self, decoders: Sequence[Any] | None = None, *, strict: bool = True) -> None:
        self._decoders = list(decoders or [NativeDecoder(), JsonBlockDecoder(), DsmlDecoder()])
        self.strict = strict

    def decode(self, response: LLMResponse) -> DecodeResult:
        for decoder in self._decoders:
            try:
                found = decoder.decode(response)
            except ParseError as exc:
                if self.strict:
                    return DecodeResult(failure=TypedFailure(
                        kind="parse_error", message=str(exc), origin="llm.decode"
                    ))
                continue
            if found:
                return DecodeResult(invocations=found, used=decoder.name)
        return DecodeResult()               # G4：解不出来不是错误


def decode_tool_calls(response: LLMResponse, schemas: Sequence[ToolSchema] = (), *,
                      decoder: AutoDecoder | None = None) -> DecodeResult:
    """统一入口。`schemas` 只用于将来做参数校验，解码本身不依赖它。"""
    return (decoder or AutoDecoder()).decode(response)
