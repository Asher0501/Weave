"""Prompt 文件加载与模板变量替换。

支持:
- 从 .md/.yaml/.txt 文件加载 prompt
- {{ context.xxx }} — 运行时上下文
- {{ env.VAR }} — 环境变量
- {{ config.xxx }} — weave.yaml 配置值
- {{ "default" | default("fallback") }} — 默认值
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ── 模板变量解析 ─────────────────────────────────────────

_VAR_RE = re.compile(r'\{\{\s*([^|}]+?)\s*(?:\|\s*default\(["\']([^"\']*)["\']\)\s*)?\}\}')


def interpolate(template: str, context: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    """替换 prompt 模板中的变量。

    变量来源优先级:
    1. context dict — 运行时上下文（含 scope_hints 的值）
    2. env — 环境变量
    3. config dict — weave.yaml 顶层配置

    语法:
        {{ context.key }}      — 从 context[key] 取值
        {{ env.VAR }}          — 从 os.environ 取值
        {{ config.llm.model }} — 从 config["llm"]["model"] 取值（点号递归）
        {{ key | default("x") }} — 变量不存在时使用默认值

    Args:
        template: 含模板变量的 markdown 字符串
        context: 运行时上下文 dict
        config: weave.yaml 配置 dict（可选）

    Returns:
        替换后的文本

    Raises:
        KeyError: 变量在所有来源都不存在且未指定默认值时
    """
    config = config or {}

    def _resolve(m: re.Match) -> str:
        expr = m.group(1).strip()
        default_val = m.group(2)  # None 表示无默认值

        try:
            value = _lookup(expr, context, config)
            return str(value)
        except (KeyError, TypeError, IndexError):
            if default_val is not None:
                return default_val
            raise KeyError(
                f"Prompt variable '{{{{ {expr} }}}}' not found in context, env, or config, "
                f"and no default value specified."
            )

    return _VAR_RE.sub(_resolve, template)


def _lookup(expr: str, context: dict[str, Any], config: dict[str, Any]) -> Any:
    """按优先级查找变量值。"""
    parts = expr.split(".", 1)
    namespace = parts[0]
    rest = parts[1] if len(parts) > 1 else None

    # 1. context
    if namespace == "context":
        return _nested_get(context, rest) if rest else context

    # 2. env
    if namespace == "env":
        if rest:
            val = os.environ.get(rest)
            if val is not None:
                return val
            raise KeyError(f"env.{rest}")
        raise KeyError("env")

    # 3. config
    if namespace == "config":
        return _nested_get(config, rest) if rest else config

    # 4. 直接在 context 中查找（向后兼容，也用于 scope_hints 合并到 context）
    return _nested_get(context, expr)


def _nested_get(d: dict[str, Any], path: str) -> Any:
    """点号分隔的嵌套 dict 访问。"""
    parts = path.split(".")
    current: Any = d
    for part in parts:
        if isinstance(current, dict):
            current = current[part]
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            raise KeyError(path)
    return current


# ── 文件加载 ─────────────────────────────────────────────


def load_prompt(file_path: str | Path, context: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> str:
    """从文件加载 prompt 并替换变量。

    Args:
        file_path: .md/.yaml/.txt 文件路径
        context: 运行时注入的变量
        config: weave.yaml 配置

    Returns:
        替换变量后的 prompt 文本
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    template = path.read_text(encoding="utf-8")
    return interpolate(template, context or {}, config)


def load_prompt_raw(file_path: str | Path) -> str:
    """从文件加载 prompt，不做变量替换。

    Args:
        file_path: .md 文件路径

    Returns:
        原始 prompt 文本
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


# ── Prompt Schema 支持 ────────────────────────────────────

# Schema 文件后缀（.schema.yaml 或 .schema.json）
_SCHEMA_SUFFIXES = (".schema.yaml", ".schema.yml", ".schema.json")


def is_schema_path(file_path: str) -> bool:
    """判断路径是否指向一个 Prompt Schema 文件。"""
    return any(file_path.endswith(suf) for suf in _SCHEMA_SUFFIXES)


def load_prompt_schema(
    schema_path: str | Path,
    variables: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> str:
    """加载 Prompt Schema 并渲染为完整 Prompt。

    Schema 文件可以是 .schema.yaml 或 .schema.json，描述 Prompt 的 composition 规则。
    变量驱动组装过程（switch 分支选择、template 渲染）。

    兼容性:
    - 如果 schema_path 不是 schema 文件 → 当作普通 prompt 文件，直接 load_prompt()
    - 如果是 schema 文件 → 解析 composition 并渲染

    Args:
        schema_path: .schema.yaml / .schema.json 文件路径
        variables: 运行时变量（用于 switch + template）
        config: weave.yaml 配置

    Returns:
        完整渲染的 Prompt 字符串
    """
    from weave_agent_sdk.prompts.schema import parse_schema, render_schema

    path = Path(schema_path)

    if not is_schema_path(str(path)):
        # 向下兼容：普通 prompt 文件
        return load_prompt(path, variables, config)

    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")

    # 解析 YAML 或 JSON
    if str(path).endswith(".json"):
        import json
        raw = json.loads(raw_text)
    else:
        import yaml
        raw = yaml.safe_load(raw_text)

    if not raw:
        return ""

    schema = parse_schema(raw)
    base_dir = path.parent  # schema 中的相对路径相对于 schema 文件所在目录
    return render_schema(schema, variables, base_dir=base_dir, config=config)
