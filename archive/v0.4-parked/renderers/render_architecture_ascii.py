"""把 Weave 架构渲染成"对齐的" ASCII 图（可直接贴进对话/终端）。

画法：**契约与词汇（weave.core）不是栈里的一级**，而是对象与 provider
两侧共用的词表——所以它画在侧边，两条依赖箭头指向它，而不是压在底下。

为什么要脚本：中文是双宽字符，手写 ASCII 框图几乎必然错位。
本脚本按**显示宽度**（东亚宽字符算 2 列）计算内边距，保证每一行列宽一致。

用法：
    python scripts/render_architecture_ascii.py
"""
from __future__ import annotations

import sys
import unicodedata


def width(text: str) -> int:
    """显示宽度：东亚全宽字符算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, target: int, align: str = "left") -> str:
    space = max(0, target - width(text))
    if align == "center":
        left = space // 2
        return " " * left + text + " " * (space - left)
    if align == "right":
        return " " * space + text
    return text + " " * space


def box(lines: list[str], *, title: str | None = None, min_width: int = 0,
        pad_x: int = 2) -> list[str]:
    body = ([title, ""] if title else []) + lines
    inner = max([min_width - pad_x * 2 - 2] + [width(line) for line in body])
    out = ["┌" + "─" * (inner + pad_x * 2) + "┐"]
    for line in body:
        out.append("│" + " " * pad_x + pad(line, inner) + " " * pad_x + "│")
    out.append("└" + "─" * (inner + pad_x * 2) + "┘")
    return out


def columns(left: list[str], right: list[str], *, gap: int = 4,
            links: dict[int, str] | None = None) -> list[str]:
    """左右两列拼接；`links` 指定左列第几行画一条指向右列的箭头。"""
    links = links or {}
    height = max(len(left), len(right))
    left_w = max(width(line) for line in left) if left else 0
    left = left + [" " * left_w] * (height - len(left))
    right = right + [""] * (height - len(right))
    out: list[str] = []
    for index, (lhs, rhs) in enumerate(zip(left, right)):
        connector = links.get(index)
        if connector:
            lhs = pad(lhs, left_w) + " " + pad(connector, gap - 1)
        else:
            lhs = pad(lhs, left_w) + " " * gap
        out.append(lhs + rhs)
    return out


def render() -> str:
    # ── 左列：应用 → 对象 → provider（依赖流向自上而下） ──
    left: list[str] = []
    app = box([
        "agora 接力引擎 · agora-web · 你的应用",
        "自己决定：上下文 / 执行 / 循环几轮",
    ], title="① 应用 / 业务（外部）")
    left += app
    left.append(pad("│ messages（core 的 Message）", width(app[0]), "center"))
    left.append(pad("▼", width(app[0]), "center"))

    obj = box([
        "装配：model=… 用默认 provider；provider=… 注入",
        "",
        "输入 messages（+ 可选 tools）",
        "  ↓",
        "reliability 超时/重放/退避/限流/取消/分类",
        "  ↓ 哑原子：一次调用，失败即抛",
        "decode 原生 / DSML / JSON → 统一结构",
        "usage 字段归一 + 对象级累计",
        "observer 注入回调（默认不上报）",
        "  ↓",
        "输出 LLMResponse（content/tool_calls/usage）",
    ], title="② weave.llm(...) —— LLM 交互（weave 唯一功能）")
    obj_top = len(left)
    left += obj
    left.append(pad("│ CallRequest（core 的信封）", width(app[0]), "center"))
    left.append(pad("▼", width(app[0]), "center"))

    atom = box([
        "OpenAIHTTPProvider（零依赖 + 可注入 transport）",
        "FakeProvider（离线确定性，测试用）",
        "一次调用，失败即抛；不重试不计时不解码",
    ], title="③ LLMProvider 实现（哑原子 · 可替换）")
    atom_top = len(left)
    left += atom

    # ── 右列：两侧共用的词表（②③ 都指向它，它不是栈里的一级） ──
    core = box([
        "②③ 唯一的共同语言",
        "不依赖任何 weave 模块",
        "I = 0.00（最稳定）",
        "",
        "接口 LLMProvider / StateStore",
        "词汇 Message / ToolCall /",
        "     ToolSchema / LLMResponse /",
        "     StreamChunk",
        "信封 CallRequest / Invocation /",
        "     TypedFailure",
        "错误 RateLimit / Server /",
        "     Network / Auth /",
        "     BadRequest / Rejected /",
        "     Timeout / Parse",
    ], title="契约与词汇（weave.core）")

    # 右列从 ② 的标题下方开始，保证两条箭头都落在框上
    lead = obj_top + 1
    right = [""] * lead + core
    while len(right) < len(left):
        right.append("")

    links = {obj_top + 2: "──▶", atom_top + 1: "──▶"}
    body = columns(left, right, gap=6, links=links)

    total = max(width(line) for line in body)

    # ── 底部：归档说明（不属于依赖图，只是备注） ──
    footer = box([
        "会话日志 · 检索 · 执行器 · 环境目录 · 组装策略 · trace/checkpoint · Redis 存储",
        "legacy provider（openai_compat + _reliability + adapter，419 行）· demos/atomic",
        "编排（多轮循环 / 工具往返 / 观察回填）本来就不在 weave，属应用",
        "归档不是删除：取回时连同接口契约与测试一起取回（见 archive/v0.4-parked/README.md）",
    ], title="④ 已归档的能力（archive/v0.4-parked/）")
    body += [""] + [pad(line, total) for line in footer]
    return "\n".join(pad(line, total) for line in body)


if __name__ == "__main__":
    text = render()
    print(text)
    lines = text.splitlines()
    widths = {width(line) for line in lines}
    print(f"\n[check] {len(lines)} lines, display widths: {sorted(widths)}", file=sys.stderr)
