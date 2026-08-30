---
name: weave-sdd
description: Weave 项目的规范驱动开发（SDD）SKILL。基于 DESIGN.md 和 docs/issues/ 中的设计决策，生成符合 Weave 架构规范、接口约定和代码风格的 Python 实现代码。当用户提到"实现 XXX 模块""生成 XXX 代码""按设计文档写代码""开始 Phase X"时触发。
---

# Weave SDD — 规范驱动代码生成

基于 `DESIGN.md` 规范，为 Weave 项目生成符合架构设计的 Python 代码。

## 核心原则

1. **DESIGN.md 是唯一真相源** — 所有代码必须对齐 DESIGN.md 中的接口、类型、命名
2. **docs/issues/ 是决策记录** — 涉及 async/sync、memory 模型、事件总线等设计取舍时，必须先读对应的 issue
3. **Phase 有序推进** — 按 DESIGN.md §10 路线图的 Phase 顺序实现，先核心后 Feature
4. **零硬编码** — 配置从 YAML 加载、Prompt 从文件加载、API Key 从环境变量读取
5. **接口先行** — 先定义抽象基类（ABC），再写具体实现

## 工作流

```
读取 DESIGN.md → 查 docs/issues/ → 生成代码 → 对齐命名与类型 → 验证
```

### Phase 0: 启动前必读

按优先级加载：

1. **`DESIGN.md`** — 完整架构（重点读对应的 §4.x 模块设计 + §5 目录结构 + §8 配置规范）
2. **`docs/issues/`** — 按需加载：
   - 涉及 async/sync → `001-async-vs-sync.md`
   - 涉及事件/流式 → `002-event-bus.md`
   - 涉及记忆存储 → `003-memory-model.md`
3. **已有代码** — 检查是否已有相关模块，增量修改而非覆盖

### Phase 1: 确认范围

向用户确认：
- 实现哪个（哪些）模块？
- 属于哪个 Phase？（Phase 1=核心 / Phase 2=高级 / Phase 3=扩展 / Phase 4=Server）
- 是否有特殊约束？

### Phase 2: 生成代码

按以下规范生成每段代码：

#### 命名与类型对齐表

生成代码前，对照以下映射确保一致性：

| DESIGN.md 定义 | 代码中的命名 | 说明 |
|---------------|-------------|------|
| `StreamMemory` | `weave/memory/stream.py` | 时序消息流 |
| `StateMemory` | `weave/memory/state.py` | 键值状态 |
| `KnowledgeMemory` | `weave/memory/knowledge.py` | 知识搜索 |
| `BaseLoop` | `weave/loop/base.py` | Loop 抽象 |
| `SimpleLoop` | `weave/loop/simple.py` | 单次请求-响应 |
| `IterativeLoop` | `weave/loop/iterative.py` | 多轮 Tool 调用 |
| `BaseLLM` | `weave/llm/base.py` | LLM 适配器抽象 |
| `LLMResponse` | `weave/llm/base.py` 中的 dataclass | content/tool_calls/model/usage/finish_reason |
| `LLMError` 体系 | `weave/llm/errors.py` | RateLimitError/AuthError/ContextLengthError/ServerError/NetworkError |
| `extract_json` | `weave/utils/json_extract.py` | A1: LLM JSON 提取 |
| `async_retry` | `weave/utils/retry.py` | A2: 指数退避重试 |
| `async_timeout` | `weave/utils/timeout.py` | A3: 超时包装 |
| `event_bus` | `weave/event_bus.py` | A5: 进程内 pub/sub |
| `timed_async` | `weave/utils/timed.py` | C1: 计时器 |
| `fallback` | `weave/utils/fallback.py` | C2: 错误兜底 |
| `lazy_registry` | `weave/utils/registry.py` | C3: 惰性注册表 |
| `validate_schema` | `weave/features/schema_validation.py` | B1: Schema 校验 |
| `structured_call` | `weave/features/structured_call.py` | B2: 结构化 LLM 调用 |
| `two_stage_call` | `weave/features/two_stage.py` | B3: 两阶段流水线 |
| `sanitize_input` | `weave/features/prompt_defense.py` | B4: 注入防御 |
| `load_prompt` | `weave/prompts/loader.py` | 单文件 Prompt 加载 + 变量替换 |
| `load_prompt_schema` | `weave/prompts/loader.py` | Schema 文件加载 + 渲染为完整 Prompt |
| `PromptSchema` | `weave/prompts/schema.py` | Prompt 结构描述（composition, separator） |
| `FilePart` / `TemplatePart` / `SwitchPart` | `weave/prompts/schema.py` | Schema 的三种 Part 类型 |
| `PromptRegistry` | `weave/prompts/prompt_registry.py` | 命名 Prompt 注册表（自动识别 .md / .schema.yaml） |
| `SearchResult` | namedtuple(id, content, score, metadata) | KnowledgeMemory 搜索返回值 |
| `LoopResult` | namedtuple(output, elapsed_ms, iterations, memory_updated) | run() 返回值 |

#### 接口一致性规则

1. **Memory 接口** — 写入方法 `namespace: str`（单数），读取方法 `namespaces: list[str]`（复数，None=全部激活）
2. **Loop 钩子** — `before_think(self, agent, current_input: str) -> dict`
3. **Tool 约定** — sync tool 通过 `asyncio.to_thread()` 桥接，async tool 直接 await
4. **Config** — 支持 `${VAR:-default}` 语法，Scope 配置含显式 `priority` 字段

#### 代码风格

