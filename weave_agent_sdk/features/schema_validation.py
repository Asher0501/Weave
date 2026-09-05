"""B1: Pydantic Schema 校验（可选 Feature）。

用 Pydantic model 校验 LLM 输出的 dict，返回结构化错误。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationError:
    """Schema 校验错误。"""
    missing: list[str] = field(default_factory=list)       # 缺少的必填字段
    type_errors: dict[str, str] = field(default_factory=dict)  # 字段名 → 错误描述
    extra: list[str] = field(default_factory=list)         # 未知字段

    @property
    def is_valid(self) -> bool:
        return not (self.missing or self.type_errors or self.extra)


def validate_schema(data: dict[str, Any], schema: type) -> tuple[Any | None, ValidationError]:
    """用 Pydantic model 校验 dict。

    Args:
        data: LLM 提取出的 dict
        schema: Pydantic BaseModel 子类

    Returns:
        (validated_instance, error) — 二者总有一个为 None
    """
    error = ValidationError()
    instance = None

    try:
        try:
            import pydantic
        except ImportError:
            raise ImportError("pydantic package required for schema_validation. Install with: pip install pydantic")

        # 先检查 Pydantic v2 vs v1
        if hasattr(schema, "model_validate"):
            instance = schema.model_validate(data)
        else:
            instance = schema.parse_obj(data)

    except Exception as e:
        error_str = str(e)

        # 解析常见的 Pydantic 错误
        if "field required" in error_str:
            # 提取缺少的字段名
            import re
            missing = re.findall(r"'(\w+)'", error_str)
            if missing:
                error.missing = missing
        elif "is not a valid" in error_str or "expected" in error_str:
            # 类型错误
            import re
            match = re.search(r"(\w+)\s+.*?\s+is not a valid", error_str)
            if match:
                error.type_errors[match.group(1)] = error_str
            else:
                error.type_errors["_root"] = error_str
        else:
            error.missing = [error_str]

    return instance, error
