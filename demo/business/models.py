"""业务领域模型。纯 Python 类型，零 weave 依赖。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    """一个 AI 人设。"""
    id: str
    name: str
    role: str


@dataclass(frozen=True)
class ObjectiveFact:
    """一条共享的客观事实 / 外部数据（所有人设看到的是同一份）。"""
    content: str
    metadata: dict | None = None


@dataclass(frozen=True)
class PersonaAnswer:
    """某个人设对某一轮输入的回答。"""
    persona_id: str
    name: str
    answer: str


@dataclass
class PersonaMemorySnapshot:
    """某个人设的私有记忆快照（用于 /report 演示隔离）。"""
    persona_id: str
    name: str
    history_count: int
    last_answer: str | None


@dataclass
class IsolationReport:
    """隔离证据：三个人设的私有记忆 + 一份共享客观上下文。"""
    personas: list[PersonaMemorySnapshot] = field(default_factory=list)
    shared_facts: list[str] = field(default_factory=list)
