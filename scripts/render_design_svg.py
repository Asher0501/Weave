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
        ("能接未知：两种形态共存", OK_E, [
            ("语义形态 str：weave 负责翻译成厂商形状 → 跨厂商可移植、写法会被校验", WHITE),
            ("原样形态 dict / list[dict]：weave 不解释任何 key，逐字透传 → 厂商独有能力立刻可达", WHITE),
            ("适配层按配置拦「别家已知形状」，未知块一律放行 —— 不挡厂商未来", DIM),
            ("代价写在明处：原样块是厂商形状，换 provider 不可移植", FAINT),
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
    svg = Svg(PAGE_W, 0)
    left_x, left_w = M, 768
    right_x, right_w = 828, PAGE_W - M - 828
    y = M
    svg.text(left_x, y + FS_BIG, "Weave 架构 · 一个功能：LLM 交互",
             size=FS_BIG, color=TITLE, weight="bold")
    svg.text(PAGE_W - M, y + FS_BIG, "docs/weave-llm-dimension.md",
             size=FS_NOTE, color=FAINT, anchor="end")
    y += FS_BIG + 10
    svg.text(left_x, y,
             "对象只有一个输入（messages + 可选 tools）和一个输出（LLMResponse）；"
             "上下文、执行、循环都由应用自己组装", size=FS_SUB, color=DIM)
    y += LINE_H + GAP

    app_y = y
    y = svg.panel(left_x, y, left_w, "应用 / 业务（外部）—— 编排与组合都在这里", [
        ("自己决定：上下文从哪来 · 工具怎么执行 · 循环几轮 · 什么时候停", WHITE),
        ("需要跨多次交互的能力一律不在 weave（所以它也从不替你决定这些）", DIM),
    ], accent=APP_E) + GAP
    svg.arrow(left_x + left_w / 2, app_y + (y - GAP - app_y), left_x + left_w / 2, y - 4,
              color=APP_E)
    svg.text(left_x + left_w / 2 + 10, y - GAP / 2 + 4, "messages（+ 可选 tools）",
             size=FS_NOTE, color=APP_E)

    obj_y = y
    y = svg.panel(left_x, y, left_w, "weave.llm(...) —— 对象：LLM 交互（weave 唯一功能）", [
        ("对外只有四个方法：call · call_streaming · stream · aclose", WHITE),
        ("发请求前预检：适配器自述 protocol / foreign_block_types / block_shape_hint", WHITE),
        ("　　　　　　→ 拦「别家形状」的块；未知块一律放行；shape_check=False 可关", DIM),
        ("reliability：单次超时 · 指数退避+抖动重放 · Retry-After · 总预算 · 取消 · 8 类分类", DIM),
        ("decode：原生 tool_calls / DSML / 围栏 JSON → ToolCall（arguments 已是 dict）", DIM),
        ("usage：厂商字段归一（含 prompt cache）+ 对象级累计 · observer：注入回调，默认不上报", DIM),
        ("输入两种形态：str（语义：适配层翻译）／ dict·list[dict]（原样：逐字透传，不解释 key）", WHITE),
        ("输出：LLMResponse（content · reasoning · tool_calls · usage · attempts ·", DIM),
        ("　　　 elapsed_ms · finish_reason · failure · raw · raw_blocks）", DIM),
    ], accent=OBJ_E) + GAP
    svg.arrow(left_x + left_w / 2, obj_y + (y - GAP - obj_y), left_x + left_w / 2, y - 4,
              color=OBJ_E)
    svg.text(left_x + left_w / 2 + 10, y - GAP / 2 + 4, "CallRequest（哑原子：一次调用，失败即抛）",
             size=FS_NOTE, color=OBJ_E)

    atom_y = y
    y = svg.panel(left_x, y, left_w, "厂商适配层 —— LLMProvider 实现（哑原子 · 可替换）", [
        ("OpenAIHTTPProvider：/chat/completions · 零第三方依赖（stdlib urllib + 自写 SSE）", WHITE),
        ("AnthropicHTTPProvider：/v1/messages · system 顶层参数 · tool_result 块 · 事件流", WHITE),
        ("FakeProvider（离线确定性）· 可注入 Transport（协议适配因此可离线验收）", DIM),
        ("厂商形状翻译 + 认证/端点 + 参数差异过滤；重试 · 超时 · 解码都不在这里", DIM),
    ], accent=ATOM_E) + GAP
    svg.arrow(left_x + left_w / 2, atom_y + (y - GAP - atom_y), left_x + left_w / 2, y - 4,
              color=ATOM_E)
    svg.text(left_x + left_w / 2 + 10, y - GAP / 2 + 4, "HTTP / SSE", size=FS_NOTE, color=ATOM_E)

    y = svg.panel(left_x, y, left_w, "厂商（weave 之外）", [
        ("OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / Ollama / vLLM / 各类网关）", DIM),
        ("Anthropic 原生 Messages API · 以及任何兼容网关", DIM),
    ], accent=VENDOR_E)

    # ── 右列：②③ 共用的词表（不是栈里的一级） ──
    core_lines = [
        ("接口　LLMProvider · StateStore", WHITE),
        ("词汇　Message · ToolCall · ToolSchema", DIM),
        ("　　　LLMResponse · StreamChunk", DIM),
        ("信封　CallRequest · Invocation · TypedFailure", DIM),
        ("失败　可重试 rate_limit · server ·", DIM),
        ("　　　network · timeout", DIM),
        ("　　　不可重试 auth · bad_request ·", DIM),
        ("　　　rejected · parse_error", DIM),
        ("规则　payload_content：str → 语义形态", DIM),
        ("　　　dict / list[dict] → 原样透传", DIM),
        ("　　　原样块 + tool_calls = 歧义，", DIM),
        ("　　　发请求前 TypeError", DIM),
        ("纪律　改动 = 基变换：②③ 同步改，", DIM),
        ("　　　替换测试全绿", DIM),
    ]
    core_bottom = svg.panel(right_x, app_y, right_w,
                            "契约与词汇（weave.core）", core_lines, accent=CORE_E,
                            face=PANEL_DEEP)
    svg.text(right_x + PAD, core_bottom + LINE_H,
             "②③ 共用；core 自己", size=FS_NOTE, color=CORE_E)
    svg.text(right_x + PAD, core_bottom + LINE_H * 2,
             "不依赖任何 weave 模块", size=FS_NOTE, color=CORE_E)
    svg.arrow(left_x + left_w, app_y + 60, right_x - 6, app_y + 60, color=CORE_E, lw=1.5)
    svg.arrow(left_x + left_w, atom_y + 60, right_x - 6, atom_y + 60, color=CORE_E, lw=1.5)

    y = max(y, core_bottom + LINE_H * 2) + GAP
    y = svg.panel(M, y, PAGE_W - M * 2, None, [
        ("依赖只有三个方向：应用 → 对象；对象 → 适配层；②③ 各自 → core"
         "（core 不依赖任何 weave 模块）。", WHITE),
        ("厂商知识只存在于适配层：对象层不认识任何厂商，Message 里也没有厂商名。", DIM),
    ], accent=VENDOR_E) + M
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
