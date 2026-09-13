"""weave.search — Search（检索）的参考实现（上下文管理域，D12）。

契约见 weave.core.interfaces.Search：只承诺「问题 → 相关记录」。
编码/重排这类模型零件**不属于契约**，需要时由装配层注入给实现。
"""
from weave.search.keyword import KeywordSearch, NullSearch

__all__ = ["KeywordSearch", "NullSearch"]
