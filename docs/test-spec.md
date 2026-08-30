# Weave — End-to-End Test Specification

## 测试策略

- **LLM Mock**: 绝大多数测试使用 `FakeLLM`，返回预定义响应，不调真实 API
- **真实验证**: Section 15 的少数关键场景使用真实 LLM API 做烟雾测试，验证全链路连通性。这些测试由环境变量 `WEAVE_E2E_REAL_LLM=1` 控制，默认跳过
- **Memory**: 使用临时 SQLite 文件或内存 SQLite（`:memory:`），每次测试独立
- **隔离**: 每个测试用例独立创建 Weave 实例和临时目录
- **覆盖度**: 覆盖所有公开 API、所有 Loop 类型、所有 Memory 访问模式、错误路径

---

## 1. 初始化与配置

### 1.1 最小配置启动
```
Given: weave.yaml 仅包含 llm.provider + llm.model + loop.type=simple + prompts.system
When:  weave = Weave("weave.yaml")
Then:  实例创建成功，Memory 自动创建 default scope（SQLite :memory: 或临时文件）
       weave.status() 返回 agent_name="default", loop_type="simple"
```

### 1.2 无配置文件
```
Given: 指定的 weave.yaml 不存在
When:  Weave("nonexistent.yaml")
Then:  抛出 FileNotFoundError
```

### 1.3 环境变量覆盖配置
```
Given: weave.yaml 中 model: "gpt-4o"
       环境变量 WEAVE_MODEL="claude-opus-4-8"
When:  Weave("weave.yaml")
Then:  config.llm.model == "claude-opus-4-8"
```

### 1.4 环境变量默认值语法
```
Given: 环境变量 WEAVE_MAX_ITER 未设置
       weave.yaml 中 max_iterations: ${WEAVE_MAX_ITER:-5}
When:  Weave("weave.yaml")
Then:  config.loop.max_iterations == 5
```

### 1.5 Claude Code settings.json 集成
```
Given: ~/.claude/settings.json 包含 {"env": {"ANTHROPIC_API_KEY": "sk-claude-xxx"}}
       环境变量未设置 ANTHROPIC_API_KEY
When:  Weave("weave.yaml") with llm.provider=anthropic
Then:  llm.api_key == "sk-claude-xxx"
```

### 1.6 完整配置加载
```
Given: weave.yaml 包含所有字段（llm, loop, memory.scopes, prompts, features, server, logging, agent）
When:  Weave("weave.yaml")
Then:  所有配置段解析正确，无异常
```

### 1.7 空 YAML / 最小 YAML
```
Given: weave.yaml 内容为 "{}" 或仅含注释
When:  Weave("weave.yaml")
Then:  所有字段使用代码默认值，实例创建成功
```

---

## 2. SimpleLoop

> 所有 LLM 调用使用 FakeLLM，返回 `LLMResponse(content="你好，有什么可以帮你？", model="fake", finish_reason="stop")`

### 2.1 基本问答
```
Given: FakeLLM 返回固定文本
When:  result = weave.run("今天天气怎么样？")
Then:  result.output == FakeLLM 的返回值
       result.iterations == 1
       result.elapsed_ms > 0
```

### 2.2 带 context 的调用
```
Given: FakeLLM
       system prompt 模板含 {{ context.topic }}
When:  result = weave.run("开始学习", context={"topic": "推荐系统"})
Then:  LLM 收到的 system prompt 中包含 "推荐系统"
       result.output 非空
```

### 2.3 带 scope_hints 的调用
```
Given: memory.scopes 含 quiz_session（path 含 {quiz_session_id}）
When:  weave.run("开始", scope_hints={"quiz_session_id": "my_session"})
Then:  Memory namespace 为 quiz_session:my_session:stream
       后续查询同一 namespace 可获取本次对话记录
```

### 2.4 SimpleLoop 不调用 Tool
```
Given: FakeLLM，注册了一个 tool
       FakeLLM 的响应不含 tool_calls
When:  result = weave.run("你好")
Then:  tool 从未被调用
       result.output == FakeLLM 响应文本
```

### 2.5 before_think 注入 Memory context
```
Given: 预先写入 stream 历史: user:"之前的问题", assistant:"之前的回答"
When:  weave.run("新问题")
Then:  LLM 收到的 system prompt 包含 "## 历史对话" 段
       其中包含 "之前的问题" 和 "之前的回答"
```

