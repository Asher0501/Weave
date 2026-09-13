"""HTTP 传输层（可注入 —— 清单 L1：不联网即可测协议适配）。

为什么单独一层：原子 provider 要"哑"，但它总得发 HTTP。把传输抽成可注入的协议后：
- 测试注入假 transport（脚本化响应、任意分片），协议适配就能离线验收；
- 生产用 stdlib 实现（`UrllibTransport`），**零第三方依赖**；
- 想换 httpx/aiohttp 也只换这一层，provider 与对象都不用动。

注意：本层不做重试、不做错误分类——那是 LLM 交互对象的职责；
这里只把"一次 HTTP 请求"做对（含把 Retry-After 原样带给上层，
以及**跨分片的 UTF-8 增量解码**——按块 decode 会把多字节字符切坏）。
"""
from __future__ import annotations

import asyncio
import codecs
import json
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "HTTPResponse",
    "Transport",
    "UrllibTransport",
    "StreamHTTPError",
    "retry_after_from_headers",
]


@dataclass(slots=True)
class HTTPResponse:
    """一次 HTTP 响应的最小形状。"""

    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body or "{}")


def retry_after_from_headers(headers: dict[str, str]) -> float | None:
    """`Retry-After` 头 → 秒（支持秒数与 HTTP 日期两种写法；解析不了返回 None）。"""
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    text = raw.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt

        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        delta = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:            # noqa: BLE001 - 头格式五花八门，解析失败就当没有
        return None


@runtime_checkable
class Transport(Protocol):
    """传输协议：一次 POST，或一次 POST 的流式分片。"""

    async def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        timeout: float | None = None,
    ) -> HTTPResponse: ...

    def post_sse(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """产出**原始文本分片**（不是行）——半行缓冲由 SSE 层负责。"""
        ...


class UrllibTransport:
    """stdlib 实现：阻塞 IO 放线程，零第三方依赖。"""

    def __init__(self, *, read_size: int = 4096) -> None:
        self._read_size = read_size

    # ── 非流式 ───────────────────────────────────────

    async def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        timeout: float | None = None,
    ) -> HTTPResponse:
        return await asyncio.to_thread(self._post, url, headers, payload, timeout)

    def _post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any],
        timeout: float | None,
    ) -> HTTPResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HTTPResponse(
                    status=int(getattr(response, "status", 200) or 200),
                    body=response.read().decode("utf-8", "replace"),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:      # 4xx/5xx 也是"响应"，交给上层分类
            raw = exc.read().decode("utf-8", "replace")
            return HTTPResponse(
                status=int(exc.code),
                body=raw,
                headers=dict(exc.headers.items()) if exc.headers else {},
            )

    # ── 流式 ─────────────────────────────────────────

    def post_sse(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        return self._sse(url, headers, payload, timeout)

    async def _sse(
        self, url: str, headers: dict[str, str], payload: dict[str, Any],
        timeout: float | None,
    ) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def worker() -> None:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                request = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if not (200 <= int(getattr(response, "status", 200) or 200) < 300):
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            StreamHTTPError(
                                int(getattr(response, "status", 500)),
                                decoder.decode(response.read(), final=True),
                                dict(response.headers.items()),
                            ),
                        )
                        return
                    while True:
                        chunk = response.read(self._read_size)
                        if not chunk:
                            break
                        text = decoder.decode(chunk)      # 跨分片的多字节字符不会被切坏
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, text)
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        loop.call_soon_threadsafe(queue.put_nowait, tail)
            except urllib.error.HTTPError as exc:
                raw = decoder.decode(exc.read(), final=True)
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    StreamHTTPError(int(exc.code), raw,
                                    dict(exc.headers.items()) if exc.headers else {}),
                )
            except Exception as exc:            # noqa: BLE001 - 交给上层分类
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = await queue.get()
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                raise item
            yield item


class StreamHTTPError(Exception):
    """流式请求返回非 2xx 时内部传递（provider 转成类型化错误 + Retry-After）。"""

    def __init__(self, status: int, body: str, headers: dict[str, str]) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.headers = headers
        self.retry_after = retry_after_from_headers(headers)
