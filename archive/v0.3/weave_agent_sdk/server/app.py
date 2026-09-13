"""FastAPI Server — 将 Weave Agent 暴露为 REST API。

用法:
    weave serve --config weave.yaml --port 48080
"""
from __future__ import annotations

import importlib
from typing import Any


def create_app(weave_instance: Any, title: str = "Weave Agent") -> Any:
    """创建 FastAPI app，将 Weave 实例路由化。

    Args:
        weave_instance: Weave 实例
        title: API 标题

    Returns:
        FastAPI app 实例
    """
    try:
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:
        raise ImportError("fastapi package required. Install with: pip install fastapi uvicorn")

    app = FastAPI(title=title, version="0.1.0")

    # CORS
    origins = weave_instance._config.server.cors_origins or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    from weave_agent_sdk.server.routes import register_routes
    register_routes(app, weave_instance)

    return app