---

## 3. IterativeLoop

### 3.1 单轮 Tool 调用
```
Given: FakeLLM 第 1 次返回 tool_calls=[ToolCall(id="1", name="search", arguments={"q":"x"})]
       第 2 次返回 LLMResponse(content="找到了", tool_calls=None)
       注册了 search tool，返回 {"result": "搜索结果"}
When:  result = weave.run("帮我搜索")
Then:  search tool 被调用 1 次，参数 q="x"
       result.output == "找到了"
       result.iterations == 2
```

### 3.2 多轮 Tool 调用
```
Given: FakeLLM 依次返回:
       调用 A → 调用 B → 无 tool_calls
When:  result = weave.run("复杂任务")
Then:  tool A 和 B 都被调用
       result.iterations == 3
       每次 LLM 调用都包含之前的 tool_result 上下文
```

### 3.3 max_iterations 硬上限
```
Given: FakeLLM 每次都返回 tool_calls=[...]（永不停止）
       max_iterations=3
When:  result = weave.run("无限循环任务")
Then:  第 3 次 LLM 调用后强制终止（不等待 stop_condition）
       result.iterations == 3
```

### 3.4 finish tool 停止条件
```
Given: stop_conditions: [{type: tool_call, name: finish}]
       FakeLLM 返回 tool_calls=[ToolCall(name="finish", arguments={"summary":"完成"})]
When:  result = weave.run("做某事")
Then:  finish tool 被视为停止信号
       result.output 包含 "完成"
       Loop 正常结束（不算异常终止）
```

### 3.5 stop_conditions 组合
```
Given: stop_conditions 同时配置 no_tool_calls + tool_call:finish + text_pattern "DONE"
       FakeLLM 返回 content="任务 DONE"，tool_calls=None
Then:  no_tool_calls 条件触发停止（比 text_pattern 先到达）
       result.output == "任务 DONE"
```

### 3.6 tool_filter 限制可用 Tool
```
Given: 注册了 tool_a, tool_b
       tool_filter=["tool_a"]
When:  weave.run("...", tool_filter=["tool_a"])
Then:  LLM 收到的 tools schema 只包含 tool_a
       tool_b 即使被 LLM 请求也无法执行
       调用结束后 tool 列表恢复为 [tool_a, tool_b]
```

### 3.7 tool_filter 异常时恢复 tool 列表
```
Given: tool_filter=["tool_a"]
       FakeLLM 抛出异常（如 RateLimitError）
When:  weave.run("...") 异常退出
Then:  finally 块确保 tool 列表恢复原状
       后续调用可使用全部 tool
```

---

## 4. Tool 注册与执行

### 4.1 装饰器注册
```
Given: @weave.tool 装饰 sync 函数
When:  调用 weave.run 触发 tool
Then:  tool 被正确注册且可被 LLM 调用
       生成的 JSON schema 包含函数名、描述、参数类型
```

### 4.2 函数引用注册
```
Given: def my_tool(x: int) -> str: ...
       weave.register_tool(my_tool)
When:  LLM 调用 my_tool
Then:  正确执行并返回结果
```

### 4.3 Sync tool 在线程池执行
```
Given: 注册的 tool 是 sync 函数（非 async）
When:  LLM 调用该 tool
Then:  tool 通过 asyncio.to_thread() 在线程池执行，不阻塞事件循环
```

### 4.4 Async tool 直接 await
```
Given: 注册的 tool 是 async def 函数
When:  LLM 调用该 tool
Then:  直接 await 执行，无需 to_thread
```

### 4.5 Tool 抛异常 → 错误信息返回给 LLM
```
Given: tool 执行时 raise ValueError("数据不存在")
When:  LLM 调用该 tool
Then:  tool_result 格式为 {"error": True, "type": "ValueError", "message": "数据不存在"}
       LLM 收到错误并可以在下一轮用不同参数重试
       Loop 不终止
```

### 4.6 调用不存在的 Tool
```
Given: FakeLLM 返回 tool_calls=[ToolCall(name="nonexistent_tool", ...)]
When:  尝试执行
Then:  tool_result.error == "Unknown tool: nonexistent_tool"
       Loop 继续，LLM 看到错误
```

