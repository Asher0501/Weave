"""把 Weave 的设计哲学与架构渲染成 SVG（零依赖：手写 XML，不需要任何绘图库）。

产出两张图（只画 weave **现在有**的东西）：

    docs/weave-design-philosophy.svg      设计哲学：先验 · 判决规则 · 哑原子/聪明对象 · 正交性预算 · 两种形态 · 不许静默
    docs/weave-global-architecture.svg    架构：应用 → 对象 → 厂商适配层，右侧是 ②③ 共用的词表

自带**排版自校验**：每行文本按 CJK 宽度估算后与它所属的框比较，溢出即报警并以非零码退出，
所以图不会被静默画坏。

用法：
    python scripts/render_design_svg.py [输出目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "docs"

# ── 版面常量（px） ──────────────────────────────────────
PAGE_W = 1280
M = 26                 # 页边距
PAD = 16               # 框内边距
LINE_H = 24            # 正文行高
TITLE_H = 32           # 框标题占高
GAP = 20               # 框间距
FS = 13                # 正文字号
FS_TITLE = 14
FS_BIG = 22
FS_SUB = 13.5
FS_NOTE = 12

BG = "#0d1117"
PANEL = "#161b22"
PANEL_DEEP = "#101820"
TITLE = "#e6edf3"
WHITE = "#eaf0ff"
DIM = "#9fb0c9"
FAINT = "#7d8da5"

APP_E = "#b08cff"
OBJ_E = "#6ea8ff"
ATOM_E = "#4fe3c1"
CORE_E = "#ffcf6e"
VENDOR_E = "#8b949e"
OK_E = "#3fb950"

FONT_STACK = ('ui-sans-serif, -apple-system, "Segoe UI", "Microsoft YaHei", '
              '"PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC", sans-serif')
MONO_STACK = '"Cascadia Mono", "JetBrains Mono", Consolas, "Courier New", monospace'


# ── CJK 宽度估算（自校验用） ────────────────────────────
def _is_wide(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0x303E or 0x3041 <= o <= 0x33FF
        or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xA000 <= o <= 0xA4CF
        or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F
        or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
    )


def text_w(s: str, size: float) -> float:
    return sum(size * (1.0 if _is_wide(c) else 0.53) for c in s)


# ── 极简 SVG 画布 ──────────────────────────────────────
class Svg:
    def __init__(self, width: float, height: float) -> None:
        self.width, self.height = width, height
        self.parts: list[str] = []
        self.problems: list[str] = []
        self.arrow_colors: list[str] = []

    # 基础图元
    def rect(self, x, y, w, h, *, fill, stroke, rx=10, lw=1.4) -> None:
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{lw}"/>'
        )

    def text(self, x, y, s, *, size=FS, color=DIM, weight="normal", anchor="start",
             mono=False, box=None) -> None:
        """y 是基线。给了 box=(bx,by,bw,bh) 就参与排版自校验。"""
        if box is not None:
            bx, by, bw, bh = box
            need = text_w(s, size)
            if x + need > bx + bw - 4:
                self.problems.append(
                    f"横向溢出 {s[:34]!r}: 需要 {need:.0f}px，框内可用 "
                    f"{bx + bw - 4 - x:.0f}px"
                )
            if y > by + bh - 2 or y - size < by + 2:
                self.problems.append(f"纵向溢出 {s[:34]!r}: 基线 y={y:.1f} 不在框 "
                                     f"[{by:.1f},{by + bh:.1f}] 内")
        family = MONO_STACK if mono else FONT_STACK
        esc = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family={family!r} font-size="{size}" '
            f'fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{esc}</text>'
        )

    def arrow(self, x1, y1, x2, y2, *, color=OBJ_E, lw=1.8) -> None:
        if color not in self.arrow_colors:
            self.arrow_colors.append(color)
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{lw}" marker-end="url(#arrow-{color[1:]})"/>'
        )

    # 组合图元
    def panel(self, x, y, w, title, lines, *, accent, face=PANEL, title_color=WHITE,
              numbered=None) -> float:
        """画一个框并返回它的底部 y。lines 里每项是 (文本, 颜色) 或 (文本, 颜色, 字号)。"""
        height = PAD * 2 + (TITLE_H if title else 0) + len(lines) * LINE_H
        self.rect(x, y, w, height, fill=face, stroke=accent)
        box = (x, y, w, height)
        cursor = y + PAD + FS_TITLE          # 第一行基线
        if title:
            label = f"{numbered} {title}" if numbered else title
            self.text(x + PAD, cursor, label, size=FS_TITLE, color=title_color,
                      weight="bold", box=box)
            cursor += TITLE_H
        for line in lines:
            text, color = line[0], line[1]
            size = line[2] if len(line) > 2 else FS
            self.text(x + PAD, cursor, text, size=size, color=color, box=box)
            cursor += LINE_H
        return y + height

    def footer(self, y, text, *, color=FAINT, size=FS_NOTE) -> float:
        self.text(M, y, text, size=size, color=color)
        return y + LINE_H

    def render(self) -> str:
        markers = "".join(
            f'<marker id="arrow-{color[1:]}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>'
            for color in self.arrow_colors
        )
        defs = f"<defs>{markers}</defs>"
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width:.0f}" '
            f'height="{self.height:.0f}" viewBox="0 0 {self.width:.0f} {self.height:.0f}">\n'
            f'{defs}\n'
            f'<rect width="100%" height="100%" fill="{BG}"/>\n'
            + "\n".join(self.parts) + "\n</svg>\n"
        )


# ══════════════════════════════════════════════════════════
# 图一：设计哲学
# ══════════════════════════════════════════════════════════
def build_philosophy() -> Svg:
    inner = PAGE_W - M * 2
    svg = Svg(PAGE_W, 0)
    y = M
    svg.text(M, y + FS_BIG, "Weave 设计哲学", size=FS_BIG, color=TITLE, weight="bold")
    y += FS_BIG + 10
    svg.text(M, y, "一个功能：LLM 交互 —— 一个输入（messages + 可选 tools）→ "
                   "一个输出（归一后的 LLMResponse）", size=FS_SUB, color=DIM)
    y += LINE_H + GAP

    panels = [
        ("先验 · 不证自明的出发点", APP_E, [
            ("业务由调用方决定；weave 只做业务无关的封装与实现", WHITE),
            ("一块能力只有在「说不清谁在用」时才该删；留下的每个名字都必须有真实消费者", DIM),
            ("同一请求的重放 = 本维度；变更输入后的再一次动作 = 应用", DIM),
        ]),
        ("判决规则 · 某个能力属不属于 weave", OBJ_E, [
            ("属于：只关乎「这一次模型交互」—— 协议适配 · 可靠性 · 解码 · 计量 · 流式", WHITE),
            ("不属于：需要跨多次交互 —— 循环 · 上下文与记忆 · 检索 · 工具执行 · 编排", DIM),
            ("一句话判据：要跨多次交互才成立的，都不在本维度", FAINT),
        ]),
        ("哑原子 + 聪明对象", ATOM_E, [
            ("适配层（哑原子）：一次调用、失败即抛；不重试 · 不计时 · 不解码 · 不知道策略", WHITE),
            ("对象层：超时 · 重放 · 指数退避+抖动 · Retry-After · 总预算 · 取消 · 8 类失败分类", DIM),
            ("　　　　+ 解码归一 + 计量归一 + 上报；门禁机械执行：适配层里不许出现 Invocation", DIM),
        ]),
        ("core 是词表，不是一层", CORE_E, [
            ("依赖只有三个方向：应用 → 对象；对象 → 适配层；②③ 各自 → core", WHITE),
            ("core 不依赖任何 weave 模块（Ca=3, Ce=0, I=0.00，全包最稳定）", DIM),
            ("改动 core = 基变换：②③ 必须同步改，替换测试会拦住不同步", DIM),
            ("正交性预算：每加一个永久冻结的挂载点，就少一格自由度 —— 能不加就不加", FAINT),
        ]),
        ("输入统一：三种形态各就各位", OK_E, [
            ("中立块 Block：你写语义（TextBlock / ImageBlock），形状由适配层生成 → 跨厂商可移植", WHITE),
            ("纯文本 str：最常用；适配层按厂商要求包成文本块 → 跨厂商可移植", DIM),
            ("原样 dict：厂商独有能力（cache_control / thinking 回传）逐字透传 → 绑定当前厂商", DIM),
            ("三种形态不可混用；Block 是封闭集合，所以能和 tool_calls 共存", FAINT),
        ]),
        ("不许静默", APP_E, [
            ("解不出工具调用不是错误（残留文本仍在 content 里）；但「说不出来」必须显式失败", WHITE),
            ("响应里的块一个都不丢（raw_blocks），否则 thinking 回传永不成立", DIM),
            ("用法错误在发请求之前就报，且不进重放 —— 否则编程错误会被当成厂商故障重试", DIM),
        ]),
    ]
    for title, accent, lines in panels:
        y = svg.panel(M, y, inner, title, lines, accent=accent) + GAP

    y += 2
    svg.text(M, y, "边界不是「能做什么」，而是「一次交互之外的事一律不做」。",
             size=FS_TITLE, color=TITLE, weight="bold")
    y += LINE_H + M
    svg.height = y
    return svg


# ══════════════════════════════════════════════════════════
# 图二：架构
# ══════════════════════════════════════════════════════════
def build_architecture() -> Svg:
    """一条链 + 两张"收齐表"：每段只写自己的事，没有独立的第四块。

    - 输入形态出现在**①的输入那一行**（它本来就是那个字段的取值）；
    - 失败 8 类出现在**②的失败那一行**（它本来就产生在那里）；
    - 词表是**上面出现过的类型的定义处**（每个名字都标了它出现在哪一段）；
    - 守则是**对上面每一段的机械保证**（每条都注明它守的是谁）。
    """
    svg = Svg(PAGE_W, 0)
    full_w = PAGE_W - M * 2
    mid = M + full_w / 2

    def band(title: str, accent: str, lines: list, y: float, *, face: str = PANEL) -> float:
        return svg.panel(M, y, full_w, title, lines, accent=accent, face=face)

    def step(y: float, label: str, color: str) -> float:
        svg.arrow(mid, y + 4, mid, y + 30, color=color)
        svg.text(mid + 12, y + 21, label, size=FS_NOTE, color=color)
        return y + 40

    y = M
    svg.text(M, y + FS_BIG, "Weave 架构 · 一个功能：LLM 交互",
             size=FS_BIG, color=TITLE, weight="bold")
    svg.text(PAGE_W - M, y + FS_BIG, "docs/weave-global-architecture.md",
             size=FS_NOTE, color=FAINT, anchor="end")
    y += FS_BIG + 16
    for line in (
        "一个输入（messages + 可选 tools）→ 一个输出（归一后的 LLMResponse）。",
        "每段只写它自己的事；最后两张表把「上面用到的词」与「要守的规矩」收齐。",
    ):
        svg.text(M, y, line, size=FS_SUB, color=DIM)
        y += LINE_H - 3
    y += 18

    # ① 应用：输入形态就写在这里（它本来就是 Message.content 的取值）
    y = band("① 应用 / 业务（外部）", APP_E, [
        ("输入　messages + tools　｜　三种取值：纯文本 / 中立块 / 原样厂商块"
         "（谁翻译：适配层 / 适配层 / 调用方）", WHITE),
        ("输出　LLMResponse（正常）｜ TypedFailure（失败，8 类）　—— 这两个名字都出自词表", DIM),
        ("自己决定　循环 · 上下文 · 执行 · 编排（需要跨多次交互的，都不在 weave）", FAINT),
    ], y)
    y = step(y, "messages（+ 可选 tools）", APP_E)

    # ② 对象层：失败 8 类就写在这里（它产生在这里）
    y = band("② weave.llm —— 对象层（一次动作的生命周期）", OBJ_E, [
        ("call · call_streaming · stream · aclose", WHITE),
        ("预检 shape_check · reliability · decode · usage · streaming · "
         "回填构造器（as_message / tool_result）", DIM),
        ("失败在这里被归类：TypedFailure 的 8 个 kind —— 可重试 4 类 / 不可重试 4 类", DIM),
    ], y)
    y = step(y, "CallRequest（哑原子：一次调用，失败即抛）", OBJ_E)

    # ③ 适配层：厂商差异在这里被吃掉
    y = band("③ weave.providers —— 适配层（哑原子 · 可替换）", ATOM_E, [
        ("OpenAIHTTPProvider · AnthropicHTTPProvider · FakeProvider · 可注入 Transport", WHITE),
        ("厂商差异在这里被吃掉：认证 · 端点 · 参数 · 形状（三种输入形态也在这里落地）", DIM),
        ("纪律　不重试 · 不计时 · 不分类 · 不解码 —— 它只做翻译", FAINT),
    ], y)
    y = step(y, "HTTP / SSE", ATOM_E)

    # ④ 厂商
    y = band("④ 厂商（外部）", VENDOR_E, [
        ("OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / Ollama / vLLM / 各类网关）", DIM),
        ("Anthropic 原生 Messages API · 以及任何兼容网关　—— 上面那些差异的来源", DIM),
    ], y)

    # ── 收齐表 1：上面出现过的词，全部在这里定义 ──
    y += 22
    y = band("收齐 1 · 词表 weave.core —— 上面出现过的类型，全部在这里定义"
             "（②③ 共用，不是栈里的一级）", CORE_E, [
        ("接口　LLMProvider · StateStore", WHITE),
        ("词汇　Message · Block（①的输入）· ToolCall · ToolSchema（工具往返）· "
         "LLMResponse（②的输出）· StreamChunk（流式）", DIM),
        ("信封　CallRequest（② → ③）· Invocation（② 内部）· TypedFailure（② 的失败出口）", DIM),
        ("失败　可重试 rate_limit · server · network · timeout　｜　"
         "不可重试 auth · bad_request · rejected · parse_error", DIM),
    ], y, face=PANEL_DEEP)

    # ── 收齐表 2：对上面每一段的机械保证 ──
    y += 22
    y = band("收齐 2 · 守则 · 不变量与门禁 —— 机械执行上面每一段（违反即测试红）", VENDOR_E, [
        ("契约面（就是上面那份词表）接口 2 个不得膨胀 · 已归档的名字必须真的不存在", DIM),
        ("原子层（③ 与 stores）不得 import 上层 · ③ 不得出现 Invocation · core 零第三方依赖", DIM),
        ("② 不得 import 存储 · 替换测试：换适配器 → 对象层与调用方零改动 · "
         "改 core = 基变换（②③ 同步改）", WHITE),
    ], y) + M
    svg.height = y
    return svg


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = False
    for name, builder in (("weave-design-philosophy.svg", build_philosophy),
                          ("weave-global-architecture.svg", build_architecture)):
        svg = builder()
        target = out_dir / name
        target.write_text(svg.render(), encoding="utf-8")
        print(f"rendered: {target}  ({svg.width:.0f}x{svg.height:.0f}, "
              f"{target.stat().st_size} bytes)")
        if svg.problems:
            failed = True
            print(f"!! {name} 排版自校验发现 {len(svg.problems)} 处问题：")
            for problem in svg.problems:
                print("   -", problem)
        else:
            print(f"   layout check: OK（{name} 无文本溢出）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
