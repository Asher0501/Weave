# encapsulation-review —— 封装空间的评审与触发条件（决定记录）

**这不是待办清单，是决定记录。** weave 的封装边界只由一条判据决定：
**需要跨多次交互才成立的，都不在本维度。** 下面写的是"考虑过、条件未到、暂时不做"，以及"永不做"。

刻意不放进 `docs/`：门面文档只描述**现在有**的东西。

## 筛子：一个封装点要过四问

1. 只关乎**一次**交互？（跨多次 → 出局）
2. 不引入新概念、不新增挂载点？（新增 → 花掉一格正交性预算）
3. 不含业务决定（执行谁做、循环几轮、历史保留什么）？
4. 不引入依赖？（core 零第三方）

判据一句话：**让"易错的契约细节"只有一处实现 = 可以；替调用方"做决定" = 不可以。**

## 已做

**A1**：`LLMResponse.as_message()` + `Message.tool_result(call, output)` —— 工具往返的两步搬运
（assistant 回填、`tool_call_id` 对应）收成一处。纯数据构造：不执行、不循环、不做策略；
契约面零新增（既有类型上的方法）。测试见 `tests/v04/test_envelopes.py` 末节。

## 未做，附触发条件

| 候选 | 价值 | 触发条件 |
|---|---|---|
| **A2** `async with weave.llm(...)` | 资源释放由库兜住，少一次"忘关" | 已经被"忘记 `aclose`"咬过一次。注：`try/finally` 已能工作，而 `async with` 会迫使整体重新缩进——diff 噪音大于收益 |
| **A3** 把 `sleep` / `clock` 注入 `ReliabilityPolicy` | 退避时序可断言、预算测试零真实耗时 | 要给 weave 的退避行为加断言，或被"测试真等 0.5 秒"烦到。现状：`run_with_reliability(sleep=, clock=)` 的注入点**存在但对象层传不进去**，所以测试只能 `backoff=0` 抹掉时序或真等 |
| **B1** `arguments` 按 `ToolSchema.parameters` 校验（`decode.py` 预留口） | 模型给错参数能早发现 | 真被"模型给错参数"咬过。要付两笔：失败算哪一类（8 类里没有 validation）+ 依赖（core 零依赖 → 只能放 `weave.llm`）。更倾向在执行前由**调用方的工具层**校验，那里有领域知识 |
| **B2** `Message` 形态工厂（`.text` / `.blocks` / …） | 歧义从"发请求前"提前到"构造时" | 与 A1 合并落地时才划算（同一处 API） |
| 结构化输出 `response_model=` | 省手写 schema | 只能作为**文档配方**（`model_json_schema()` 塞进 `ToolSchema.parameters`）：要引 pydantic（破 core 零依赖），且"不符合就重试"是跨交互循环 |

## 永不做（被判据挡死）

| 封装点 | 被哪一问挡死 |
|---|---|
| 多轮 driver / `ask()` / 自动循环 | 第 1 问：循环与停止判定跨多次交互，是业务 |
| 工具注册表 / 执行器 / handler 表 | 第 1 问 + 需要执行；有归档前科（`ToolRegistry` / `Executor`） |
| 自动把 messages 存进 `StateStore` / 会话日志 | 第 1 问 + 硬门禁：`weave/llm` 不得 import 存储 |
| 上下文裁剪 / 摘要 / token 预算 | 第 1 问；且要分词器依赖（第 4 问） |
| 多厂商路由 / 失败降级 / 批量 gather | 第 1 问：跨动作编排；`asyncio.gather` 本就是调用方一行 |
| 从函数签名生成 `ToolSchema` | 第 2 问：要反射 + 类型→JSON Schema 映射；且会让 weave **第一次"认识工具函数"** |
| 厂商能力表（"这家支持哪些块"） | 第 2 问：永远滞后，违背"不挡厂商未来" |
| 事件类型化（`TypedEvent` 取代 `dict`） | 第 2 问：事件名已冻结 5 个，收益低 |
| 成本估算 | 第 1 问（需价格表）→ 注入的 observer（H5 已定不做） |