### 4.7 Tool 参数自动生成 JSON Schema
```
Given: def search(query: str, limit: int = 10) -> list[dict]: ...
       注册为 tool
When:  LLM 调用时
Then:  生成的 schema 包含 {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}
```

---

## 5. Memory SDK 公共 API

### 5.1 Stream 写入和读取
```
Given: 激活 scope
When:  weave.memory.stream.append({"role": "user", "content": "你好"}, namespace="default:0:stream")
       msgs = weave.memory.stream.last(5, namespaces=["default:0:stream"])
Then:  len(msgs) == 1
       msgs[0]["role"] == "user"
       msgs[0]["content"] == "你好"
```

### 5.2 Stream 按时间排序
```
Given: 依次写入 msg1, msg2, msg3
When:  msgs = weave.memory.stream.last(2)
Then:  msgs 包含 msg2 和 msg3（最新的 2 条），按时间正序
```

### 5.3 Stream trim 裁剪
```
Given: 写入 10 条消息
When:  weave.memory.stream.trim(5, namespace="default:0:stream")
Then:  stream.last(20) 只返回最近 5 条
```

### 5.4 State 写入和读取
```
Given: weave.memory.state.set("topic", "推荐系统", namespace="default:0:state")
When:  val = weave.memory.state.get("topic", namespace="default:0:state")
Then:  val == "推荐系统"
```

### 5.5 State 覆盖旧值
```
Given: set("topic", "A")
       set("topic", "B")
When:  val = get("topic")
Then:  val == "B"
```

### 5.6 State 删除
```
Given: set("topic", "xxx")
       delete("topic")
When:  val = get("topic")
Then:  val is None
```

### 5.7 State get_all 跨 namespace 合并
```
Given: namespace A 有 {"k1": "v1", "k2": "v2a"}
       namespace B 有 {"k2": "v2b", "k3": "v3"}
       A priority < B（A 更窄）
When:  result = get_all(namespaces=[A, B])
Then:  result == {"k1": "v1", "k2": "v2a", "k3": "v3"}  # k2 取 A 的值
```

### 5.8 Knowledge 写入和搜索
```
Given: add("协同过滤是基于用户行为的推荐算法", namespace="default:0:knowledge")
       add("冷启动问题需要基于内容的推荐", namespace="default:0:knowledge")
When:  results = search("冷启动")
Then:  len(results) >= 1
       results[0].content 包含 "冷启动"
       results[0].score > 0
```

### 5.9 Knowledge 搜索无结果
```
Given: 空 knowledge
When:  results = search("不存在的内容")
Then:  results == []
```

### 5.10 Knowledge 不覆盖
```
Given: add("内容A", namespace="default:0:knowledge")
       add("内容B", namespace="default:0:knowledge")
When:  results = search("内容")
Then:  len(results) == 2  # 两条都存在，追加而非覆盖
```

### 5.11 TTL 过期
```
Given: stream.append(..., ttl=0.01)  # 10ms 过期
       sleep(0.05)
When:  msgs = stream.last(10)
Then:  过期的消息不在结果中
```

### 5.12 Max_items 裁剪（stream append 时触发）
```
Given: stream 配置 max_items=3
       依次 append 5 条
When:  msgs = stream.last(10)
Then:  len(msgs) == 3  # 只保留最近 3 条
```

### 5.13 memory.stats()
```
Given: 写入若干数据
When:  stats = weave.memory.stats()
Then:  返回各 namespace 的条目计数
       total_entries > 0
```

### 5.14 memory.list() 按 scope 筛选
```
Given: 两个 scope 都有数据
When:  weave.memory.list(scope="session")
Then:  只返回 session scope 的 namespace 统计
```

### 5.15 memory.clear(scope="session")
```
Given: session scope 有数据，其他 scope 也有数据
When:  weave.memory.clear(scope="session")
Then:  session scope 的数据被清空
       其他 scope 的数据不受影响
```

---

## 6. Scope & Namespace

