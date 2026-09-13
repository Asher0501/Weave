"""weave.strategy —— **未来维度**的素材（当前**不接入** LLM 交互对象）。

这里目前只有组装策略（素材 / 召回 / 写回 / 拼装），它属于"上下文管理"这个未来维度：
要不要记忆、从文本还是数据库取上下文，**由应用自己决定**（weave 不替它决定）。

当前唯一在用的维度是 LLM 交互（``weave.llm``）；本模块保留为未来维度的起点，
不属于兼容承诺。
"""
from weave.strategy.assembly import (
    MemoryAssembly,
    message_from_record,
    observation_record,
    record_from_message,
)

__all__ = [
    "MemoryAssembly",
    "record_from_message",
    "message_from_record",
    "observation_record",
]
