"""Prompt Composition Schema — 声明式 Prompt 组装。

Schema 描述 Prompt 由哪些部分组成、以什么规则组装。
变量驱动组装过程（选择哪个文件、拼哪些文本），不做参数校验。

还支持声明式工具选择：通过 tools: 字段声明该 Schema 需要的工具集，
Weave 运行时会自动过滤，LLM 只看到声明的工具。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weave_agent_sdk.prompts.loader import load_prompt, interpolate


# ── Part Types ───────────────────────────────────────────

@dataclass
class FilePart:
    """固定文件部分 — 永远加载该文件。"""
    file: str
    description: str = ""


@dataclass
class TemplatePart:
    """行内模板部分 — 运行时渲染变量。"""
    template: str
    description: str = ""


@dataclass
class SwitchPart:
    """条件切换部分 — 根据变量值选择不同文件。"""
    switch: SwitchConfig


@dataclass
class SwitchConfig:
    variable: str                          # 变量名（从 context 取值）
    cases: dict[str, str]                  # 值 → 文件路径
    default: str = ""                      # 默认文件路径


# 任一 Part 类型
Part = FilePart | TemplatePart | SwitchPart


# ── Tool Reference ───────────────────────────────────────

@dataclass
class ToolRef:
    """引用一个工具声明文件。

    YAML 格式:
        tools:
          - include: tools/node_add.yaml
          - include: tools/edge_add.yaml
          - node_list                        # 简写：直接工具名
    """
    include: str
    description: str = ""


# ── Schema ───────────────────────────────────────────────

@dataclass
class PromptSchema:
    """Prompt 结构描述。

    YAML 格式:
        prompt:
          separator: "\n\n---\n\n"
          composition:
            - file: prompts/role.md
            - switch:
                variable: strategy
                cases: {topo: prompts/topo.md}
            - template: "领域: {{ domain }}"
          tools:
            - include: tools/node_add.yaml
            - include: tools/edge_add.yaml
    """
    composition: list[Part] = field(default_factory=list)
    tools: list[ToolRef] = field(default_factory=list)
    separator: str = "\n\n"
    description: str = ""


# ── Parser ───────────────────────────────────────────────

def parse_schema(raw: dict) -> PromptSchema:
    """从 YAML 解析的 dict 构建 PromptSchema。

    Args:
        raw: YAML 解析后的 dict，格式为 {"prompt": {"separator": ..., "composition": [...], "tools": [...]}}

    Returns:
        PromptSchema 实例
    """
    prompt_raw = raw.get("prompt", raw)  # 兼容有无 "prompt:" 顶层 key
    composition: list[Part] = []

    for item in prompt_raw.get("composition", []):
        if isinstance(item, str):
            # 简写: 直接字符串 → 视为 file
            composition.append(FilePart(file=item))
        elif isinstance(item, dict):
            if "file" in item:
                composition.append(FilePart(
                    file=item["file"],
                    description=item.get("description", ""),
                ))
            elif "template" in item:
                composition.append(TemplatePart(
                    template=item["template"],
                    description=item.get("description", ""),
                ))
            elif "switch" in item:
                sw = item["switch"]
                composition.append(SwitchPart(switch=SwitchConfig(
                    variable=sw["variable"],
                    cases=sw.get("cases", {}),
                    default=sw.get("default", ""),
                )))

    # 解析 tools 声明
    tools: list[ToolRef] = []
    for item in prompt_raw.get("tools", []):
        if isinstance(item, str):
            # 简写: 直接字符串 → 工具名/路径
            tools.append(ToolRef(include=item))
        elif isinstance(item, dict):
            if "include" in item:
                tools.append(ToolRef(
                    include=item["include"],
                    description=item.get("description", ""),
                ))
            elif "name" in item:
                # 内联工具声明: {name: "node_add", description: "..."}
                tools.append(ToolRef(
                    include=item["name"],
                    description=item.get("description", ""),
                ))

    return PromptSchema(
        composition=composition,
        tools=tools,
        separator=prompt_raw.get("separator", "\n\n"),
        description=prompt_raw.get("description", ""),
    )


# ── Renderer ─────────────────────────────────────────────

def render_schema(
    schema: PromptSchema,
    variables: dict[str, Any],
    base_dir: str | Path = ".",
    config: dict[str, Any] | None = None,
) -> str:
    """根据 Schema 和变量渲染完整的 Prompt。

    Args:
        schema: 解析后的 PromptSchema
        variables: 运行时变量（用于 switch 判断 + template 渲染）
        base_dir: 文件路径的基准目录
        config: weave.yaml 配置（用于 template 的 {{ config.xxx }}）

    Returns:
        完整渲染的 Prompt 字符串
    """
    parts: list[str] = []
    base = Path(base_dir)

    for part in schema.composition:
        rendered = _render_part(part, variables, base, config)
        if rendered.strip():
            parts.append(rendered)

    return schema.separator.join(parts)


def _render_part(
    part: Part,
    variables: dict[str, Any],
    base: Path,
    config: dict[str, Any] | None,
) -> str:
    """渲染单个 Part。"""
    if isinstance(part, FilePart):
        path = base / part.file
        if not path.exists():
            raise FileNotFoundError(f"Schema file part not found: {path}")
        template = path.read_text(encoding="utf-8")
        return interpolate(template, variables, config or {})

    elif isinstance(part, TemplatePart):
        return interpolate(part.template, variables, config or {})

    elif isinstance(part, SwitchPart):
        sw = part.switch
        value = variables.get(sw.variable, "")
        file_path = sw.cases.get(str(value), sw.default)
        if not file_path:
            # 没有匹配且无默认值 → 空字符串，静默跳过
            return ""
        path = base / file_path
        if not path.exists():
            raise FileNotFoundError(f"Schema switch file not found: {path}")
        template = path.read_text(encoding="utf-8")
        return interpolate(template, variables, config or {})

    return ""


# ── Tool Resolution ──────────────────────────────────────

def resolve_tool_names(
    schema: PromptSchema,
    base_dir: str | Path = ".",
) -> list[str]:
    """从 Schema 的 tools 声明中解析出工具名列表。

    用于传入 Weave.run() / arun() / stream() 的 tool_filter 参数。

    每个 ToolRef 的 include 字段指向一个工具声明 YAML 文件，
    其中必须包含 ``name:`` 字段。如果文件不存在，
    则回退为 include 路径的 stem（去掉扩展名）。

    Args:
        schema: 解析后的 PromptSchema
        base_dir: 工具 YAML 文件路径的基准目录

    Returns:
        工具名列表，可直接作为 tool_filter 使用。
        如果 schema.tools 为空，返回空列表（表示不限工具）。

    Example:
        >>> schema = parse_schema(yaml.safe_load(\"\"\"
        ... prompt:
        ...   tools:
        ...     - include: tools/node_add.yaml
        ...     - include: tools/edge_add.yaml
        ... \"\"\"))
        >>> resolve_tool_names(schema, base_dir="./prompts")
        ['node_add', 'edge_add']
    """
    import yaml

    names: list[str] = []
    base = Path(base_dir)

    for ref in schema.tools:
        tool_path = base / ref.include

        if tool_path.exists():
            try:
                raw = yaml.safe_load(tool_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("name"):
                    names.append(raw["name"])
                else:
                    # 文件存在但没有 name 字段 → 用 stem
                    names.append(tool_path.stem)
            except Exception:
                # YAML 解析失败 → 用 stem
                names.append(tool_path.stem)
        else:
            # 文件不存在 → 用 include 路径的 stem
            names.append(Path(ref.include).stem)

    return names