### 6.1 多 Scope 激活
```
Given: memory.scopes 定义了 quiz_session, domain, kb_global 三个 scope
       scope_hints = {"quiz_session_id": "qs1", "domain_id": "ml"}
When:  weave.run("...", scope_hints=scope_hints)
Then:  active_scopes 包含:
         quiz_session:qs1 (priority 0)
         domain:ml (priority 10)
         kb_global:default (priority 20)
```

### 6.2 Scope ID 自动生成
```
Given: memory.scopes 含 quiz_session
       scope_hints 未提供 quiz_session_id
When:  weave.run("...")
Then:  quiz_session 的 scope_id 自动生成为 UUID
       该 UUID 在单次 run() 内保持不变
```

### 6.3 Scope 按 priority 排序
```
Given: scope A priority=30, scope B priority=0
When:  active_scopes 列表
Then:  B 排在 A 前面（priority 升序）
```

### 6.4 Namespace 格式正确
```
Given: scope_name="quiz_session", scope_id="abc", access_type="stream"
When:  get_namespace("quiz_session", "stream")
Then:  返回 "quiz_session:abc:stream"
```

### 6.5 默认 Scope（无 memory 配置）
```
Given: weave.yaml 无 memory 段
When:  weave.run("...")
Then:  自动创建 scope "default"，priority=0
       stream + state 使用默认路径
```

### 6.6 Scope 名称对应的 hint key 约定
```
Given: scope 名为 "edit_session"
When:  解析 scope_hints
Then:  查找 key "edit_session_id"
       如果 scope_hints 含 {"edit_session_id": "es1"}，scope_id = "es1"
```

---

## 7. 事件总线 & 流式

### 7.1 weave.stream() 基本流程
```
Given: FakeLLM
When:  async for event in weave.stream("你好"):
        收集所有 event
Then:  收到 event 序列，至少包含 done 事件
       done 事件含 output, elapsed_ms, iterations
```

### 7.2 weave.stream() 异常时收到 error 事件
```
Given: FakeLLM 抛出异常
When:  async for event in weave.stream("..."):
Then:  收到 error 事件，含 message 和 exception
       不收到 done 事件
```

### 7.3 weave.stream() 调用方提前退出
```
Given: FakeLLM 耗时较长
When:  async for event in weave.stream("..."):
         if event.type == "tool_call": break  # 提前退出
Then:  后台任务被 cancel，资源释放
       事件总线订阅者正常清理
```

### 7.4 weave.on() 订阅事件
```
Given: 手动调用 weave.emit("custom_event", {"data": "hello"})
When:  async for event in weave.on("custom_event"):
Then:  收到 type="custom_event", data={"data": "hello"}
```

### 7.5 weave.on() 多类型订阅
```
Given: async for event in weave.on("type_a", "type_b"):
Then:  两种类型的事件都能收到
       退出时退订所有类型
```

### 7.6 无订阅者时 emit 不报错
```
Given: 没有订阅 "never_subscribed" 的消费者
When:  weave.emit("never_subscribed", {...})
Then:  正常返回，无异常
```

---

## 8. REST API

### 8.1 POST /agents/{name}/run
```
Given: FastAPI TestClient + Weave 实例（agent.name="test_agent"）
When:  POST /agents/test_agent/run {"input": "你好"}
Then:  返回 200
       body 含 {"output": ..., "elapsed_ms": ..., "iterations": ..., "memory_updated": {...}}
```

### 8.2 GET /agents/{name}/memory
```
Given: 预先写入 memory 数据
When:  GET /agents/test_agent/memory
Then:  返回 {"stats": {...}, "scopes": [...]}
```

### 8.3 DELETE /agents/{name}/memory
```
Given: 有 memory 数据
When:  DELETE /agents/test_agent/memory
Then:  返回 {"status": "cleared"}
       memory 数据被清空
```

### 8.4 GET /agents/{name}/status
```
Given: Weave 实例
When:  GET /agents/test_agent/status
Then:  返回 {"agent_name": "test_agent", "loop_type": "simple", "is_running": false, ...}
```

### 8.5 WebSocket 流式
```
Given: WebSocket 客户端连接 ws://.../ws/agents/test_agent/stream
When:  发送 {"input": "你好"}
       等待接收消息
Then:  至少收到 run_complete 或 error 事件
       每个 event 格式为 {"type": str, "data": dict, "timestamp": float}
```

