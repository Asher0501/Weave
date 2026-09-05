"""Weave 配置管理。

从 YAML 文件 + 环境变量加载配置，支持 ${VAR:-default} 模板语法。
零硬编码——所有值来自配置文件或环境变量。
"""
from __future__ import annotations

import json as _json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from weave_agent_sdk.types import (
    AgentConfig,
    CheckpointConfig,
    DEFAULT_PROVIDER,
    FeatureConfig,
    LLMConfig,
    LoggingConfig,
    LoopConfig,
    MemoryConfig,
    MemoryScopeConfig,
    PromptConfig,
    ServerConfig,
    WeaveConfig,
)

logger = logging.getLogger(__name__)

# ── 环境变量插值 ─────────────────────────────────────────

# 支持三种格式：
#   ${VAR}          — 无默认值，环境变量不存在时返回空字符串
#   ${VAR:default}  — 带默认值（POSIX 风格）
#   ${VAR:-default} — 带默认值（Bash 风格，忽略 - 前缀）
# 注意：可选组 (?:...) 末尾必须有 ?，否则 ${VAR}（无默认值）格式完全不匹配。
_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::(-?)([^}]*))?\}")


def _resolve_env(value: str) -> str:
    """解析字符串中的 ${VAR:-default} 占位符。

    支持的格式:
        ${VAR}           — 环境变量不存在时返回空字符串
        ${VAR:default}   — 环境变量不存在时使用 default
        ${VAR:-default}  — 同上，兼容 Bash 风格（- 被忽略）
    """
    def _replace(m: re.Match) -> str:
        var_name = m.group(1)
        has_colon = m.group(2) is not None  # None 表示无 :，"" 表示仅有 :，"-" 表示 :-
        if not has_colon:
            # ${VAR} — 无默认值
            return os.environ.get(var_name, "")
        # ${VAR:default} 或 ${VAR:-default}
        default = m.group(3) or ""
        return os.environ.get(var_name, default)
    return _ENV_VAR_RE.sub(_replace, value)


# ── 配置加载 ─────────────────────────────────────────────


def _access_ttl(access_cfg: Any) -> Any:
    """读取 access 配置块内的 ttl（weave.yaml 如 session.stream.ttl: 3600）。

    仅当 access 配置为 dict 时读取；否则返回 None（未配置）。
    """
    if isinstance(access_cfg, dict):
        return access_cfg.get("ttl")
    return None


def _resolve_relative_path(path: str, base_dir: Path) -> str:
    """把相对路径解析为相对 base_dir 的绝对路径；绝对路径原样返回。

    路径规范化（docs/issues/012）：默认相对路径相对配置文件目录，而非 CWD。
    特殊值 ``:memory:``（SQLite 内存数据库标识）不是文件路径，原样返回。
    绝对路径用字符串前缀判断（`/`、`\\`、盘符 `C:`）而非 Path.is_absolute——
    后者在 Windows 上无法识别 Unix 风格 `/abs`（会被当作盘根相对路径）。
    相对路径 resolve 后用 as_posix() 统一 `/` 分隔符。
    """
    if path == ":memory:":
        return path
    if _is_absolute(path):
        return path
    return (base_dir / path).as_posix()


def _is_absolute(path: str) -> bool:
    """用字符串前缀判断是否为绝对路径（跨平台）。

    Unix: 以 `/` 开头；Windows: 以 `\\` 开头或盘符 `C:`（`C:\\` / `C:/`）。
    """
    if path.startswith(("/", "\\")):
        return True
    return len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\")


def _resolve_access_path(access_cfg: Any, base_dir: Path | None) -> Any:
    """解析 access 配置块内的 path 字段（stream.path / state.path / knowledge.path）。

    返回新的 dict（不修改原 dict），path 字段被解析为相对配置文件目录的
    绝对路径；无 path 字段、非 dict、或 base_dir 为 None（单元测试直调
    _parse_memory_scopes 时）则原样返回。
    """
    if base_dir is None or not isinstance(access_cfg, dict) or "path" not in access_cfg:
        return access_cfg
    resolved = dict(access_cfg)
    resolved["path"] = _resolve_relative_path(str(access_cfg["path"]), base_dir)
    return resolved


