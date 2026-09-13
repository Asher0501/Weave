# Issue #009: 缺少公开的 LLM 注入点（离线/测试）

## 问题

`Weave.__init__` 直接调用 `create_llm(...)`，且无 key 时抛 `ValueError`。宿主没有公开途径注入自定义 `BaseLLM`（如 FakeLLM），离线 demo / 单元测试只能改环境变量塞 dummy key + 覆盖 `weave._llm` 内部属性。

## 现状

- `weave/agent.py:50-63`：构造器无条件 `self._llm = create_llm(...)`。
- `docs/basic.md` §7 已声明「FakeLLM 做单元测试」，但无对应公开通道。
- demo 离线实现：`os.environ.setdefault(...)` 塞三个 dummy key，再 `self._weave._llm = FakeLLM()`（触碰 `_` 前缀内部属性，违反 public-api.md 的稳定性承诺）。

## 影响

- 离线/无 key 场景无法用公开 API 干净启动。
- 依赖内部属性，未来重构即破坏宿主。

## 建议

1. `Weave.__init__` 增加可选 `llm: BaseLLM | None = None`（或 `llm_factory: Callable`）：传入时跳过 `create_llm`，否则保持现有行为（向后兼容）。
2. 对应在 `docs/public-api.md` 补充该参数为稳定接口。

## 讨论记录

2026-08-30, Asher & Claude.
