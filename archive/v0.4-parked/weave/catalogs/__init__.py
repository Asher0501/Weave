"""weave.catalogs — EnvironmentCatalog（环境描述）的参考实现（D10）。

默认用反射：注册了函数就有 schema，零额外负担；
需要贴合外部环境时换 StaticCatalog 或自己实现 describe()。
"""
from weave.catalogs.reflection import (
    ReflectionCatalog,
    StaticCatalog,
    schema_from_function,
)

__all__ = ["ReflectionCatalog", "StaticCatalog", "schema_from_function"]