### 8.6 WebSocket 客户端断开
```
Given: WebSocket 已连接，arun 正在执行
When:  客户端断开
Then:  服务器端取消后台任务，不泄漏资源
```

---

## 9. 错误处理

### 9.1 LLM 可重试错误（ServerError/NetworkError/RateLimitError）
```
Given: FakeLLM 第 1-2 次抛 ServerError，第 3 次正常返回
       配置了 retry(max_attempts=3)
When:  weave.run("...")
Then:  自动重试 2 次后成功
       result.iterations == 1（重试在 LLM 层，不增加 loop 迭代计数）
```

### 9.2 LLM 不可重试错误（AuthError）
```
Given: FakeLLM 抛 AuthError("API key invalid")
When:  weave.run("...")
Then:  不重试，直接抛 AuthError
```

### 9.3 重试耗尽后抛 RetryExhaustedError
```
Given: FakeLLM 一直抛 ServerError
       max_attempts=3
       未配置 fallback
When:  weave.run("...")
Then:  抛 RetryExhaustedError，含 last_exception 链
```

### 9.4 Tool 执行异常后 Loop 继续
```
Given: ToolA 抛异常，ToolB 正常
       FakeLLM 第 1 轮调用 ToolA → 收到错误 → 第 2 轮调用 ToolB
When:  result = weave.run("...")
Then:  ToolA 的异常被标准化为 tool_result error
       LLM 据此决定调用 ToolB
       Loop 正常完成
       result.iterations == 2
```

### 9.5 async timeout 超时
```
Given: async with timeout(0.1): await slow_operation()  # 实际耗时 > 0.1s
When:  超时
Then:  抛 WeaveTimeoutError
```

---

## 10. 并发

### 10.1 两个 arun() 并发执行
```
Given: 两个 FakeLLM 分别返回不同文本
When:  async with TaskGroup:
         t1 = create_task(weave.arun("问题A", scope_hints={"session_id": "a"}))
         t2 = create_task(weave.arun("问题B", scope_hints={"session_id": "b"}))
Then:  两个 run 各自独立完成
       result_a != result_b（各自使用各自的 FakeLLM 响应）
       两个 session 的 memory 互不干扰
```

### 10.2 并发写入同一 namespace
```
Given: 两个协程同时向同一 namespace 写 stream
When:  并发 append
Then:  所有写入最终都持久化（SQLite WAL 串行化）
       无数据丢失，无死锁
```

### 10.3 并发写入不同 namespace
```
Given: 协程 A 写 namespace X，协程 B 写 namespace Y
When:  并发写入
Then:  互不阻塞，同时完成
```

---

## 11. Prompt 模板

### 11.1 context 变量注入
```
Given: system.md 含 "专攻 {{ context.domain }}"
When:  weave.run("...", context={"domain": "机器学习"})
Then:  system_prompt 渲染结果包含 "专攻 机器学习"
```

### 11.2 环境变量引用
```
Given: WEAVE_MODEL 环境变量="test-model"
       system.md 含 "模型: {{ env.WEAVE_MODEL }}"
Then:  渲染结果为 "模型: test-model"
```

### 11.3 配置引用
```
Given: config.llm.model = "fake-model"
       system.md 含 "使用 {{ config.llm.model }}"
Then:  渲染结果为 "使用 fake-model"
```

### 11.4 scope_hints 合并到 context
```
Given: scope_hints={"session_id": "s1"}
       system.md 含 "会话 {{ context.session_id }}"
Then:  渲染结果包含 "会话 s1"
```

### 11.5 default 过滤器
```
Given: system.md 含 "{{ context.topic | default("通用话题") }}"
       context 未提供 topic
Then:  渲染结果为 "通用话题"
```

### 11.6 变量不存在且无默认值 → 报错
```
Given: system.md 含 "{{ context.missing_var }}"
       context 无 missing_var
Then:  抛 KeyError，信息明确指出哪个变量未找到
```

### 11.7 文件不存在
```
Given: weave.yaml 中 prompts.system: "prompts/nonexistent.md"
When:  加载 system prompt
Then:  抛 FileNotFoundError
```

---

## 12. ScheduledLoop（Phase 3）

