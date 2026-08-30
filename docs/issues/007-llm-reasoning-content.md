# Issue #007: 推理模型的 thinking/reasoning block 未提取

## 问题

Anthropic adapter 解析响应时只取 `type == "text"` 与 `type == "tool_use"`，忽略 `thinking`/`reasoning` block。推理型模型（如 DeepSeek V4 Pro、Claude extended thinking）把正文放在 thinking block，导致 `content` 为空。

## 现状

- `weave/llm/anthropic.py:289-328`：`_parse_anthropic_response` 遍历 `content` 只处理 `block_type in ("text", "tool_use")`。

## 影响

实测对接 DeepSeek V4 Pro：`finish_reason='length'`、`usage.output=32` 但 `content=''`，用户得到空回答。这是对接任何 reasoning 模型的静默丢内容，而非显式报错。

## 建议

1. 增加对 `thinking` / `reasoning` / `redacted_thinking` block 的识别，按配置决定「并入正文 / 仅记录 / 丢弃」。
2. `finish_reason='length'` 且无 text 时，至少给出明确告警而非静默返回空串（与 iterative 的「未收敛」提示同理）。

## 讨论记录

2026-08-30, Asher & Claude.

三人设 demo 的 `--real` 用 `deepseek-v4-pro` 时复现；改用 `deepseek-chat`（非推理模型）正常返回文本。
