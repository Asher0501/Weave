"""Weave CLI — 脚手架等开发者工具。

用法:
    weave init [target_dir]     # 生成最小可跑通的项目骨架
    python -m weave init [...]  # 等价入口

生成的文件是「标准等价文件」——与手写完全一致，不引入任何脚手架专用机制，
高级功能（memory.scopes / checkpoint / register_* / adapter.yaml）全部保留，
用户可在生成的骨架上自由扩展。
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── 骨架模板 ─────────────────────────────────────────────
# 注意：模板是普通字符串（非 f-string），因为其中含 ${VAR} 和 {{ var }}，
# 是 weave.yaml / prompt 的合法语法，不应被 Python 插值。

_WEAVE_YAML = """\
# Weave 最小配置 —— 能直接跑通。
# 高级功能（memory.scopes / checkpoint / 自定义 loop / 自定义 llm）见 docs/。
llm:
  provider: anthropic                    # anthropic | openai | deepseek
  model: ${WEAVE_MODEL:-claude-sonnet-5-20251001}

loop:
  type: iterative                        # simple | iterative | scheduled
  max_iterations: 10
"""

_SYSTEM_MD = """\
你是一个乐于助人的 AI 助手，用简洁的中文回答。
当用户的问题可以用工具解决时，主动调用工具。
"""

_AGENT_PY = '''\
"""你的 Weave Agent 入口。

跑起来：
    python agent.py

高级自定义（都在生成的骨架上继续加，无需改动 Weave）：
    - 记忆隔离：在 weave.yaml 加 memory.scopes
    - 状态回滚：在 weave.yaml 加 checkpoint
    - 自定义 Loop/LLM：register_loop / register_llm，或 Weave(..., llm=, loop=)
    - 完整 API：docs/public-api.md
"""
from weave_agent_sdk import Weave

weave = Weave("weave.yaml")


@weave.tool
def add(a: int, b: int) -> int:
    """计算两个整数之和。"""
    return a + b


@weave.tool
def get_time() -> str:
    """获取当前时间。"""
    import datetime
    return datetime.datetime.now().isoformat()


if __name__ == "__main__":
    result = weave.run("帮我计算 3 + 5")
    print(result.output)
'''

_README_MD = """\
# {name}

由 `weave init` 生成的 Weave Agent 骨架。

## 运行

```bash
python agent.py
```

## 下一步（高级功能）

| 想做什么 | 怎么做 |
|---------|--------|
| 记忆按会话隔离 | `weave.yaml` 加 `memory.scopes` |
| 状态回滚 | `weave.yaml` 加 `checkpoint` |
| 换模型 / 加 provider | 改 `weave.yaml` 的 `llm.provider`，或 `register_llm` |
| 自定义循环策略 | `register_loop` 或 `Weave(..., loop=)` |
| 流式输出 / REST 服务 | `weave.stream()` / `weave.server.create_app` |

完整文档：`docs/basic.md`（红线）、`docs/public-api.md`（公开 API）、
`docs/issues/`（设计决策记录）。
"""


# ── init 命令 ────────────────────────────────────────────


def init_project(target_dir: str) -> None:
    """在 target_dir 生成最小骨架（不覆盖已存在的文件）。"""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    name = target.resolve().name or "my_agent"

    files = {
        "weave.yaml": _WEAVE_YAML,
        "agent.py": _AGENT_PY,
        "prompts/system.md": _SYSTEM_MD,
        "README.md": _README_MD.format(name=name),
    }

    created: list[str] = []
    skipped: list[str] = []

    for rel_path, content in files.items():
        dest = target / rel_path
        if dest.exists():
            skipped.append(rel_path)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        created.append(rel_path)

    # 输出结果
    print(f"在 {target.resolve()} 生成 Weave 骨架")
    for f in created:
        print(f"  + {f}")
    for f in skipped:
        print(f"  - {f}（已存在，跳过）")
    print()
    print("下一步：")
    print(f"  cd {target_dir}")
    print("  python agent.py")


# ── 入口 ────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台统一 UTF-8，避免中文乱码 / UnicodeEncodeError
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    import argparse

    parser = argparse.ArgumentParser(
        prog="weave",
        description="Weave CLI —— 非侵入式 Agent SDK 工具",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="生成最小可跑通的项目骨架")
    init_parser.add_argument("target", nargs="?", default=".", help="目标目录（默认当前目录）")

    args = parser.parse_args(argv)

    if args.command == "init":
        init_project(args.target)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
