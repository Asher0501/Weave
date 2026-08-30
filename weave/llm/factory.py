"""LLM 适配器自动检测与创建。

按优先级确定使用哪个后端:
1. weave.yaml 中显式指定的 provider
2. 环境变量 LLM_PROVIDER
3. 智能推断: ANTHROPIC_API_KEY → anthropic, OPENAI_API_KEY → openai
4. 从 ~/.claude/settings.json 读取（注入 os.environ 后自动感知）

模型名从 weave.yaml 或环境变量读取，代码中不硬编码（R3）。
"""
from __future__ import annotations

import os
from typing import Any

from weave.llm.base import BaseLLM
from weave.registry import Registry
from weave.utils.env import load_claude_env


# ── LLM 注册表 ──────────────────────────────────────────
# 内置 provider = 预注册默认值；宿主可 register_llm 扩展（见 docs/issues/010）。

def _make_anthropic(api_key: str | None, model: str, base_url: str | None, auth_token: str | None = None) -> BaseLLM:
    from weave.llm.anthropic import AnthropicAdapter
    return AnthropicAdapter(api_key=api_key, model=model, base_url=base_url, auth_token=auth_token)


def _make_openai(api_key: str | None, model: str, base_url: str | None, auth_token: str | None = None) -> BaseLLM:
    from weave.llm.openai import OpenAIAdapter
    return OpenAIAdapter(api_key=api_key, model=model, base_url=base_url)


def _make_deepseek(api_key: str | None, model: str, base_url: str | None, auth_token: str | None = None) -> BaseLLM:
    from weave.llm.openai import OpenAIAdapter
    return OpenAIAdapter(
        api_key=api_key,
        model=model,
        base_url=base_url or "https://api.deepseek.com/v1",
    )


LLM_REGISTRY = Registry("llm provider")
LLM_REGISTRY.register("anthropic", _make_anthropic)
LLM_REGISTRY.register("openai", _make_openai)
LLM_REGISTRY.register("deepseek", _make_deepseek)


def create_llm(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> BaseLLM:
    """创建 LLM 适配器实例。

    Args:
        provider: 显式指定后端 ("anthropic" | "openai")，None 则自动检测
        model: 模型名，None 则从环境变量或自动推断
        api_key: API Key，None 则从环境变量读取
        base_url: 自定义 API 地址

    Returns:
        BaseLLM 实例

    Raises:
        ValueError: 无法确定 LLM 后端、缺少 API Key 或缺少模型名
    """
    # ── 0. 注入 Claude Code settings.json env ──
    load_claude_env()

    # ── 1. 确定 provider ──
    if provider is None:
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower()

    if not provider:
        provider = _auto_detect_provider()

    # ── 2. 确定 API Key / auth token ──
    # ANTHROPIC_AUTH_TOKEN 是 Bearer token，应传 SDK 的 auth_token（Authorization: Bearer），
    # 而非 api_key（x-api-key）。仅当 key 来自环境且未显式配 ANTHROPIC_API_KEY 时按
    # auth_token 传递（docs/issues/011）。
    auth_token: str | None = None
    if api_key is None:
        api_key = _resolve_api_key(provider)
        if provider == "anthropic" and api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            auth_token = api_key
            api_key = None

    if not api_key and not auth_token:
        raise ValueError(
            f"No API key found for provider '{provider}'. "
            f"Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY "
            f"environment variable."
        )

    # ── 3. 确定 model ──
    # 模型名优先级：显式传入 > WEAVE_MODEL 环境变量 > provider 默认 env var
    if model is None:
        model = _resolve_model(provider)

    if not model:
        raise ValueError(
            f"No model specified for provider '{provider}'. "
            f"Set model in weave.yaml or WEAVE_MODEL environment variable."
        )

    # ── 4. 创建实例（经注册表，内置 provider = 预注册默认值）──
    return LLM_REGISTRY.create(
        provider, api_key=api_key, model=model, base_url=base_url, auth_token=auth_token
    )


def _auto_detect_provider() -> str:
    """根据环境变量自动推断 LLM 后端（settings.json 已注入 os.environ）。"""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"  # 默认


def _resolve_api_key(provider: str) -> str | None:
    """按优先级解析 API Key（settings.json 已注入 os.environ）。"""
    if provider == "anthropic":
        return (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
    elif provider == "deepseek":
        return (
            os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
    elif provider == "openai":
        return os.environ.get("OPENAI_API_KEY")
    return None


def _resolve_model(provider: str) -> str:
    """从环境变量获取模型名，代码中不硬编码（R3）。

    优先级：
    1. WEAVE_MODEL 环境变量（通用）
    2. {PROVIDER}_MODEL 环境变量（如 ANTHROPIC_MODEL、OPENAI_MODEL）
    """
    # 通用环境变量优先
    model = os.environ.get("WEAVE_MODEL", "")
    if model:
        return model

    # provider 专用环境变量
    env_var_map = {
        "anthropic": "ANTHROPIC_MODEL",
        "openai": "OPENAI_MODEL",
        "deepseek": "DEEPSEEK_MODEL",
    }
    env_var = env_var_map.get(provider)
    if env_var:
        model = os.environ.get(env_var, "")
        if model:
            return model

    # 无任何配置时返回空字符串，由调用方处理
    return ""