### 12.1 单次执行正确
```
Given: ScheduledLoop，FakeLLM 返回固定文本
When:  result = await loop.run(agent, "检查风险")
Then:  result.output == FakeLLM 响应
       result.elapsed_ms > 0
```

### 12.2 失败写入 last_run_error
```
Given: FakeLLM 抛异常
When:  loop._execute_once(agent, "...")
Then:  StateMemory 中 last_run_error 被设置为 None（原来的错误被清除）或记录新错误
       StateMemory 中 last_run_at 被更新
```

### 12.3 handle_shutdown 取消当前任务
```
Given: loop._current_task 正在执行
When:  loop.handle_shutdown()
Then:  task 被 cancel
       _shutting_down == True
```

---

## 13. 边界 & 默认值

### 13.1 空输入
```
Given: weave.run("")
Then:  LLM 收到 content="" 的 user message
       result.output 非空（LLM 仍能响应）
```

### 13.2 极长输入（ContextLengthError 场景）
```
Given: 输入超过模型 context window
       FakeLLM 抛 ContextLengthError
When:  weave.run(very_long_input)
Then:  抛 ContextLengthError，不自动重试
```

### 13.3 scope_hints=None
```
Given: scope_hints=None
When:  weave.run("...", scope_hints=None)
Then:  等价于 scope_hints={}，所有 scope_id 自动生成
```

### 13.4 context=None
```
Given: context=None
When:  weave.run("...", context=None)
Then:  system prompt 渲染时 context 为空 dict
       default 过滤器正常工作
```

### 13.5 tool_filter=None
```
Given: tool_filter=None
When:  weave.run("...")
Then:  所有注册的 tool 可用
```

### 13.6 零 Tool 注册
```
Given: 未注册任何 tool
When:  weave.run("...") with IterativeLoop
Then:  LLM 收到的 tools=None
       因无 tool 可调，直接满足 no_tool_calls 停止条件
       loop 正常结束
```

### 13.7 注册同名 Tool（覆盖）
```
Given: 注册 tool_a（版本1），再注册 tool_a（版本2）
When:  LLM 调用 tool_a
Then:  执行版本2（后者覆盖前者）
```

### 13.8 status() 无历史运行记录
```
Given: 全新 Weave 实例，未调用 run()
When:  weave.status()
Then:  "last_run" == None
       "is_running" == False
```

---

## 14. 集成场景（模拟真实项目）

### 14.1 myKG 场景：自主推荐学习路径
```
Given: FakeLLM，tools: get_kb_status, find_learnable_nodes, suggest_learning_path
       memory.scopes: quiz_session(stream+state), domain(state+knowledge)
       scope_hints: quiz_session_id="user_s1", domain_id="推荐系统"
When:  result = weave.run("分析我当前的学习进度，推荐接下来学什么")
Then:  LLM 调用 get_kb_status → 获取统计
       然后调用 find_learnable_nodes → 获取可学节点
       最终输出建议文本
       quiz_session 的 stream 记录了完整对话
       result.elapsed_ms > 0
```

### 14.2 bePM 场景：风险扫描
```
Given: FakeLLM，tools: get_project_status, scan_risks, get_critical_path
       memory.scopes: workspace(knowledge), project(state+knowledge), session(stream)
       scope_hints: project_id="proj_001"
When:  result = weave.run("检查所有项目，找出有延迟风险的任务并汇报")
Then:  LLM 调用 get_project_status → scan_risks → get_critical_path
       最终输出风险报告
       project scope 的 state 记录了上次扫描结果
```

### 14.3 跨 run() 的 Memory 持久化
```
Given: run_1 写入 state: {"last_topic": "推荐系统"}
       run_2 使用同一 scope_hints
When:  run_2 的 before_think 加载 state
Then:  state 包含 {"last_topic": "推荐系统"}（跨 run 持久化）
```

### 14.4 不同 scope_id 的会话隔离
```
Given: run_A session_id="user_a", run_B session_id="user_b"
       run_A 写入 state: {"progress": 0.5}
When:  run_B 加载 state
Then:  state 不包含 "progress"（不同 scope_id，完全隔离）
```

---

## 测试基础设施

