"""weave.core.interfaces — 接口。

weave 只有一个功能：**LLM 交互**（`weave.llm(...)`，一个输入、一个输出）。
所以这里只有两个接口，都说得清谁在用：

| 接口 | 谁在用 |
|---|---|
| `LLMProvider` | LLM 交互对象（唯一契约）；`OpenAIHTTPProvider` / `FakeProvider` 实现它 |
| `StateStore` | KV 存储（`SQLiteStateStore` / `InMemoryStateStore`），业务方直接使用 |

契约纪律：

1. **原子是哑的**：不重试 / 超时 / 错误分类 / 解码——那些属于 `weave.llm` 对象；
2. **原子不知道策略**：不解释命名约定、不产生副作用；
3. **失败在原子内以类型化异常表达**，对象层统一转成 `TypedFailure`；
4. 接口之间只递 `weave.core.envelopes` 里的信封。

已归档的接口（`Provider`(legacy) / `EventSink` / `Database` / `ConversationLog` /
`Search` / `Executor` / `EnvironmentCatalog` / `ToolRegistry` 与三个可选存储协议）
连同实现一起放在 `archive/v0.4-parked/`。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from weave.core.envelopes import CallRequest
from weave.core.types import LLMResponse, StreamChunk

# ══════════════════════════════════════════════════════════
# LLM 交互（当前唯一的模型接口）
# ══════════════════════════════════════════════════════════


class LLMProvider(ABC):
    """一次模型动作（哑原子）。

    契约：
    - **单次**：失败即抛类型化异常（`RateLimitError` / `AuthError` / …），
      **不自行重试、不计时、不分类**；
    - 只做协议/格式适配：厂商请求体与响应在这里翻译，工具 schema 以厂商中立的
      `ToolSchema` 传入（`CallRequest.schemas`）；
    - **不得解析工具调用语法**：`LLMResponse.tool_calls` 保持厂商原样，
      解码是对象的职责（`weave.llm.decode`）；
    - 推理内容（`reasoning`）绝不隐式回传后续请求。

    适配器**可选自述**（对象层据此在发请求前做"错家形状"预检；**不是接口的一部分**，
    不声明就跳过 —— 注入自己的哑原子时无需实现）：`protocol`（厂商名）·
    `foreign_block_types`（明确属于别家的块类型，遇到即拒，未知块一律放行）·
    `block_shape_hint`（本厂商正确形状示例，仅用于报错信息）。
    """

    @abstractmethod
    async def complete(self, request: CallRequest) -> LLMResponse:
        """单次非流式调用。"""

    def stream(self, request: CallRequest) -> AsyncIterator[StreamChunk]:
        """流式调用，返回异步迭代器（实现通常写成 async generator）。

        这是**可选能力**：未实现时抛 `NotImplementedError`，对象应回退到 `complete()`。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 未实现流式调用；请回退到 complete()"
        )


# ══════════════════════════════════════════════════════════
# 存储（KV）—— 被实现与业务方直接使用
# ══════════════════════════════════════════════════════════


class StateStore(ABC):
    """通用键值存储（哑原子）。

    契约：
    - `namespace` / `key` 视为**不透明字符串**：weave 不解析、不定义任何命名约定；
    - value 必须 JSON 可序列化；`get` 读不到返回 `None`；
    - `append` 把元素追加到 key 处的**有序列表**（列表不存在则创建）；
    - 不解释内容、不做检索、不做裁剪策略、不做 TTL 判断。
    """

    @abstractmethod
    async def get(self, namespace: str, key: str) -> Any | None: ...

    @abstractmethod
    async def set(self, namespace: str, key: str, value: Any) -> None: ...

    @abstractmethod
    async def delete(self, namespace: str, key: str) -> None: ...

    @abstractmethod
    async def append(self, namespace: str, key: str, value: Any) -> None:
        """把 value 追加到 key 的有序列表（列表不存在则创建）。"""
