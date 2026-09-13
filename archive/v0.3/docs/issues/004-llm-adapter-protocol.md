# Issue #004: LLM Adapter 协议差异

## 问题

不同 LLM SDK 对多 tool result 的消息格式要求不同。Adapter 层负责将 Weave 内部通用格式转换为各 SDK 专有格式。

## 各 SDK 的多 tool result 格式

| SDK | 格式 | 示例 |
|-----|------|------|
| **OpenAI** | 每个 tool result 一条独立 `role="tool"` 消息 | `[msg(role="tool", id=A), msg(role="tool", id=B)]` |
| **Anthropic** | 所有 tool result 打包到一条 `role="user"` 消息的多个 content block | `[msg(role="user", content=[{type:"tool_result", id:A}, {type:"tool_result", id:B}])]` |
| **OpenAI 兼容** (DeepSeek 等) | 同 OpenAI | 同 OpenAI |

## Weave 内部消息模型

Weave 内部统一使用 OpenAI 风格——每个 tool result 一条独立 `Message(role="tool", ...)`。

```python
# Weave 内部消息（OpenAI 格式）
[
    Message(role="assistant", content="..."),           # LLM 决定调 tool
    Message(role="tool", content="...", tool_call_id="A"),  # tool A 结果
    Message(role="tool", content="...", tool_call_id="B"),  # tool B 结果
]
```

## Adapter 职责

格式转换是 **Adapter 层的专属职责**。Loop、Memory、Tool Registry 不应感知 SDK 差异。

### 转换规则

AnthropicAdapter 在发送前：

1. 扫描消息列表
2. 将连续的 `role="tool"` 消息合并为一条 `role="user"` 消息
3. 每个 tool 消息转为 `{"type": "tool_result", "tool_use_id": ..., "content": ...}` content block

```
输入（Weave 通用格式）:
  msg(role="assistant", content="...")
  msg(role="tool", tool_call_id="A", content="...")
  msg(role="tool", tool_call_id="B", content="...")

输出（Anthropic 格式）:
  {"role": "assistant", "content": [...]}
  {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "A", "content": "..."},
      {"type": "tool_result", "tool_use_id": "B", "content": "..."},
  ]}
```

## 新增 Adapter 的检查清单

实现新的 LLM Adapter 时，必须确认以下协议差异：

- [ ] 多 tool result 的打包方式（独立消息 vs. 同一条消息的多个 block）
- [ ] System prompt 的传递方式（`messages[0]` vs. `system` 参数）
- [ ] Tool Schema 格式（OpenAI `{type: "function", function: {...}}` vs. Anthropic `{name, description, input_schema}`）
- [ ] 流式响应的 token 提取方式
- [ ] 错误类型映射（SDK 异常 → `weave.llm.errors` 类型化异常）

## 讨论记录

2026-08-02, Asher & Claude.

- 问题发现：myKG 适配器多 tool 调用时 Anthropic API 报错
- 根因：Anthropic 要求同轮 tool result 打包在同一条 user 消息
- 决策：只修 AnthropicAdapter，不改 Loop 或通用消息模型
