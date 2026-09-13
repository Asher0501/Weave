# Issue #005: Prompt 路径解析与 Schema 显式路径

## 问题

`.schema.yaml` 型 prompt 只能通过 `PromptRegistry` 的命名解析加载。一旦 `prompts.system` 配置为显式路径（含目录分隔符或绝对路径），`_load_system_prompt` 会走 `load_prompt`（纯 `{{ }}` 插值），YAML 骨架（`prompt:` / `composition:` / `- template: |`）会整段漏进 system prompt。同时 registry 的 base_dir 被写死为 `"prompts"`，无法由配置驱动。

## 现状

- `weave/agent.py:59-63`：`PromptRegistry(base_dir="prompts", ...)`，base_dir 写死、相对 CWD。
- `weave/agent.py:_load_system_prompt`：只有「裸名 → `registry.get(name)`」或「路径在 `prompts/` 下 → `relative_to` → registry」两条路会触发 schema 解析；其余路径落到 `load_prompt()`。
- `weave/prompts/loader.py:load_prompt`：只读文本 + `interpolate`，不判断 `is_schema_path`。
- `weave/prompts/prompt_registry.py:get`：按 `.schema.yaml/.yml/.json` 顺序发现并 `load_prompt_schema`。

## 影响

宿主若想把 prompt 与其它适配文件（配置/代码）放在同一目录（如 `weave_adapter/prompts/system.schema.yaml`），做不到——schema 文件只能放 CWD 下的 `prompts/`。demo 里 prompt 被迫拆到 `demo/prompts/`，破坏了「适配文件集中一个目录」的目标。

## 建议

1. registry base_dir 改为可配置（如 weave.yaml 的 `prompts.base_dir`，默认 `prompts` 保持向后兼容）。
2. `_load_system_prompt` 对显式路径先 `is_schema_path()` 判断：是 schema 走 `load_prompt_schema`，否则 `load_prompt`。

## 讨论记录

2026-08-30, Asher & Claude.

基于三人设 demo（prompt 需与 weave_adapter 同目录）发现。
