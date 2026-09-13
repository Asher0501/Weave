"""weave.logs — ConversationLog（会话历史）的参考实现（上下文管理域，D11）。

契约见 weave.core.interfaces.ConversationLog：
只增不改、按 Record.id 幂等、单次追加成本不随历史长度增长、scope 透明。
"""
from weave.logs.memory import MemoryConversationLog
from weave.logs.sqlite import SqliteConversationLog

__all__ = ["MemoryConversationLog", "SqliteConversationLog"]
