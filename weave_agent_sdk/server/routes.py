"""REST API 路由 — /agents/{name}/*"""
from __future__ import annotations

import json
from typing import Any

from weave_agent_sdk.server.ws import ws_agent_stream


def register_routes(app: Any, weave: Any) -> None:
    """注册所有 API 路由到 FastAPI app。"""
    from fastapi import HTTPException, WebSocket
    from fastapi.responses import StreamingResponse
    import asyncio

    agent_name = weave._config.agent.name

    @app.post(f"/agents/{agent_name}/run")
    async def agent_run(request: dict[str, Any]):
        """执行一次 Agent 运行。"""
        input_text = request.get("input", "")
        scope_hints = request.get("scope_hints")
        context = request.get("context")
        tool_filter = request.get("tool_filter")
        result = await weave.arun(input_text, scope_hints=scope_hints, context=context, tool_filter=tool_filter)
        return {
            "output": result.output,
            "elapsed_ms": result.elapsed_ms,
            "iterations": result.iterations,
            "memory_updated": result.memory_updated,
        }

    @app.get(f"/agents/{agent_name}/memory")
    async def agent_memory_list():
        """列出 Memory 状态。"""
        return {"stats": weave._memory.stats(), "scopes": weave._memory.active_scope_names}

    @app.delete(f"/agents/{agent_name}/memory")
    async def agent_memory_clear():
        """清空 Memory。"""
        weave._memory.close()
        return {"status": "cleared"}

    @app.get(f"/agents/{agent_name}/status")
    async def agent_status():
        """获取 Agent 状态。"""
        return weave.status()

    @app.websocket(f"/ws/agents/{agent_name}/stream")
    async def agent_stream_ws(websocket: WebSocket):
        """WebSocket 流式输出。"""
        await ws_agent_stream(websocket, weave)
