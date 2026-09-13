"""交互式 CLI：输入一个问题，三个人设分别作答。

运行：
    python main.py            # 离线（FakeLLM，无需 API Key）
    python main.py --real     # 真实模型（需 DEEPSEEK_API_KEY）
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让 demo 可从任意目录运行：加入仓库根（含 weave 包）到 sys.path，并切到 demo 目录
DEMO_DIR = Path(__file__).resolve().parent
REPO_DIR = DEMO_DIR.parent
sys.path.insert(0, str(REPO_DIR))
os.chdir(DEMO_DIR)

# Windows 控制台统一 UTF-8，避免中文乱码 / UnicodeEncodeError（含管道 stdin 的代理字符）
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from business.loader import load_objective_facts, load_personas  # noqa: E402
from weave_adapter.agent import PersonaAgent  # noqa: E402


BANNER = """\
============================================================
 Weave 三人设对话 Demo
 三个人设共享同一份客观上下文，各自独立记忆/会话/保存。
 输入你的问题，三个人设会分别作答。
 命令：/report 查看隔离证据 | /help 帮助 | /quit 退出
============================================================
"""


def _parse_args(argv: list[str]) -> bool:
    return "--real" in argv


async def run(real: bool) -> None:
    personas = load_personas("business/personas.yaml")
    facts = load_objective_facts("business/objective.yaml")

    try:
        agent = PersonaAgent(
            "weave_adapter/weave.yaml",
            "weave_adapter/adapter.yaml",
            offline=not real,
        )
    except Exception as e:  # 常见：真实模型缺 API Key / 模型名非法
        print(f"[初始化失败] {e}")
        print("提示：")
        print("  - 离线运行：去掉 --real（默认，无需 API Key）")
        print("  - 真实模型：设置 ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY 之一")
        print("  - 覆盖 provider/model：LLM_PROVIDER=deepseek WEAVE_MODEL=deepseek-chat python main.py --real")
        return

    await agent.seed_objective(facts)

    print(BANNER)
    mode = "真实模型" if real else "离线桩(FakeLLM)"
    print(f"[{mode}] {agent.llm_info}，已加载 {len(personas)} 个人设，共享客观事实 {len(facts)} 条。\n")

    while True:
        try:
            line = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not line:
            continue
        low = line.lower()
        if low in ("/quit", "/q", "exit", "quit"):
            print("再见。")
            break
        if low in ("/help", "help"):
            print("命令：/report 隔离证据 | /help 帮助 | /quit 退出；其它输入会交给三个人设作答。\n")
            continue
        if low in ("/report", "report"):
            print(_format_report(await agent.report(personas)))
            continue

        print()
        for p in personas:
            a = await agent.answer(p, line)
            print(f"【{a.name}】")
            print(f"  {a.answer}\n")
        print("-" * 60)


def _format_report(report) -> str:
    lines = ["", "===== 隔离证据 ====="]
    lines.append("【共享客观上下文（所有人设一致）】")
    for f in report.shared_facts:
        lines.append(f"  - {f}")
    lines.append("")
    lines.append("【各人设私有记忆（彼此隔离）】")
    for snap in report.personas:
        lines.append(
            f"  [{snap.name}] 会话条数={snap.history_count} "
            f"最后回答={snap.last_answer or '(无)'}"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    asyncio.run(run(_parse_args(sys.argv)))


if __name__ == "__main__":
    main()
