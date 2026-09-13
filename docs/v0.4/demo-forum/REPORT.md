# 14_forum（agora）× weave v0.4 — 真实 LLM 接入 Demo

> 目的：验证 weave v0.4 组件库在**外部真实项目**中的业务无关接入，并跑通**真实 LLM 调用**。
> 时间：2026 会话记录 · 状态：✅ 成功（12 轮接力，`fixed_rounds` 收敛停止）

## 汇总表

| 维度 | 内容 |
|------|------|
| **背景 · 项目** | `../14_forum` = **agora**：基于 weave_agent_sdk(v0.3) 的多方接力协作引擎；brainstorm 场景为纯声明式配置（3 角色 round_robin + fixed_rounds 判停） |
| **背景 · 动机** | 证明 v0.4「业务全在外部、weave 只做业务无关机制」：LLM 通信与可靠性由 weave v0.4 承担，agora 业务（角色/prompt/接力/停止）零改动 |
| **背景 · 接入方式** | 唯一改动点：`14_forum/agora/adapter/llm.py` —— 把 `weave_agent_sdk.create_llm` 替换为 **weave v0.4 `OpenAICompatibleProvider`**（薄适配 `complete(prompt)->str`）；Repository/场景/CLI 均未动 |
| **运行环境** | Provider 内嵌重试/超时/类型化错误（`RetryPolicy` 默认值）；凭证取自 `14_forum/weave.yaml` 的 DeepSeek key → **仅进程环境变量**（不回显/不落盘）；端点 `https://api.deepseek.com/v1`，模型 `deepseek-chat`；DB 落于工作区新文件（旧库 schema 不变、不影响 agora 其它 run） |
| **运行命令** | `python run_agora_demo.py run --config scenarios/brainstorm.yaml --topic "为 weave v0.4 组件库设计一段 5 分钟开发者上手教程的开篇方案" --db data/forum_demo_run1.db`（工作目录 = 14_forum） |
| **运行记录 · 过程** | `run_id=3642c26e-9367-4445-8be5-008a8c190e9a`；skeptic → optimizer → devil 按序接力共 **12 轮**，每轮真实请求 DeepSeek 并返回；进度逐轮落 stderr（`[n] <role> 生成中…/完成`） |
| **运行记录 · 产物** | 共享转录 12 条全部持久化（`agora.db` 新库，v0.3 Repository）；运行日志 `run-1.log`（45KB，UTF-8）与逐轮抽样 `run-1-turns-sample.txt` 已归档于本目录 |
| **运行记录 · 内容抽样** | ①skeptic：质疑"5 分钟是否真的给开发者最高价值"；②optimizer：提出"从演示秀重构为问题解决契约"；③devil：给出"揭伤疤→差异→重构→行动号召"的风险分析；…⑪optimizer：收敛到"认知脚手架/最小成功标准（MSC）"（主题自洽、逐轮深化、无重复模板） |
| **结果 · 停止** | `status=stopped termination=fixed_rounds converged=False conclusion=None turns=12`——达到场景配置的最大轮数正常停止（brainstorm 判停语义，非错误） |
| **总结 · v0.4 验证点** | ①真实网络 + 真实 Key 走通 OpenAI 兼容 Provider；②消息类型化往返（`weave.core.types.Message`）；③可靠性内嵌（Provider 层，独立于任何 Agent/Loop 使用）；④业务无关性实证：agora 仅把 `LLM.complete(prompt)` 当作原子，v0.3→v0.4 切换只改一个适配文件；⑤与 v0.3 共存：Repository 等其它集成点不受影响 |
| **总结 · 成本与边界** | 12 次 DeepSeek 调用（小额 token）；demo 不覆盖工具循环/多 Agent 记忆（agora 的 LLM 原子只需单轮文本）；reasoner 特性未在本 demo 触发（可用 `deepseek-reasoner` 单回合另验） |
| **后续** | ①把 agora 的 Repository 也迁到 v0.4 `SQLiteStateStore`（Listable/Checkpoint 能力可直接复用）；②reasoner 剥离单测已覆盖（`tests/v04/test_provider.py`）；③根 README 已按 I4 重写（A8） |

## 佐证文件

- `run-1.log` — 完整运行记录（run_id、进度、12 轮转录、状态行）
- `run-1-turns-sample.txt` — 逐轮首句抽样
- 补丁：`14_forum/agora/adapter/llm.py`（weave v0.4 版，含归一化说明注释）
