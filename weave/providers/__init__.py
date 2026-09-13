"""weave.providers — LLMProvider 的参考实现（可替换 · 不进兼容承诺）。

两种**协议**（各自零第三方依赖，HTTP 走可注入 `Transport`）：

- **`OpenAIHTTPProvider`**：OpenAI 兼容 `/chat/completions`（OpenAI / DeepSeek / 通义 /
  Ollama / vLLM / 各类网关）——`weave.llm(model=…)` 的默认协议。
- **`AnthropicHTTPProvider`**：Anthropic 原生 `/v1/messages`（system 是顶层参数、
  tool 结果是内容块、流式事件不同）——`weave.llm(model=…, protocol="anthropic")`。
- `FakeProvider`：离线确定性实现（测试用）。

两者都是**哑原子**：一次调用、失败即抛、不重试、不计时、不解码；
可靠性由 `weave.llm` 对象负责。

SSE 与传输层：`weave.providers.sse`（半行缓冲/事件组装）· `weave.providers.transport`。

旧的 `OpenAICompatibleProvider`（openai SDK + 内嵌重试）与 `LegacyProviderAdapter`
已归档到 `archive/v0.4-parked/weave/providers/`。
"""
from weave.providers.anthropic_http import AnthropicHTTPProvider
from weave.providers.fake import FakeProvider, FakeTurn
from weave.providers.openai_http import OpenAIHTTPProvider
from weave.providers.transport import HTTPResponse, Transport, UrllibTransport

__all__ = [
    "OpenAIHTTPProvider",
    "AnthropicHTTPProvider",
    "FakeProvider",
    "FakeTurn",
    "Transport",
    "UrllibTransport",
    "HTTPResponse",
]
