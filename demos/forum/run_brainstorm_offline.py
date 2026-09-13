"""离线：FakeLLM 跑完整个 brainstorm 接力（不触网、零成本、确定性）。

绕过 agora CLI 的 _resolve_llm（其策略是"存在 weave.yaml 即真实 LLM"），
直接用 agora SDK（wiring/relay/session）装配 FakeLLM，验证：
    机制链路 create_run → relay → fixed_rounds 判停 → transcript 落库；
    weave v0.4 补丁不影响 agora SDK 路径。
用法：
    python demos/forum/run_brainstorm_offline.py [--topic "…"] [--db <路径>]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FORUM = REPO.parent / "14_forum"
# agora 已全量运行于 weave v0.4，运行时不再需要 archive/v0.3
for _p in (REPO, FORUM):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(description="离线 brainstorm（FakeLLM，确定性）")
    ap.add_argument("--forum", default=str(FORUM))
    ap.add_argument("--topic", default="离线测试主题：组件库文档怎么写？")
    ap.add_argument("--db", default=str(REPO / "data" / "forum_offline_test.db"))
    args = ap.parse_args()

    forum = Path(args.forum)
    scenario_path = forum / "scenarios" / "brainstorm.yaml"
    if not scenario_path.exists():
        print(f"错误：{scenario_path} 不存在", file=sys.stderr)
        return 1

    from agora.adapter.llm import FakeLLM
    from agora.adapter.repository import Repository
    from agora.config.schema import load_config
    from agora.types import RuntimeValues
    from agora.wiring import build_registry, run_scenario

    repository = Repository(args.db)
    try:
        scenario = load_config(str(scenario_path))
        registry = build_registry(llm=FakeLLM(reply="离线固定回复（测试链路）"))
        outcome = asyncio.run(
            run_scenario(repository, scenario, RuntimeValues(topic=args.topic), registry)
        )
    finally:
        repository.close()

    recap = getattr(outcome, "recap", None)
    verdict = getattr(outcome, "verdict", None)
    transcript = getattr(outcome, "transcript", [])
    print(
        f"status={getattr(outcome, 'status', '?')} "
        f"termination={getattr(recap, 'termination', '?') if recap else '?'} "
        f"converged={verdict.converged if verdict else False} "
        f"turns={len(transcript)}"
    )
    for turn in transcript:
        print(f"\n[{turn.seq}] {turn.agent_id}: {turn.text[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
