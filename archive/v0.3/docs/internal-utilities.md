# Weave 内部工具（非功能，排查用）

> 本页记录 Weave 内置的**非功能**工具——它们在框架内部自动生效，宿主正常使用时无需感知。
> 仅当你在**适配 Weave 或排查问题**（LLM 调用偶发失败、挂起、或担心 prompt 注入）时，
> 才需要了解这里，用它们调试 / 调参。

这些工具**不再是公开 API**（已从 `public-api.md` 撤下），改由框架内部接线、默认生效，
参数通过 `weave.yaml` 配置。

## 1. LLM 调用重试（可靠性）

- **接线位置**：`loop/base.py` 的 `_chat_with_retry`，包住每次**非流式** LLM 调用。
- **行为**：瞬时失败自动重试——网络抖动 `NetworkError`、限流 `RateLimitError`、
  服务端 5xx `ServerError`；认证 / 参数 / 上下文超长等错误**不重试**（重试无意义）。
- **配置**（`loop:` 段）：

```yaml
loop:
  llm_retry_attempts: 3      # 总尝试次数（含首次）；1 = 不重试
  llm_retry_backoff: 2.0     # 退避倍率（第 n 次重试等待 backoff^(n-1) 秒，带随机抖动）
```

- **排查**：偶发 `NetworkError` / `ServerError` 时调大 `llm_retry_attempts`；429 限流保留重试即可。

## 2. LLM 调用硬超时（时效性）

- **接线位置**：同上 `_chat_with_retry`，用 `asyncio.timeout` 给整个重试序列设硬上限。
- **配置**（`loop:` 段）：

```yaml
loop:
  llm_call_timeout: 120.0     # 单次非流式 LLM 调用的硬超时（秒）
```

- **排查**：LLM 调用长时间无响应（挂起）时，超时抛 `WeaveTimeoutError`，避免永久卡住。
  注意：超时会取消在途调用，底层 HTTP 连接可能残留（协程/线程取消的固有限制）。

## 3. Prompt 注入防御（安全性）

- **接线位置**：`agent.py` 的 `_run_impl_inner`，在输入进 loop 前清洗。
- **开关**（默认关闭）：

```yaml
features:
  prompt_defense: true        # 开启后对用户输入做注入检测
```

- **行为**：检测 `ignore previous instructions`、`[INST]` 等注入模式；默认策略 `defend`
  （检测到则追加一条防御指令）。`sanitize()` 内部还支持 `reject`（拒绝）/ `strip`（抹除），
  但当前接线用默认 `defend`。
- **注意**：开启后输入会被截断到 2000 字符（`sanitize` 的 `max_length` 默认值）。

## 排查速查表

| 症状 | 相关工具 | 建议 |
|------|---------|------|
| LLM 调用偶发失败 | 重试 | 调大 `llm_retry_attempts` |
| 频繁 429 | 重试 | 保持重试（已内建退避）；必要时调大 `llm_retry_backoff` |
| LLM 调用长时间无响应 | 硬超时 | 调小 `llm_call_timeout` |
| 担心用户输入操纵 AI | 注入防御 | 开 `features.prompt_defense: true` |
