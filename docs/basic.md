# Weave — 基础事实与约束

> 任何基于事实来更新本项目的会话，**必须先读取本文档**。
>
> 这里记录的每一项都是不可协商的。违反任何一条 = 破坏了项目的基本设计。

---

## 1. 项目是什么

**Weave** = 非侵入式、基于 SDK 的 Agent 插件框架。

只做两件事：**Loop（循环）** + **Memory（记忆）**。不做复杂 Agent 框架、不做 Multi-Agent、不做 Workflow 编排。

一句话：> 把一个纯"请求-响应"的项目，变成一个"能迭代、能记住"的 Agent，只需 5 行代码。

---

## 2. 设计红线（绝对不能破）

| # | 红线 | 解释 |
|---|------|------|
| **R1** | 代码中**绝不出现**硬编码的 API Key | 所有凭证从环境变量或 `~/.claude/settings.json` 读取 |
| **R2** | 代码中**绝不出现**硬编码的 Prompt | 所有 prompt 从 `.md` 文件加载，模板变量注入 |
| **R3** | 代码中**绝不出现**硬编码的模型名 | 从 `weave.yaml` 或环境变量读取 |
| **R4** | 宿主项目的原有代码**一行都不动** | Weave 是 `pip install` + `import` + 配置 = 胶水代码，不侵入宿主架构 |
| **R5** | SDK 和 REST API 暴露**同一抽象层级** | `weave.run("...")` 和 `POST /agents/default/run` 是等价操作，不存在"先有 REST 再包装 SDK" |
| **R6** | **不内置任何 Tool** | Tool 100% 由宿主项目定义。Weave 不给 Agent 预设任何能力 |
| **R7** | 配置的**唯一来源**是 `weave.yaml` + 环境变量 | 不存在"在代码里 `if DEBUG:` 换配置"的模式 |

---

## 3. 架构分层

```
Integration Layer  →  Python SDK + REST Server    (对外，不可变)
Core Layer         →  Weave Agent                 (编排 Loop + Memory + Tool Registry)
                      ├── Loop Engine             (simple / iterative / scheduled)
                      └── Memory Manager           (stream / state / knowledge)
Adapter Layer      →  LLM Adapters / Store Adapters / Prompt Loader  (可插拔)
```

**规则**：上层依赖下层，下层不感知上层。Adapter 可平行扩展，Core 接口不变。

---

## 4. 核心概念

### 4.1 Loop — 三种策略

| 策略 | 行为 | 何时用 |
|------|------|--------|
| `simple` | 一次请求 → 一次响应 → 结束。**不调 Tool** | 问答、翻译、摘要 |
| `iterative` | LLM 反复调用 Tool 直到停止条件 | Tool-use、多步推理 |
| `scheduled` | cron 或事件驱动，定时自动执行 | 监控、定期扫描 |

> **默认值是 `iterative`。** 注册了 Tool 后无需显式设置 `loop.type`；
> 若只要单次问答、不暴露任何 Tool，可显式设置 `loop.type: simple`。

停止机制（iterative）：
- `max_iterations` — 硬上限（**必配**），防无限循环
- `stop_conditions` — 语义停止（可选）：`no_tool_calls` / `tool_call:finish` / `text_pattern`

### 4.2 Memory — 三种访问模式

| 模式 | 操作 | 比喻 |
|------|------|------|
| `stream` | append / last(N) / trim | 对话历史，时序流 |
| `state` | set / get / delete / get_all | 键值状态，新值覆盖旧值 |
| `knowledge` | add / search(query, top_k) | 知识库，追加不覆盖，语义搜索 |

**关键约束**：
- **时间维度 = TTL**，不是独立的 Memory 类型。每条记忆可配过期时间
- **作用域由项目自定义**。Weave 不内置 system/project/session 层级。接入项目在 `weave.yaml` 中定义自己的 scope
- **Namespace 隔离**：格式 `{scope_name}:{scope_id}:{access_type}`，单表行级隔离
- **Scope 优先级**：`priority` 越小越"窄"，同 key 时窄 scope 覆盖宽 scope

### 4.3 命名

- Memory 的三种访问模式叫 **stream / state / knowledge**（不是 conversation / working / long_term）
- Scope 命名：由项目自定义（如 `quiz_session`, `domain`, `workspace`）
- Scope hint key 约定：`{scope_name}_id`（如 scope 名 `quiz_session` → hint key `quiz_session_id`）
- Namespace 格式：`{scope_name}:{scope_id}:{access_type}`

---

## 5. 公开 API 承诺（不可变）

详见 `docs/public-api.md`。核心约束：

1. **`weave.run()` / `weave.arun()` / `weave.stream()` 的签名不变**
2. **`LoopResult` 的 4 个字段（output, elapsed_ms, iterations, memory_updated）不变**
3. **`stream()` 的 5 种事件类型（token, tool_call, tool_result, done, error）和 schema 不变**
4. **`weave.yaml` 配置格式向后兼容**——新增字段可以，已有字段的行为不能改变
5. **所有 `_` 前缀的方法/属性是内部实现**，可随时删除或重命名
6. **REST API 端点路径和方法不变**