### FakeLLM 设计
```python
class FakeLLM(BaseLLM):
    """可编程的 LLM mock，返回预定义的响应序列。"""
    def __init__(self, responses: list[LLMResponse | Exception]):
        self.responses = responses
        self.call_count = 0
        self.last_messages: list[Message] = []
        self.last_tools: list[dict] | None = None

    async def chat(self, messages, tools=None, ...):
        self.call_count += 1
        self.last_messages = messages
        self.last_tools = tools
        resp = self.responses[self.call_count - 1]
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def chat_stream(self, messages, ...):
        # 逐 token yield FakeLLM 的 content
        resp = await self.chat(messages, ...)
        for char in resp.content:
            yield char
```

### 测试 fixtures
- `tmp_weave_config` — 临时目录 + 临时 weave.yaml
- `fake_llm` — FakeLLM 实例
- `weave_instance` — 注入 FakeLLM 的 Weave 实例
- `temp_db` — 临时 SQLite 数据库

---

## 15. 真实验证（真实 LLM API）

> **前置条件**: 设置 `WEAVE_E2E_REAL_LLM=1` 且 `ANTHROPIC_API_KEY` 已配置。
> 这些测试使用 **真实 Anthropic API**，验证全链路连通性。
> 未设置环境变量时自动跳过（`pytest.mark.skipif`）。
>
> **成本**: 总共约 6 次 API 调用，每次请求 token 数控制在 200 以内，总成本 < $0.01 USD。

### 15.1 真实 SimpleLoop — 基础问答
```
Given: 真实 Anthropic API（model=claude-haiku-4-5-20251001 或最便宜的可用模型）
       SimpleLoop，无 tool
When:  result = weave.run("请用'1, 2, 3'格式列出三个编程语言")
Then:  result.output 非空
       输出中包含至少一种编程语言（Python/JavaScript/Go 等）
       result.iterations == 1
       result.elapsed_ms > 0
```

### 15.2 真实 IterativeLoop — Tool Calling
```
Given: 真实 Anthropic API
       注册一个 add_numbers(a: int, b: int) -> int tool
       IterativeLoop，max_iterations=5, stop_conditions=[no_tool_calls]
When:  result = weave.run("请帮我计算 123 + 456，用 add_numbers 工具")
Then:  LLM 调用 add_numbers(123, 456)
       tool_result = 579
       LLM 在最终输出中包含 579
       result.iterations >= 2  # 至少一轮 tool call + 一轮总结
```

### 15.3 真实 Memory 跨轮持久化
```
Given: 真实 Anthropic API + SQLite Memory
       scope_hints={"session_id": "e2e_test_session"}
       第 1 轮: "记住我最喜欢的编程语言是 Python"
       第 2 轮: (同一 scope_hints) "我最喜欢的编程语言是什么？"
When:  执行两轮 run()
Then:  第 2 轮的 system prompt 中包含第 1 轮的对话历史
       LLM 能正确回答 "Python"（验证 Memory 跨轮生效）
```

### 15.4 真实 stream() — 流式输出
```
Given: 真实 Anthropic API
When:  async for event in weave.stream("请用 50 字介绍 Python"):
        收集所有 event
Then:  至少收到 2 个 token 事件（验证流式生效）
       收到 done 事件，output 非空
       done 事件的 elapsed_ms > 0
```

### 15.5 真实 IterativeLoop — 多 Tool 组合调用
```
Given: 真实 Anthropic API
       注册两个 tool:
         search_kb(query: str) → 返回模拟搜索结果
         summarize(text: str)  → 返回固定摘要
       IterativeLoop
When:  result = weave.run("搜索'微服务架构'并总结结果")
Then:  LLM 先调用 search_kb("微服务架构")
       再调用 summarize(搜索结果)
       最终输出包含摘要
       result.iterations >= 2
```

### 15.6 真实错误处理 — 不存在的 Tool
```
Given: 真实 Anthropic API + 注册一个 dummy tool
       LLM 故意被要求调用不存在的 tool（通过预写入不存在的 tool schema 场景）
       或更简单：注册一个会返回异常的工具，验证异常被正确处理
When:  注册 failing_tool 直接 raise ValueError("模拟失败")
       LLM 调用该 tool
Then:  tool_result 以 error 格式返回给 LLM
       LLM 根据错误调整策略（换 tool 或告知用户失败）
       Loop 不崩溃
```
