# Issue #008: LLM 鉴权与模型解析的语义混用

## 问题

LLM 工厂/配置层三处一致性问题：

1. `ANTHROPIC_AUTH_TOKEN` 被当作 `api_key` 使用：`_resolve_api_key("anthropic")` 返回 `ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN`，而 `AnthropicAdapter` 永远 `AsyncAnthropic(api_key=...)`。`auth_token`（`Authorization: Bearer`）与 `api_key`（`x-api-key`）语义不同。
2. model 回退链是死代码：`load_config` 给 `model` 一个默认字符串后，`create_llm` 只在 `model is None` 时才走 `_resolve_model`；agent 传的是非 None 字符串，`{PROVIDER}_MODEL` 回退永不生效。
3. `create_llm` 报错文案硬编码：「Set ANTHROPIC_API_KEY or OPENAI_API_KEY」，对 deepseek 等其它 provider 场景误导。

## 现状

- `weave/llm/factory.py:98-112`：`_resolve_api_key("anthropic")` 混用两个变量名。
- `weave/llm/anthropic.py:112`：`AsyncAnthropic(api_key=self._api_key)`，无 `auth_token` 分支。
- `weave/config.py:244-246`：`model_name = llm_raw.get("model", "")`，空则仅取 `WEAVE_MODEL`。
- `weave/llm/factory.py:54-58`：报错文案固定 ANTHROPIC/OPENAI。

## 影响

- auth_token 语义混用在「Anthropic SDK 走第三方网关」场景碰巧能跑（demo 实测可通），但网关若严格区分两种 header 则失败。
- 配置里无法干净地「回退到 `ANTHROPIC_MODEL`」；只能显式指定 model。
- 报错对 deepseek/openai 用户误导（正是 demo 首轮 `--real` 报错的直接观感问题）。

## 建议

1. `AnthropicAdapter` 区分 `api_key` 与 `auth_token`：`_resolve_api_key` 分开返回，来源是 `ANTHROPIC_AUTH_TOKEN` 时传 `auth_token=`。
2. 让 model 回退链可用：`load_config` 不填默认 model 时，交由 `create_llm` 的 `_resolve_model` 走 `WEAVE_MODEL → {PROVIDER}_MODEL` 链（model 为 None/空时才调用）。
3. 报错文案按 provider 动态生成对应环境变量名。

## 讨论记录

2026-08-30, Asher & Claude.