def _parse_memory_scopes(raw: dict, config_dir: Path | None = None) -> dict[str, MemoryScopeConfig]:
    """解析 memory.scopes 配置块。

    TTL 按 access 类型分别解析（stream.ttl / state.ttl / knowledge.ttl），
    scope 级 ttl（session.ttl）作为统一回退——允许同一 scope 下不同 access
    类型配置不同过期时间，而非折叠为单一 scope 级值
    （review round-6 issue 1）。此前 ttl 被静默丢弃、后端写入永不设
    expires_at，记忆永不过期、DB 无界增长（review round-4 issue 1）。
    backend 解析 scope 级 backend（session.backend），access 级 backend
    （session.stream.backend）保留在 access dict 中由 MemoryManager 读取
    （review round-6 issue 2）。
    access 的 path 字段经 _resolve_access_path 解析为相对配置文件目录的
    绝对路径（docs/issues/012 路径规范化）。
    """
    scopes: dict[str, MemoryScopeConfig] = {}
    for scope_name, scope_data in raw.items():
        if not isinstance(scope_data, dict):
            continue
        # 用 len(scopes) 替代 scopes.__len__()，语义更清晰
        priority = scope_data.get("priority", len(scopes))
        stream_cfg = _resolve_access_path(scope_data.get("stream"), config_dir)
        state_cfg = _resolve_access_path(scope_data.get("state"), config_dir)
        knowledge_cfg = _resolve_access_path(scope_data.get("knowledge"), config_dir)

        scopes[scope_name] = MemoryScopeConfig(
            priority=priority,
            backend=scope_data.get("backend"),
            ttl=scope_data.get("ttl"),
            ttl_stream=_access_ttl(stream_cfg),
            ttl_state=_access_ttl(state_cfg),
            ttl_knowledge=_access_ttl(knowledge_cfg),
            stream=stream_cfg if isinstance(stream_cfg, dict) else None,
            state=state_cfg if isinstance(state_cfg, dict) else None,
            knowledge=knowledge_cfg if isinstance(knowledge_cfg, dict) else None,
        )
    return scopes


def _validate_raw(raw: dict) -> None:
    """校验 raw YAML 配置，配错时给出「字段路径 期望 X 得到 Y」的清晰报错（D2）。

    只校验当前会产生天书错误（无 try/except 的 int()/float() 转换）的字段，
    以及枚举字段（llm.provider / loop.type）。已有宽松兜底的字段
    （如 stream_timeout / memory_knowledge_topk）不在校验范围，避免改变既有行为。
    """
    from weave_agent_sdk.config_error import ConfigError

    # 枚举字段：字段 → 允许值。
    # 注意：loop.type / llm.provider 不再在此白名单校验——改为由注册表 resolver
    # （Registry.create）报错，使「分发 + 校验 + 报错」收敛到单一事实来源
    # （docs/issues/010），从而允许宿主经 register_loop / register_llm 扩展。
    enum_fields = {}
    # 数值字段：int / float
    int_fields = {
        "llm": ["max_tokens"],
        "loop": ["max_iterations", "memory_context_limit", "messages_window", "llm_retry_attempts"],
        "server": ["port"],
    }
    float_fields = {
        "llm": ["temperature"],
        "loop": ["timeout", "tool_timeout", "llm_timeout", "event_min_interval", "llm_retry_backoff", "llm_call_timeout"],
    }

    for (section, field), allowed in enum_fields.items():
        sec = raw.get(section)
        if isinstance(sec, dict) and field in sec and sec[field] is not None:
            if sec[field] not in allowed:
                raise ConfigError(
                    f"{section}.{field} 期望 {'/'.join(allowed)} 之一，得到 {sec[field]!r}"
                )

    for section, fields in int_fields.items():
        sec = raw.get(section)
        if not isinstance(sec, dict):
            continue
        for field in fields:
            if field in sec:
                try:
                    int(sec[field])
                except (TypeError, ValueError):
                    raise ConfigError(f"{section}.{field} 期望整数，得到 {sec[field]!r}")

    for section, fields in float_fields.items():
        sec = raw.get(section)
        if not isinstance(sec, dict):
            continue
        for field in fields:
            if field in sec:
                try:
                    float(sec[field])
                except (TypeError, ValueError):
                    raise ConfigError(f"{section}.{field} 期望数值，得到 {sec[field]!r}")


