# Issue #010: 插件化承诺的落地 —— 统一注册表与注入点

## 结论

Weave 定位为可插拔框架（basic.md §3「Adapter 可平行扩展」、public-api.md §2「扩展点」），
但三处 adapter 分发现状是硬编码的 `if/elif`，宿主导入自定义实现必须改框架源码。
**采用「统一 Registry + 可选实例注入」：内置实现从「特例」降级为「预注册的默认值」。**

## 问题抽象

三个 adapter 层（LLM / Loop / Memory backend）的「配置字符串 → 实现」映射被写死，
且**同一个枚举被重复硬编码在「分发 + 校验 + 报错」三处**：

| 层 | 分发（写死） | 校验（写死） | 报错（写死） |
|----|------------|------------|------------|
| LLM | `create_llm` 的 `if provider == ...` | `_validate_raw` 白名单 | `"Supported: anthropic, openai, deepseek"` |
| Loop | `_create_loop` 的 `if loop_type == ...` | 同上 | `"Supported loop types: ..."` |
| Memory | `_get_backend_for_namespace` 的 `if backend == ...` | — | `"Unknown backend ... falling back to sqlite"` |

根因：缺「注册 → 解析」机制。内置实现是「特例」而非「默认注册项」。

## 设计

### 1. 统一 Registry（`weave/registry.py`）

```python
class Registry:
    def register(self, name, factory, *, replace=False): ...   # 重名报错
    def get(self, name): ...       # 返回 factory，未命中 ValueError（列出 names）
    def create(self, name, *args, **kwargs): ...  # get + call
    def names(self): ...           # 按注册顺序（保证报错文案稳定）
    def __contains__(self, name): ...
```

factory 可以是类（Loop/Backend，直接 `cls(*args)`）或工厂函数（LLM，懒 import + 定制构造）。

### 2. 三个分发改走注册表（内置 = 预注册）

- `LLM_REGISTRY`（anthropic/openai/deepseek）
- `LOOP_REGISTRY`（simple/iterative/scheduled）
- `BACKEND_REGISTRY`（sqlite/file/chroma）

### 3. 公开注册 API + 实例注入

```python
from weave import register_llm, register_loop, register_memory_backend
Weave("weave.yaml", llm=..., loop=...)   # 运行期覆盖（离线/测试逃生口）
```

### 4. 校验收敛 + 能力声明

- 删除 `_validate_raw` 对 `loop.type` / `llm.provider` 的硬编码白名单，未知值交给
  resolver（`Registry.create`）报错——「分发 + 校验 + 报错」收敛到注册表一处。
- 用 backend 的 `SUPPORTED_ACCESS_TYPES` 类属性替代 `_CHROMA_ACCESS_TYPES` 硬编码，
  自定义"只支持 knowledge"的后端无需改 manager。

## 向后兼容

- 内置实现预注册，`loop.type: iterative` / `provider: anthropic` / `backend: sqlite` 零改动。
- Loop 报错文案保持原样（`Unknown loop type 'X'. Supported loop types: simple, iterative,
  scheduled. Please check the 'loop.type' setting in weave.yaml.`），`names()` 按注册顺序保证
  排序稳定。
- `Weave(config_path)` 无 `llm=`/`loop=` 时行为不变。

## 讨论记录

2026-08-31, Asher & Claude.

- 来源：三人设 demo 暴露「可插拔」承诺未兑现（无注入点，宿主只能 hack `_llm`）。
- 决策：注册表为「单一事实来源」，内置实现降级为预注册默认值；实例注入作为运行期逃生口
  （与 `@weave.tool` 的程序化注册同一哲学）。
- 边界：本 issue 只解决「接线」，不解决「适配器实现质量」（见 #011）。
