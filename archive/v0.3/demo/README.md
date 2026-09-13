# Weave 三人设对话 Demo

三个人设（理性分析者 / 激进冒险者 / 保守谨慎者）共享**同一份客观上下文**（外部任务与数据），
但**各自的会话、记忆、处理结果的保存彼此隔离**。命令行输入一个问题，三个人设分别作答。

## 运行

```bash
cd demo
python main.py           # 离线（FakeLLM，无需 API Key，保证能跑通）
python main.py --real    # 真实模型
```

真实模型 provider / model 由 `weave.yaml` 里的 env 占位符解析，默认：

- `provider: anthropic`（读取 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`；本机 Claude Code 环境会经由
  `ANTHROPIC_BASE_URL` 指向的网关转发）
- `model: deepseek-chat`（能正常出文本；`deepseek-v4-pro` 是推理模型，输出在 thinking block，暂不提取）

按需覆盖：

```bash
# DeepSeek 直连
LLM_PROVIDER=deepseek WEAVE_MODEL=deepseek-chat DEEPSEEK_API_KEY=sk-xxx python main.py --real
# OpenAI
LLM_PROVIDER=openai WEAVE_MODEL=gpt-4o-mini OPENAI_API_KEY=sk-xxx python main.py --real
```

交互命令：

| 输入 | 作用 |
|------|------|
| 任意问题 | 三个人设分别作答 |
| `/report` | 查看隔离证据（共享上下文 + 各人设私有记忆） |
| `/help` | 帮助 |
| `/quit` / `exit` / `Ctrl+C` | 退出 |

## 目录结构与隔离原则

```
demo/
├── main.py                     # 入口：CLI 组装，无业务规则
├── business/                   # ★ 业务层（零 weave 依赖）
│   ├── models.py               # 领域模型（纯 Python 类型）
│   ├── personas.yaml           # 三个人设（数据）
│   ├── objective.yaml          # 共享客观事实（数据）
│   └── loader.py               # YAML → 模型
├── prompts/                    # Weave prompt（registry base_dir="prompts" 约定位置）
│   └── system.schema.yaml      # 人设 system prompt（YAML schema）
└── weave_adapter/              # ★ 适配层（唯一 import weave）
    ├── weave.yaml              # Weave 配置（双 scope）
    ├── adapter.yaml            # 业务↔weave 映射参数
    ├── fake_llm.py             # 离线假 LLM
    └── agent.py                # PersonaAgent facade
```

- **依赖方向**：`business/` 绝不 `import weave`；`weave_adapter/` 是唯一 import weave 的地方。
- **零硬编码**：人设、客观事实、prompt 正文、模型/scope 配置、适配映射全部来自 YAML。

## 核心映射

| 业务概念 | weave 概念 |
|----------|-----------|
| `Persona.id` | `persona_id` scope_hint → `persona:{id}:stream/state` |
| `Persona.name` / `.role` | system prompt 模板变量 `{{ persona_name }}` / `{{ persona_role }}` |
| 用户输入（客观任务） | `arun(input=...)`，三人设收到同一份 |
| `ObjectiveFact`（共享客观数据） | `world:shared:state` 的 `objective_facts`（全量注入） |
| 人设最近回答（保存结果） | `persona:{id}:state` 的 `last_answer` |

## 如何验证「共享客观 + 内部隔离」

1. 连续输入两个问题（如「客户投诉怎么办？」「那延迟呢？」）。
2. 输入 `/report`，会看到：
   - **共享客观上下文**：三个人设的 `shared_facts` 是同一份。
   - **各人设私有记忆**：三行 `history_count` 各自独立增长，`最后回答` 各自不同、互不串。
3. 因为三个人设的 `before_think` 只注入自己的 `persona:{id}:stream`，所以对某个人设追问时，它只记得自己此前的对话。