def load_config(config_path: str | Path) -> WeaveConfig:
    """加载并解析 weave.yaml 配置文件。

    解析优先级:
    1. 环境变量（os.environ）
    2. weave.yaml 文件
    3. 代码默认值

    Args:
        config_path: weave.yaml 文件路径

    Returns:
        解析后的 WeaveConfig 对象
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # 路径规范化的基准：配置文件所在目录。所有默认相对路径（memory 的
    # default_path / scope 的 path）都相对此目录解析，而非 CWD——这样
    # `pip install weave` 后从任意目录运行，数据落点都可预期
    # （docs/issues/012 路径规范化）。
    config_path = config_path.resolve()
    config_dir = config_path.parent

    # 1. 读取并解析 YAML
    raw_text = config_path.read_text(encoding="utf-8")
    # 对整份 YAML 原文做唯一一次环境变量插值。此处已覆盖全部 `${...}`
    # 占位符（含 llm 段在内），因此后续不再对任何子段二次插值——
    # 否则 env 值本身含 `${...}` 形态子串时会被二次替换产生非预期值
    # （单值应只解析一次、结果确定，review round-2 issue 3）。
    raw_text = _resolve_env(raw_text)
    raw = yaml.safe_load(raw_text) or {}

    # 2.5 校验配置：配错时给出清晰报错（D2）
    _validate_raw(raw)

    # 3. 逐层解析为 dataclass
    agent_raw = raw.get("agent", {}) or {}
    # llm 段已由上方整份原文插值解析过一次，直接取用即可
    llm_raw = raw.get("llm", {}) or {}

    # LLM api_key 只从 yaml 读取；环境变量解析交由 create_llm 的 _resolve_api_key
    # （provider 感知）处理。此处跨 provider 读 env 会把 anthropic 的 key 错配给
    # deepseek/openai（docs/issues/011）。
    llm_api_key = llm_raw.get("api_key")

    # 模型名从 weave.yaml 或环境变量读取，代码中不硬编码（R3）
    # weave.yaml 中应配置 model: ${WEAVE_MODEL:-claude-sonnet-5-20251001}
    # 若仍未配置，降级为 WEAVE_MODEL 环境变量
    model_name = llm_raw.get("model", "")
    if not model_name:
        model_name = os.environ.get("WEAVE_MODEL", "")

    loop_raw = raw.get("loop", {}) or {}
    memory_raw = raw.get("memory", {}) or {}
    prompts_raw = raw.get("prompts", {}) or {}
    features_raw = raw.get("features", {}) or {}
    server_raw = raw.get("server", {}) or {}
    logging_raw = raw.get("logging", {}) or {}
    checkpoint_raw = raw.get("checkpoint", {}) or {}

    # stream_timeout 为可选"空闲超时"（秒）：未配置（None）表示不启用；
    # 非数值 / 非法配置降级为 None，避免 asyncio.wait_for 收到非法 timeout。
    # 语义为"自上次事件的时间"：每次等待事件都以 stream_timeout 为上限、
    # 事件到达即重置，不是整次运行总时长上限（与 types.py / weave.yaml 对齐，
    # review round-1 issue 3）。
    _stream_timeout = loop_raw.get("stream_timeout")
    if _stream_timeout is None:
        stream_timeout = None
    else:
        try:
            stream_timeout = float(_stream_timeout)
        except (TypeError, ValueError):
            stream_timeout = None

    # memory_knowledge_topk 为 before_think 知识检索注入条数上限（top_k），
    # 默认 5。非数值 / 非正配置回退默认 5（与 base.before_think 的守卫一致，
    # 避免非法值传入 knowledge.search 的 top_k，review round-8 issue 3）。
    try:
        memory_knowledge_topk = int(loop_raw.get("memory_knowledge_topk", 5))
    except (TypeError, ValueError):
        memory_knowledge_topk = 5
    if memory_knowledge_topk <= 0:
        memory_knowledge_topk = 5

    return WeaveConfig(
        agent=AgentConfig(name=agent_raw.get("name", "default")),
        llm=LLMConfig(
            provider=llm_raw.get("provider", DEFAULT_PROVIDER),
            model=model_name,
            max_tokens=int(llm_raw.get("max_tokens", 4096)),
            temperature=float(llm_raw.get("temperature", 0.7)),
            api_key=llm_api_key,
            base_url=llm_raw.get("base_url"),
        ),
        loop=LoopConfig(
            type=loop_raw.get("type", "iterative"),
            max_iterations=int(loop_raw.get("max_iterations", 10)),
            stop_conditions=loop_raw.get("stop_conditions", []),
            schedule=loop_raw.get("schedule"),
            timeout=float(loop_raw.get("timeout", 5.0)),
            stream_timeout=stream_timeout,
            tool_timeout=float(loop_raw.get("tool_timeout", 30.0)),
            llm_timeout=float(loop_raw.get("llm_timeout", 120.0)),
            memory_context_limit=int(loop_raw.get("memory_context_limit", 20)),
            memory_knowledge_topk=memory_knowledge_topk,
            # messages_window 为 iterative LLM 消息窗口条数上限；小于 3 时裁剪
            # 逻辑退化（窗口-2 为 0/负，切片失效使 messages 无界增长），故钳制
            # 下限为 3（review round-3 issue 2）。
            messages_window=max(3, int(loop_raw.get("messages_window", 20))),
            event_min_interval=float(loop_raw.get("event_min_interval", 1.0)),
            llm_retry_attempts=int(loop_raw.get("llm_retry_attempts", 3)),
            llm_retry_backoff=float(loop_raw.get("llm_retry_backoff", 2.0)),
            llm_call_timeout=float(loop_raw.get("llm_call_timeout", 120.0)),
        ),
        memory=MemoryConfig(
            scopes=_parse_memory_scopes(memory_raw.get("scopes", {}), config_dir),
            default_backend=str(memory_raw.get("default_backend", "sqlite")),
            # 默认数据路径解析为相对配置文件目录（docs/issues/012 路径规范化）
            default_path=_resolve_relative_path(
                str(memory_raw.get("default_path", "./.weave/memory.db")), config_dir
            ),
        ),
        prompts=PromptConfig(
            system=prompts_raw.get("system", ""),
            loop_instruction=prompts_raw.get("loop_instruction", ""),
            memory_use=prompts_raw.get("memory_use", ""),
        ),
        features=FeatureConfig(
            schema_validation=bool(features_raw.get("schema_validation", False)),
            structured_call=bool(features_raw.get("structured_call", False)),
            two_stage_pipeline=bool(features_raw.get("two_stage_pipeline", False)),
            prompt_defense=bool(features_raw.get("prompt_defense", False)),
        ),
        server=ServerConfig(
            host=str(server_raw.get("host", "127.0.0.1")),
            port=int(server_raw.get("port", 48080)),
            cors_origins=list(server_raw.get("cors_origins", [])),
        ),
        logging=LoggingConfig(
            level=str(logging_raw.get("level", "INFO")),
            file=logging_raw.get("file"),
        ),
        checkpoint=CheckpointConfig(
            enabled=bool(checkpoint_raw.get("enabled", False)),
            trigger=str(checkpoint_raw.get("trigger", "after_each_tool")),
            keep=int(checkpoint_raw.get("keep", 10)),
        ),
    )
