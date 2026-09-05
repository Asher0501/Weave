"""Weave 配置错误类型。

load_config 校验配置失败时抛出，message 格式统一为「字段路径 期望 X 得到 Y」，
让配置错误一眼定位到具体字段，而非天书的 ``ValueError: invalid literal for int()``。
"""
from __future__ import annotations


class ConfigError(ValueError):
    """Weave 配置不合法时抛出（D2 配置校验）。"""
