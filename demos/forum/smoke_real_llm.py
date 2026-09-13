"""冒烟：通过 agora 适配器 + weave v0.4 发起 1 次真实 DeepSeek 调用。

- 读取 <14_forum>/weave.yaml 的 llm 段（provider/model/base_url/api_key）；
- 走 agora/adapter/llm.py::build_real_llm（v0.4 版，已把 anthropic 网关写法归一为 OpenAI 兼容端点）；
- Key 仅存于内存，绝不打印。
退出码：0=成功（打印 model/redacted url/回复长度），1=失败（错误信息）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]          # 13_weave
FORUM = REPO.parent / "14_forum"                     # 默认 ../14_forum
# agora 已全量运行于 weave v0.4（LLM + Repository），运行时不再需要 archive/v0.3
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
    ap = argparse.ArgumentParser(description="agora × weave v0.4 真实 LLM 冒烟")
    ap.add_argument("--forum", default=str(FORUM), help="14_forum 目录")
    ap.add_argument("--prompt", default="只回复两个词：连接成功", help="测试 prompt")
    args = ap.parse_args()

    forum = Path(args.forum)
    if not (forum / "weave.yaml").exists():
        print(f"错误：{forum / 'weave.yaml'} 不存在（--forum 指向 14_forum？）", file=sys.stderr)
        return 1

    import asyncio

    from agora.adapter.llm import build_real_llm  # noqa: E402

    async def _run() -> None:
        llm = build_real_llm(config_path=forum / "weave.yaml")
        prov = getattr(llm, "_llm", None)
        model = getattr(prov, "_model", "?")
        base = getattr(prov, "_base_url", "?")
        print(f"model = {model}")
        print(f"base_url = {base}")
        text = await llm.complete(args.prompt)
        print(f"reply_len = {len(text)}")
        print(f"reply = {text[:120]!r}")
        if not text.strip():
            raise RuntimeError("empty reply")

    try:
        asyncio.run(_run())
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
