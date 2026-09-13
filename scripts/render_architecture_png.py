"""把 Weave 架构渲染成 PNG（离线，不依赖 mermaid 运行时）。

当前形态：weave 只做一个维度（LLM 交互）——一个输入、一个输出；
其余能力是"未来维度"，不接入对象。详见 docs/weave-llm-dimension.md。

用法：
    python scripts/render_architecture_png.py [输出路径]

自带**排版自校验**：每个文本都与它所属的框比较包围盒，超出就报警并以非零码退出。
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager, patches  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "weave-global-architecture.png"

BG = "#0d1117"
BAND = "#161b22"
TITLE = "#e6edf3"
DIM = "#9fb0c9"
WHITE = "#eaf0ff"
APP_F, APP_E = "#2a2140", "#b08cff"
OBJ_F, OBJ_E = "#1b2b45", "#6ea8ff"
ATOM_F, ATOM_E = "#123029", "#4fe3c1"
BASE_F, BASE_E = "#3a2f18", "#ffcf6e"
PARK_F, PARK_E = "#1c1c22", "#8b949e"

W = 14.0
PAD = 0.22
LINE = 0.25
TITLE_GAP = 0.33
LABEL_H = 0.32
GAP = 0.16

TEXTS: list[tuple[object, tuple[float, float, float, float]]] = []


def pick_font() -> str:
    names = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC"):
        if candidate in names:
            return candidate
    return "DejaVu Sans"


FONT = pick_font()
matplotlib.rcParams["font.sans-serif"] = [FONT, "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False


def box_h(title: str | None, lines: list[str]) -> float:
    return (TITLE_GAP if title else 0.0) + len(lines) * LINE + PAD * 2


class Sheet:
    def __init__(self, fig: Figure, ax) -> None:
        self.fig, self.ax = fig, ax

    def text(self, x, y, s, *, size=10, color=DIM, weight="normal", ha="left",
             va="top", parent=None, rotation=0):
        artist = self.ax.text(x, y, s, fontsize=size, color=color, fontweight=weight,
                              ha=ha, va=va, zorder=6, rotation=rotation)
        if parent is not None:
            TEXTS.append((artist, parent))
        return artist

    def box(self, x, y, w, h, *, face, edge, title=None, lines=(), title_size=12,
            line_size=9, lw=1.6):
        self.ax.add_patch(patches.FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.10",
            linewidth=lw, edgecolor=edge, facecolor=face, zorder=2))
        rect = (x, y, w, h)
        cursor = y + h - PAD
        if title:
            self.text(x + 0.18, cursor, title, size=title_size, color=WHITE,
                      weight="bold", parent=rect)
            cursor -= TITLE_GAP
        for line in lines:
            self.text(x + 0.18, cursor, line, size=line_size, color=DIM, parent=rect)
            cursor -= LINE
        return rect

    def band(self, x, y, w, h, label, *, edge="#30363d", label_size=11.5, face=None):
        self.ax.add_patch(patches.FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.10",
            linewidth=1.3, edgecolor=edge, facecolor=face or BAND, zorder=1))
        rect = (x, y, w, h)
        self.text(x + 0.20, y + h - 0.14, label, size=label_size, color=TITLE,
                  weight="bold", parent=rect)
        return rect

    def arrow(self, x1, y1, x2, y2, *, color="#6ea8ff", width=1.8, dashed=False):
        self.ax.add_patch(patches.FancyArrowPatch(
            (x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=15,
            linewidth=width, color=color, linestyle="--" if dashed else "-",
            shrinkA=0, shrinkB=0, zorder=5))


def build() -> Figure:
    app_lines = [
        "agora 接力引擎 · agora-web 界面层 · 你的应用",
        "自己决定：上下文从哪来（文本 / 数据库 / 关键词）",
        "　　　　　工具怎么执行（bash / 脚本 / HTTP）· 循环几轮 · 什么时候停",
    ]
    obj_lines = [
        "装配：weave.llm(model=…) 用默认 provider；weave.llm(provider=…) 注入自己的",
        "",
        "输入  messages（+ 可选 tools / 本次动作参数）",
        "  ↓",
        "reliability：超时 · 重放 · 退避 · 限流(Retry-After) · 取消 · 错误分类",
        "  ↓  哑原子 provider：一次调用，失败即抛（不重试 / 不计时）",
        "decode：原生 tool_calls / DSML / JSON 兜底 → 统一调用结构",
        "usage：各厂商字段归一 + 对象级累计",
        "observer：注入回调（默认不上报；对象自己不写日志、不埋点）",
        "  ↓",
        "输出  LLMResponse（content / tool_calls / usage / attempts / elapsed_ms）",
    ]
    atom_lines = [
        "OpenAIHTTPProvider（推荐）：零第三方依赖 + 可注入 transport + 自带 SSE 解析",
        "FakeProvider：离线确定性实现（测试用）",
        "协议适配 + 认证/端点 + 厂商参数差异过滤；可靠性不在这里",
    ]
    base_lines = [
        "②③ 唯一的共同语言；不依赖任何 weave 模块（I=0.00，最稳定）",
        "接口  LLMProvider · StateStore",
        "词汇  Message · ToolCall · ToolSchema",
        "      LLMResponse · StreamChunk",
        "信封  CallRequest · Invocation · TypedFailure",
        "错误  RateLimit · Server · Network · Auth",
        "      BadRequest · Rejected · Timeout · Parse",
        "纪律  改动 = 基变换：②③ 必须同步改，替换测试全绿",
    ]
    park_lines = [
        "openai_compat + _reliability + adapter（legacy provider，419 行）· 会话日志 · 检索",
        "执行器 · 环境目录 · 组装策略 · trace/checkpoint · Redis 存储 · demos/atomic · 6 份旧文档/图",
        "编排（多轮循环 / 工具往返 / 观察回填）本来就不在 weave，属应用",
        "归档不是删除：取回时连同接口契约与测试一起取回（见 archive/v0.4-parked/README.md）",
    ]

    app_h = box_h("x", app_lines)
    obj_h = box_h("x", obj_lines)
    atom_h = box_h("x", atom_lines)
    base_h = box_h("x", base_lines)
    park_h = box_h("x", park_lines)
    header = 1.05
    total = (header + app_h + obj_h + atom_h + base_h + park_h
             + GAP * 4 + 0.55)

    fig = Figure(figsize=(W, total), dpi=150, facecolor=BG)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, W)
    ax.set_ylim(0, total)
    ax.axis("off")
    sheet = Sheet(fig, ax)

    left = 0.45                       # 左列：应用 → 对象 → provider
    inner = 8.10
    mid = left + inner / 2
    right_x = 9.35                    # 右列：契约与词汇（两侧共用，不是栈里的一级）
    right_w = W - 0.45 - right_x
    page_right = W - 0.45

    y = total - 0.30
    sheet.text(left, y, "Weave 架构 · 一个功能：LLM 交互",
               size=17, color=TITLE, weight="bold")
    sheet.text(left, y - 0.36,
               "对象只有一个输入（messages + 可选 tools）和一个输出（LLMResponse）；"
               "上下文、执行、循环都由应用自己组装",
               size=10, color=DIM)
    sheet.text(page_right, y, "docs/weave-llm-dimension.md", size=9.5, color=DIM, ha="right")
    y -= header

    rect = sheet.band(left, y - app_h, inner, app_h, "① 应用 / 业务（外部）—— 编排与组合在这里",
                      edge=APP_E)
    ty = y - PAD - LABEL_H
    for line in app_lines:
        sheet.text(left + 0.30, ty, line, size=10, color=WHITE if line is app_lines[0] else DIM,
                   parent=rect)
        ty -= LINE
    sheet.arrow(mid, y - app_h, mid, y - app_h - GAP + 0.01, color=APP_E)
    y -= app_h + GAP

    obj_top = y
    rect = sheet.band(left, y - obj_h, inner, obj_h,
                      "② weave.llm(...) —— LLM 交互（weave 唯一功能）", edge=OBJ_E)
    ty = y - PAD - LABEL_H
    for index, line in enumerate(obj_lines):
        color = WHITE if index < 3 else DIM
        sheet.text(left + 0.30, ty, line, size=9.6, color=color, parent=rect)
        ty -= LINE
    sheet.arrow(mid, y - obj_h, mid, y - obj_h - GAP + 0.01, color=OBJ_E)
    y -= obj_h + GAP

    atom_rect = sheet.band(left, y - atom_h, inner, atom_h,
                           "③ LLMProvider 实现（哑原子 · 可替换 · 不进兼容承诺）", edge=ATOM_E)
    ty = y - PAD - LABEL_H
    for index, line in enumerate(atom_lines):
        sheet.text(left + 0.30, ty, line, size=9.6, color=WHITE if index == 0 else DIM,
                   parent=atom_rect)
        ty -= LINE
    atom_top = y
    y -= atom_h + GAP

    # ── 右列：契约与词汇（②③ 都指向它；它不是栈里的一级） ──
    core_top = obj_top
    core_rect = sheet.band(right_x, core_top - base_h, right_w, base_h,
                           "契约与词汇（weave.core）", edge=BASE_E, face="#101820")
    ty = core_top - PAD - LABEL_H
    for index, line in enumerate(base_lines):
        sheet.text(right_x + 0.26, ty, line, size=9.2, color=WHITE if index == 0 else DIM,
                   parent=core_rect)
        ty -= LINE
    sheet.arrow(left + inner, obj_top - obj_h / 2, right_x - 0.06, obj_top - obj_h / 2,
                color=BASE_E, width=1.6)
    sheet.arrow(left + inner, atom_top - atom_h / 2, right_x - 0.06, atom_top - atom_h / 2,
                color=BASE_E, width=1.6)
    sheet.text((left + inner + right_x) / 2, obj_top - obj_h / 2 + 0.10, "依赖",
               size=8.6, color=BASE_E, ha="center", va="bottom")
    sheet.text((left + inner + right_x) / 2, atom_top - atom_h / 2 + 0.10, "依赖",
               size=8.6, color=BASE_E, ha="center", va="bottom")

    rect = sheet.band(left, y - park_h, page_right - left, park_h,
                      "④ 已归档的能力（archive/v0.4-parked/ · 不是删除）", edge=PARK_E,
                      face="#141821")
    ty = y - PAD - LABEL_H
    for line in park_lines:
        sheet.text(left + 0.30, ty, line, size=9.6, color=DIM, parent=rect)
        ty -= LINE

    ax.text(0.16, (y + total) / 2 - 0.3,
            "依赖只有两个方向：应用 → 对象；对象 → provider；②③ 各自 → core",
            fontsize=8.8, color=DIM, rotation=90, va="center", ha="center")
    ax.text(page_right + 0.16, core_top - base_h / 2 - 0.3,
            "core 不依赖任何 weave 模块（I=0.00）",
            fontsize=8.8, color=DIM, rotation=90, va="center", ha="center")
    return fig


def verify(fig: Figure) -> list[str]:
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    inv = fig.axes[0].transData.inverted()
    problems: list[str] = []
    for artist, (bx, by, bw, bh) in TEXTS:
        bb = artist.get_window_extent(renderer=renderer)
        (x0, y0) = inv.transform((bb.x0, bb.y0))
        (x1, y1) = inv.transform((bb.x1, bb.y1))
        if x0 < bx - 0.02 or x1 > bx + bw + 0.02 or y0 < by - 0.02 or y1 > by + bh + 0.02:
            problems.append(
                f"溢出框 {artist.get_text()[:30]!r}: 文本 x[{x0:.2f},{x1:.2f}] "
                f"y[{y0:.2f},{y1:.2f}] vs 框 x[{bx:.2f},{bx + bw:.2f}] y[{by:.2f},{by + bh:.2f}]"
            )
    return problems


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig = build()
    problems = verify(fig)
    fig.savefig(out, dpi=150, facecolor=BG)
    print(f"rendered: {out}  (font: {FONT}, {out.stat().st_size} bytes)")
    if problems:
        print(f"!! 排版自校验发现 {len(problems)} 处溢出：")
        for problem in problems:
            print("   -", problem)
        return 1
    print("layout check: OK（所有文本都在自己的框内）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
