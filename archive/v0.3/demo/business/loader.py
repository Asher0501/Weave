"""YAML → 业务模型。纯函数，仅依赖 pyyaml（weave 已有依赖）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from business.models import ObjectiveFact, Persona


def load_personas(path: str | Path) -> list[Persona]:
    """从 personas.yaml 加载三个人设。"""
    raw = _read(path)
    return [
        Persona(id=p["id"], name=p["name"], role=p["role"])
        for p in raw.get("personas", [])
    ]


def load_objective_facts(path: str | Path) -> list[ObjectiveFact]:
    """从 objective.yaml 加载共享客观事实。"""
    raw = _read(path)
    return [ObjectiveFact(content=f) for f in raw.get("facts", [])]


def _read(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