---

## 6. 配置哲学

```yaml
# weave.yaml — 所有配置的唯一来源
llm:
  provider: anthropic          # anthropic | openai | deepseek
  model: ${WEAVE_MODEL:-claude-sonnet-5-20251001}
  max_tokens: ${WEAVE_MAX_TOKENS:-4096}
  temperature: ${WEAVE_TEMP:-0.7}
  # api_key 绝不在此出现

loop:
  type: iterative
  max_iterations: ${WEAVE_MAX_ITER:-10}
  stop_conditions:
    - type: no_tool_calls

memory:
  scopes:
    session:                   # priority 越小越窄
      stream:
        backend: sqlite
        path: ${WEAVE_DATA_DIR:-./data}/memory.db
        ttl: 3600
      state:
        backend: sqlite

prompts:
  system: prompts/system.md    # .md 文件，模板变量
  loop_instruction: prompts/loop.md
```

**规则**：
- `${VAR:-default}` 语法用于环境变量覆盖
- `api_key` 从 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` 环境变量或 `~/.claude/settings.json` 读取
- 不写 `memory:` 段时自动创建默认 scope（SQLite, `./data/memory.db`）
- Prompt 模板变量优先级：`context` → `env` → `config` → default 过滤器

---

## 7. 技术事实

| 项目 | 事实 |
|------|------|
| 语言 | Python 3.11+ |
| 默认存储 | SQLite（WAL 模式），单文件 |
| 并发模型 | 内部 async，对外 `run()`(sync) + `arun()`(async) |
| 包管理 | `pyproject.toml`，`pip install -e .` |
| 依赖 | 核心零强制依赖（`pyyaml` 除外）；Anthropic/OpenAI/ChromaDB/FastAPI 均为 optional extras |
| LLM SDK | Anthropic Python SDK + OpenAI Python SDK |
| 服务框架 | FastAPI（可选，`pip install weave[server]`） |
| WebSocket | 通过 EventBus → WebSocket 桥接 |
| 测试 | FakeLLM 做单元测试，`WEAVE_E2E_REAL_LLM=1` 做真实验证 |

---

## 8. 常见反模式（不要这样做）

| ❌ 反模式 | ✅ 正确做法 |
|-----------|-------------|
| 在 `Weave.__init__` 里写死 `model="claude-sonnet-5"` | 从 `weave.yaml` 读取 |
| 在代码里 `f"你是{name}助手"` 拼 prompt | 写 `prompts/system.md`，用 `{{ name }}` 模板 |
| 给 Weave 内置一个 `web_search` tool | Tool 100% 由宿主注册 |
| 把 `llm.max_tokens` 写到 `LoopConfig` 上 | `LLMConfig` 和 `LoopConfig` 各管各的 |
| 新增一个 `permanent` Memory 类型 | 用 TTL=null 表示永不过期，不新增类型 |
| 把 Memory 的 stream/state/knowledge 叫成 conversation/working/long_term | 这两个命名体系不能混用，只用前者 |
| 在 Loop 中用 `len(messages)` 估算写入数 | 实际追踪写入次数 |
| 在 `__init__.py` 导出内部实现类 | 只导出稳定公开 API |
| 假设 `asyncio.run()` 总是可用 | 先 `get_running_loop()` 检查已有事件循环 |

---

## 9. 目录约定

```
weave/                      # Git 仓库根
├── pyproject.toml
├── weave.yaml              # 默认配置（示例）
├── DESIGN.md               # 设计文档
├── doc/                    # 项目文档
│   └── basic.md            # 本文档
├── docs/                   # 设计讨论
│   ├── issues/             # 决策记录
│   ├── test-spec.md        # 测试规格
│   └── public-api.md       # 公开接口清单
├── weave/                  # Python 包
│   ├── __init__.py
│   ├── agent.py            # Weave 类
│   ├── config.py           # 配置加载
│   ├── types.py            # 共享类型
│   ├── loop/               # 循环策略
│   ├── memory/             # 记忆系统
│   ├── llm/                # LLM 适配
│   ├── utils/              # 内置工具
│   ├── event_bus.py        # 事件总线
│   ├── features/           # 可选功能
│   ├── prompts/            # Prompt 管理
│   └── server/             # REST Server
├── prompts/                # 宿主项目 prompt 模板
│   ├── system.md
│   └── loop.md
├── examples/               # 集成示例
└── tests/                  # 测试
```

---

## 10. 相关文档

| 文档 | 内容 |
|------|------|
| `DESIGN.md` | 完整设计文档 |
| `doc/basic.md` | 本文档 — 基础事实与约束 |
| `docs/public-api.md` | 对外公开接口清单 |
| `docs/test-spec.md` | E2E 测试用例规格 |
| `docs/issues/001-async-vs-sync.md` | Async/Sync 设计决策 |
| `docs/issues/002-event-bus.md` | 事件总线设计 |
| `docs/issues/003-memory-model.md` | Memory 模型设计 |
| `raw.txt` | 项目原始需求 |
