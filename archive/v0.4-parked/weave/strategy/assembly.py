"""组装策略 — 素材 / 召回 / 写回 / 拼装（D3 / D6 / D7 / D8）。

一个对象管四件事（**不拆**：它们是同一件事的四个动作）：
1. 素材：`initial`（init 一次的初始输入）
2. 召回（读）：tail / 检索
3. 写回（写）：轮次、观察（按 Record.id 幂等）
4. 拼装：把上面三样拼成 `Context`（= `weave.context`，模型实际看到的东西）

**命名约定只在这一层**（D7）：namespace / key / 模板都在策略里，
原子层（Database/ConversationLog/Search）把它们当不透明字符串。

`weave.assemble(...)` 是本模块类的工厂（见 weave/__init__.py）。
"""
from __future__ import annotations

from typing import Any

from weave.core.envelopes import (
    Context,
    RecallRequest,
    Record,
    RememberRequest,
    Scope,
)
from weave.core.interfaces import ConversationLog, Database, Search
from weave.core.types import Message, ToolCall


# ── Message ↔ Record（信封之间的翻译，只在策略层做） ─────


def record_from_message(message: Message, *, meta: dict[str, Any] | None = None) -> Record:
    """一条消息 → 一条记忆记录。"""
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
        ]
    return Record(kind="message", payload=payload, meta=dict(meta or {}))


def message_from_record(record: Record) -> Message | None:
    """一条记忆记录 → 一条消息（非 message 记录返回 None）。"""
    payload = record.payload
    if record.kind != "message" or not isinstance(payload, dict):
        return None
    calls = payload.get("tool_calls") or []
    return Message(
        role=str(payload.get("role", "user")),
        content=str(payload.get("content", "")),
        name=payload.get("name"),
        tool_call_id=payload.get("tool_call_id"),
        tool_calls=[
            ToolCall(id=str(c.get("id", "")), name=str(c.get("name", "")),
                     arguments=dict(c.get("arguments") or {}))
            for c in calls
        ]
        or None,
    )


def observation_record(observation: Any) -> Record:
    """一条 Observation → 一条记忆记录（失败也要留下，便于复盘/自救）。"""
    invocation = getattr(observation, "invocation", None)
    payload: dict[str, Any] = {
        "ok": bool(getattr(observation, "ok", True)),
        "call_id": getattr(observation, "id", None),
        "name": getattr(invocation, "name", "") if invocation else "",
        "arguments": getattr(invocation, "arguments", {}) if invocation else {},
    }
    if payload["ok"]:
        payload["value"] = getattr(observation, "value", None)
    else:
        failure = getattr(observation, "failure", None)
        payload["error"] = getattr(failure, "kind", "error")
        payload["message"] = getattr(failure, "message", "")
    return Record(kind="observation", payload=payload)


# ── 默认组装策略 ────────────────────────────────────────


class MemoryAssembly:
    """默认策略：**读 + 写 = 会记忆**（D3）。

    `stateless=True` 时既不读也不写（等价于"换了一个策略"，而不是给 run 传开关）。
    """

    def __init__(
        self,
        *,
        namespace: str = "default",
        key: str = "history",
        window: int = 20,
        mode: str = "tail",          # tail | search | hybrid
        system: str | None = None,
        initial: str | None = None,
        stateless: bool = False,
        keep_observations: bool = True,
        search_k: int | None = None,
        namespace_template: str | None = None,
    ) -> None:
        self.namespace = namespace_template or namespace
        self.key = key
        self.window = window
        self.mode = mode
        self.system = system
        self.initial = initial
        self.stateless = stateless
        self.keep_observations = keep_observations
        self.search_k = search_k
        # 由装配层绑定（D5：构造器 + 实例属性，无全局可变状态）
        self.log: ConversationLog | None = None
        self.database: Database | None = None
        self.search: Search | None = None
        self._params: dict[str, Any] = {}

    # ── 装配（由 weave.init 调用） ────────────────────

    def bind(
        self,
        *,
        log: ConversationLog | None = None,
        database: Database | None = None,
        search: Search | None = None,
        params: dict[str, Any] | None = None,
    ) -> "MemoryAssembly":
        """把原子/参数装进策略（同一个策略对象可以被重新装配）。"""
        if log is not None:
            self.log = log
        if database is not None:
            self.database = database
        if search is not None:
            self.search = search
        if params:
            self._params = {**self._params, **params}
        return self

    def scope(self, **params: Any) -> Scope:
        """按命名约定（模板）算出本次作用域——约定住在策略层（D7）。"""
        merged = {**self._params, **params}
        scope = Scope(namespace=self.namespace, key=self.key)
        return scope.render(**merged) if merged else scope

    # ── 召回（读） ───────────────────────────────────

    async def recall(self, request: RecallRequest) -> list[Record]:
        if self.stateless or self.log is None:
            return []
        if request.mode == "search" and self.search is not None and request.query:
            hits = await self.search.search(
                request.scope, request.query, self.search_k or request.n
            )
            return hits
        return await self.log.tail(request.scope, request.n)

    # ── 写回（写） ───────────────────────────────────

    async def remember(self, request: RememberRequest) -> None:
        if self.stateless or self.log is None:
            return
        records = [
            r
            for r in request.records
            if self.keep_observations or r.kind != "observation"
        ]
        if records:
            await self.log.append(request.scope, records)

    # ── 拼装 ─────────────────────────────────────────

    async def context(
        self,
        *,
        prompts: str | None = None,
        extra: list[Message] | None = None,
        scope_params: dict[str, Any] | None = None,
    ) -> Context:
        """把 素材 + 召回 + 实时输入 拼成模型看到的消息（= weave.context）。"""
        scope = self.scope(**(scope_params or {}))
        messages: list[Message] = []
        if self.system:
            messages.append(Message(role="system", content=self.system))
        if self.initial:
            messages.append(Message(role="user", content=self.initial))

        records = await self.recall(
            RecallRequest(
                scope=scope,
                n=self.window,
                mode="search" if (self.mode != "tail" and prompts) else "tail",
                query=prompts if self.mode != "tail" else None,
            )
        )
        for record in records:
            message = message_from_record(record)
            if message is not None:
                messages.append(message)

        if extra:
            messages.extend(extra)
        if prompts:
            messages.append(Message(role="user", content=prompts))

        return Context(
            messages=messages,
            meta={
                "scope": {"namespace": scope.namespace, "key": scope.key},
                "recalled": len(records),
                "window": self.window,
                "mode": self.mode,
                "stateless": self.stateless,
            },
        )

    # ── 供编排层使用的便利方法 ────────────────────────

    async def remember_messages(self, messages: list[Message], **params: Any) -> None:
        await self.remember(
            RememberRequest(
                scope=self.scope(**params),
                records=[record_from_message(m) for m in messages],
            )
        )