- Python 3.11+ 语法（`str | None` 而非 `Optional[str]`）
- 所有 I/O 操作用 `async/await`
- 公开 API 同时提供 `weave.run()` (sync) 和 `weave.arun()` (async)
- 内部 `_run_impl()` 是唯一的 async 实现
- SQLite 默认后端，WAL 模式，check_same_thread=False
- 类型注解完整，公开接口有 docstring

### Phase 3: 验证

生成代码后自查：
- [ ] 文件名和目录路径与 §5 目录结构一致
- [ ] 类名和方法签名与 §4 接口定义一致
- [ ] 命名是 stream/state/knowledge（非旧的 conversation/working/long_term）
- [ ] 配置 key 来自 §8.1 配置参考
- [ ] 无硬编码 API Key、无内联 Prompt 字符串
- [ ] 错误类型来自 `weave/llm/errors.py`（非裸字符串）

---

## 按 Phase 生成的具体指南

### Phase 1 模块（核心 SDK）

```
weave/
├── __init__.py          # 导出 Weave, WeaveConfig
├── agent.py             # Weave 类 — 编排器
├── config.py            # YAML + ENV 配置加载
├── types.py             # Message, ToolCall, LoopResult, SearchResult
├── event_bus.py         # A5: 事件总线
├── loop/
│   ├── base.py          # BaseLoop ABC
│   └── simple.py        # SimpleLoop
├── memory/
│   ├── base.py          # StreamMemory/StateMemory/KnowledgeMemory ABC
│   ├── manager.py       # MemoryManager — scope + namespace 管理
│   ├── stream.py        # StreamMemory 实现
│   ├── state.py         # StateMemory 实现
│   ├── knowledge.py     # KnowledgeMemory 实现 (FTS5)
│   └── backends/
│       └── sqlite.py    # SQLite 后端
├── llm/
│   ├── base.py          # BaseLLM ABC + LLMResponse
│   ├── anthropic.py     # Anthropic adapter
│   ├── openai.py        # OpenAI adapter
│   ├── factory.py       # 自动检测
│   └── errors.py        # LLM 错误类型
├── prompts/
│   ├── loader.py            # .md 文件加载 + Schema 渲染
│   ├── schema.py            # Prompt Composition Schema 解析器
│   └── prompt_registry.py   # 自动识别 .md / .schema.yaml
└── utils/
    ├── json_extract.py  # A1
    ├── retry.py         # A2
    ├── timeout.py       # A3
    ├── timed.py         # C1
    ├── fallback.py      # C2
    └── registry.py      # C3
```

### Phase 2 模块

```
weave/
├── loop/
│   └── iterative.py     # IterativeLoop + 停止条件
├── memory/
│   ├── backends/
│   │   └── file.py      # JSON 文件后端（兼容 myKG/bePM）
│   └── manager.py       # 增强：跨 scope 合并、TTL 清理
└── features/
    ├── schema_validation.py  # B1
    ├── structured_call.py    # B2
    └── two_stage.py          # B3
```

### Phase 3 模块

```
weave/
├── loop/
│   └── scheduled.py     # ScheduledLoop + 优雅关闭
├── memory/
│   ├── backends/
│   │   └── chroma.py    # ChromaDB 向量后端
│   └── knowledge.py     # 增强：ChromaDB 搜索
└── features/
    └── prompt_defense.py # B4
```

### Phase 4 模块

```
weave/
└── server/
    ├── app.py           # FastAPI app 工厂
    ├── routes.py        # /agents/{name}/* 路由
    └── ws.py            # WebSocket + event_bus 桥接
```

---

## 决策速查

> 实现时遇到以下问题，直接按既定决策执行，不要重复讨论：

| 问题 | 决策 | 来源 |
|------|------|------|
| async vs sync API | 内部 async，对外 `run()` sync + `arun()` async | `docs/issues/001-async-vs-sync.md` |
| 事件通知机制 | `event_bus.py` — `asyncio.Queue` 阻塞式，不丢消息 | `docs/issues/002-event-bus.md` |
| Memory 访问模式 | stream / state / knowledge（非 conversation/working/long_term） | `docs/issues/003-memory-model.md` |
| Memory 作用域 | 项目自定义 scopes，显式 `priority` 字段 | DESIGN.md §4.2 |
| Memory 默认后端 | SQLite，WAL 模式，单文件 | DESIGN.md 决策 Q3 |
| Memory 隔离 | namespace `{scope}:{id}:{access_type}` 行级隔离 | DESIGN.md §4.2 |
| LLM 错误 | 类型化异常 `RateLimitError` / `AuthError` 等 | DESIGN.md §4.6 A4 |
| 重试耗尽 | 抛 `RetryExhaustedError` → fallback → StateMemory 记录 | DESIGN.md §4.6 A2 |
| Tool 错误 | 标准化为 error result 返回 LLM，不终止 Loop | DESIGN.md §4.5 |
| Scope ID key | `{scope_name}_id` 格式（如 scope "quiz_session" → "quiz_session_id"） | DESIGN.md §6.1 |
| Scope 优先级 | `priority` 越小越"窄"，同 key 窄覆盖宽 | DESIGN.md §4.2 |
| TTL 清理 | 读时过滤 + 写时被动批量清理（LIMIT 100），max_items 优先于 TTL | DESIGN.md §4.2 |
| Prompt 模板变量 | 优先级: context > env > config > default，scope_hints 合并到 context | DESIGN.md §4.4 |
| Features 配置 | `features:` 块，每个 feature 独立开关 | DESIGN.md §4.6 |
