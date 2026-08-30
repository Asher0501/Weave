"""PersonaAgent —— 把业务层（Persona / ObjectiveFact）接进 weave 的唯一适配层。

对外只暴露业务类型；scope 名 / world_id / prompt 变量名 / state 键名全部从
adapter.yaml 读取，代码中不写死任何业务数据或配置字面量。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from business.models import (
    IsolationReport,
    ObjectiveFact,
    Persona,
    PersonaAnswer,
    PersonaMemorySnapshot,
)
from weave.agent import Weave


class PersonaAgent:
    """三个人设共享客观上下文、各自隔离记忆的适配门面。"""

    def __init__(self, config_path: str | Path, adapter_path: str | Path, *, offline: bool = True):
        self._m = _read_yaml(adapter_path)
        self._seeded_facts: list[str] = []
        # 离线：经 Weave 的 llm= 注入点注入 FakeLLM（无需 dummy key，也不碰 _llm 内部属性）。
        # 这是 docs/issues/010 注册表/注入机制的活示例。
        llm = None
        if offline:
            from weave_adapter.fake_llm import FakeLLM
            llm = FakeLLM()
        self._weave = Weave(str(config_path), llm=llm)

    # ── 映射 helper（全部来自 adapter.yaml，无字面量） ──

    def _scope(self, key: str) -> str:
        return self._m["scope_names"][key]

    def _activate(self, persona_id: str | None) -> None:
        hints: dict[str, str] = {f"{self._scope('world')}_id": self._m["world_id"]}
        if persona_id is not None:
            hints[f"{self._scope('persona')}_id"] = persona_id
        self._weave.memory.activate_scopes(hints)

    def _ns(self, scope_key: str, access: str) -> str:
        return self._weave.memory.get_namespace(self._scope(scope_key), access)

    @property
    def llm_info(self) -> str:
        """当前解析到的 provider / model（用于启动提示）。"""
        c = self._weave._config.llm
        return f"provider={c.provider} model={c.model}"

    # ── 公共接口（业务类型进出） ──

    async def seed_objective(self, facts: list[ObjectiveFact]) -> None:
        """把共享客观事实写入 world 的 state（before_think 会全量注入每个人设）。"""
        self._activate(None)
        sns = self._ns("world", "state")
        contents = [f.content for f in facts]
        await self._weave.memory.state.set(
            self._m["state_keys"]["objective_facts"], contents, sns
        )
        self._seeded_facts = contents

    async def answer(self, persona: Persona, question: str) -> PersonaAnswer:
        """让指定人设回答一个客观问题，并把结果保存到它自己的私有 state。"""
        hints = {
            f"{self._scope('persona')}_id": persona.id,
            f"{self._scope('world')}_id": self._m["world_id"],
        }
        context = {
            self._m["context_keys"]["name"]: persona.name,
            self._m["context_keys"]["role"]: persona.role,
        }
        result = await self._weave.arun(question, scope_hints=hints, context=context)
        # 把该人设的处理结果保存到它自己的私有 state（演示"保存隔离"）
        self._activate(persona.id)
        sns = self._ns("persona", "state")
        await self._weave.memory.state.set(
            self._m["state_keys"]["last_answer"], result.output, sns
        )
        return PersonaAnswer(persona.id, persona.name, result.output)

    async def report(self, personas: list[Persona]) -> IsolationReport:
        """收集隔离证据：各人设私有记忆 + 一份共享客观上下文。"""
        report = IsolationReport(shared_facts=list(self._seeded_facts))
        for p in personas:
            self._activate(p.id)
            sns = self._ns("persona", "stream")
            st_ns = self._ns("persona", "state")
            history = await self._weave.memory.stream.last(1000, [sns])
            last_answer = await self._weave.memory.state.get(
                self._m["state_keys"]["last_answer"], st_ns
            )
            report.personas.append(PersonaMemorySnapshot(
                persona_id=p.id,
                name=p.name,
                history_count=len(history),
                last_answer=last_answer,
            ))
        return report


def _read_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
