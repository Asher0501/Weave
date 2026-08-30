"""MemoryManager — 管理作用域、namespace、后端分配。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from weave.memory.backends.sqlite import SQLiteBackend
from weave.memory.backends.file import FileBackend
from weave.memory.backends.chroma import ChromaBackend
from weave.memory.stream import SQLiteStreamMemory
from weave.memory.state import SQLiteStateMemory
from weave.memory.knowledge import SQLiteKnowledgeMemory
from weave.types import MemoryConfig, MemoryScopeConfig, SearchResult
from weave.registry import Registry

logger = logging.getLogger(__name__)

# 合法的 access_type 值
_VALID_ACCESS_TYPES = frozenset({"stream", "state", "knowledge"})

# ── Backend 注册表 ─────────────────────────────────────
# 内置 backend = 预注册默认值；宿主可 register_memory_backend 扩展（见 docs/issues/010）。
# 能力声明：backend 类可用 SUPPORTED_ACCESS_TYPES 声明支持的 access_type，
# 默认支持全部（stream/state/knowledge）。

BACKEND_REGISTRY = Registry("memory backend")
BACKEND_REGISTRY.register("sqlite", SQLiteBackend)
BACKEND_REGISTRY.register("file", FileBackend)
BACKEND_REGISTRY.register("chroma", ChromaBackend)


class MemoryManager:
    """统一管理 Weave 的所有 Memory 后端和 namespace。

    职责:
    1. 根据配置创建后端实例（SQLite / File / Chroma）
    2. 管理 scope → namespace 映射
    3. 提供 stream / state / knowledge 的统一访问入口
    4. TTL 清理
    """

    def __init__(self, config: MemoryConfig):
        self._config = config
        self._backends: dict[str, Any] = {}  # key = f"{backend_type}:{db_path}"
        self._active_scopes: list[tuple[str, int, str]] = []  # (scope_name, priority, scope_id)

    def activate_scopes(self, scope_hints: dict[str, str] | None = None) -> None:
        """激活所有配置的 scope，解析 scope_id。

        未提供 {scope_name}_id hint 的 scope 使用稳定的默认 scope_id
        （默认 scope 用 "default"，配置的 scope 用 scope_name 本身），
        保证跨运行 Memory 持久，而非每次生成随机 uuid。

        Args:
            scope_hints: {scope_name}_id → value 映射。
        """
        scope_hints = scope_hints or {}
        scopes = self._config.scopes

        if not scopes:
            # 默认 scope：使用稳定 scope_id，保证跨运行持久
            self._active_scopes = [("default", 0, scope_hints.get("default_id", "default"))]
            return

        active = []
        for scope_name, scope_cfg in scopes.items():
            hint_key = f"{scope_name}_id"
            # 未提供 hint 时回退到 scope_name 作为稳定的默认 scope_id
            scope_id = scope_hints.get(hint_key, scope_name)
            priority = scope_cfg.priority
            active.append((scope_name, priority, scope_id))

        # 按 priority 升序（小=窄，优先覆盖）
        active.sort(key=lambda x: x[1])
        self._active_scopes = active

    @property
    def stream(self) -> "SQLiteStreamMemory":
        return SQLiteStreamMemory(self)

    @property
    def state(self) -> "SQLiteStateMemory":
        return SQLiteStateMemory(self)

    @property
    def knowledge(self) -> "SQLiteKnowledgeMemory":
        return SQLiteKnowledgeMemory(self)

    def _get_backend_for_namespace(self, namespace: str) -> Any:
        """根据 namespace 解析对应的后端实例。

        后端类型从配置读取：access 级 backend（session.stream.backend）→
        scope 级 backend → MemoryConfig.default_backend。此前
        backend/default_backend 配置被静默忽略、恒创建 SQLiteBackend
        （review round-6 issue 2）。sqlite / file 支持全部 access 类型；
        chroma 仅支持 knowledge（向量搜索），配置到 stream/state 时告警并
        回退 sqlite，而非静默使用错误语义。
        实例按 (backend_type, db_path) 缓存——同配置共享实例，不同配置独立。

        Namespace 格式: {scope_name}:{scope_id}:{access_type}
        access_type 必须是 stream / state / knowledge 之一。
        """
        # 从 namespace 解析 scope_name 和 access_type
        parts = namespace.rsplit(":", 1)
        scope_name = parts[0].rsplit(":", 1)[0] if len(parts) == 2 else "default"
        access_type = parts[1] if len(parts) == 2 else "stream"

        # 验证 access_type 合法性
        if access_type not in _VALID_ACCESS_TYPES:
            raise ValueError(
                f"Invalid access_type '{access_type}' in namespace '{namespace}'. "
                f"Must be one of: {', '.join(sorted(_VALID_ACCESS_TYPES))}"
            )

        # 解析 backend 类型与路径（access 级 backend 优先，其次 scope 级，
        # 最后 default_backend）
        scope_cfg = self._config.scopes.get(scope_name)
        backend_type = None
        db_path = None
        if scope_cfg:
            access_cfg: dict[str, Any] | None = getattr(scope_cfg, access_type, None)
            if isinstance(access_cfg, dict):
                backend_type = access_cfg.get("backend") or scope_cfg.backend
                path = access_cfg.get("path")
                if isinstance(path, str):
                    db_path = path
            else:
                backend_type = scope_cfg.backend
        if not backend_type:
            backend_type = self._config.default_backend
        if not db_path:
            db_path = self._config.default_path

        backend_type = str(backend_type or "sqlite").strip().lower()

        # 未知 backend：告警并回退 sqlite（保留既有语义）
        if backend_type not in BACKEND_REGISTRY:
            logger.warning(
                "Unknown backend '%s' for namespace %s; falling back to sqlite.",
                backend_type, namespace,
            )
            backend_type = "sqlite"

        # 能力声明：backend 不支持该 access_type 时告警并回退 sqlite
        backend_cls = BACKEND_REGISTRY.get(backend_type)
        supported = getattr(backend_cls, "SUPPORTED_ACCESS_TYPES", _VALID_ACCESS_TYPES)
        if access_type not in supported:
            logger.warning(
                "Backend '%s' does not support access_type '%s' (namespace %s); "
                "falling back to sqlite. Configure 'backend: sqlite' for this scope's %s.",
                backend_type, access_type, namespace, access_type,
            )
            backend_type = "sqlite"

        # 按 (backend_type, db_path) 缓存后端实例
        key = f"{backend_type}:{db_path}"
        if key not in self._backends:
            # FileBackend 把 path 当作目录（在其内写 JSON 文件），而 weave.yaml 默认
            # path 是 sqlite 文件式路径（.../memory.db）。宿主仅把 backend 改为 file 而
            # 沿用默认 path 时，会静默创建名为 memory.db 的目录——同一 path 字段对两种
            # 后端语义不同且无告警（review round-7 issue 3）。此处配置时即告警。
            if backend_type == "file" and Path(db_path or "").suffix:
                logger.warning(
                    "Backend 'file' treats 'path' as a directory, but the "
                    "configured path '%s' looks like a file path (weave.yaml "
                    "default is sqlite file style e.g. '.../memory.db'). "
                    "FileBackend will create a directory named '%s' and write "
                    "JSON files inside it. Configure a directory path for the "
                    "file backend, or keep 'backend: sqlite'.",
                    db_path, Path(db_path).name,
                )
            self._backends[key] = BACKEND_REGISTRY.create(backend_type, db_path)
        return self._backends[key]

    def _ttl_for_namespace(self, namespace: str) -> float | None:
        """返回指定 namespace 配置的 TTL（秒）；未配置返回 None。

        按 access_type 分别解析：优先 access 类型专属 ttl（stream.ttl /
        state.ttl / knowledge.ttl），未单独配置时回退 scope 级 ttl。此前
        ttl 被折叠为单一 scope 级值，同一 scope 下 stream/state/knowledge
        无法各自配置过期时间（review round-6 issue 1）。后端写入时据此
        计算 expires_at=created_at+ttl，实现"每条记忆可配过期时间"
        （basic.md §4.2 / weave.yaml ttl 示例）。此前 ttl 配置被静默丢弃、
        写入永不设 expires_at、清理函数无调用方，记忆永不过期、DB 无界增长
        （review round-4 issue 1）。非数值 / 非正配置回退 None（永不过期），
        避免非法值污染 SQL。
        """
        parts = namespace.rsplit(":", 1)
        scope_name = parts[0].rsplit(":", 1)[0] if len(parts) == 2 else "default"
        access_type = parts[1] if len(parts) == 2 else "stream"
        if access_type not in _VALID_ACCESS_TYPES:
            return None
        scope_cfg = self._config.scopes.get(scope_name)
        if scope_cfg is None:
            return None
        ttl = getattr(scope_cfg, f"ttl_{access_type}", None)
        if ttl is None:
            ttl = getattr(scope_cfg, "ttl", None)
        if ttl is None:
            return None
        try:
            ttl = float(ttl)
        except (TypeError, ValueError):
            return None
        return ttl if ttl > 0 else None

    def get_namespaces(self, access_type: str) -> list[str]:
        """获取所有激活 scope 的指定 access_type 的 namespace 列表。

        Args:
            access_type: "stream" | "state" | "knowledge"

        Returns:
            按 priority 升序排列的 namespace 列表

        Raises:
            ValueError: 当 access_type 不是合法值之一时
        """
        if access_type not in _VALID_ACCESS_TYPES:
            raise ValueError(
                f"Invalid access_type '{access_type}'. "
                f"Must be one of: {', '.join(sorted(_VALID_ACCESS_TYPES))}"
            )
        return [
            f"{scope_name}:{scope_id}:{access_type}"
            for scope_name, _priority, scope_id in self._active_scopes
        ]

    def get_namespace(self, scope_name: str, access_type: str) -> str:
        """获取指定 scope 和 access_type 的 namespace。

        Args:
            scope_name: scope 名称
            access_type: "stream" | "state" | "knowledge"

        Returns:
            namespace 字符串

        Raises:
            ValueError: 当 scope 未激活或 access_type 不合法时
        """
        if access_type not in _VALID_ACCESS_TYPES:
            raise ValueError(
                f"Invalid access_type '{access_type}'. "
                f"Must be one of: {', '.join(sorted(_VALID_ACCESS_TYPES))}"
            )
        for sn, _p, sid in self._active_scopes:
            if sn == scope_name:
                return f"{sn}:{sid}:{access_type}"
        raise ValueError(f"Scope '{scope_name}' not active")

    def _ensure_configured_backends(self) -> None:
        """实例化所有激活 scope 对应的后端，使全后端遍历路径可用。

        MemoryManager._backends 是懒加载的（仅由 _get_backend_for_namespace
        在读写时填充）。新进程（尚未发生任何写/读）下 cleanup() / stats() /
        stream.last(None) / knowledge.search(None) 遍历空 _backends 会静默
        返回 0/空——尽管 DB 文件里已有历史数据（review round-8 issue 1）。
        本方法按激活 scope 的三种 access 类型解析并实例化后端（打开对应
        DB 文件），使既有数据可见。个别后端实例化失败不影响其他后端。
        """
        for access_type in _VALID_ACCESS_TYPES:
            for ns in self.get_namespaces(access_type):
                try:
                    self._get_backend_for_namespace(ns)
                except Exception as e:
                    logger.warning(
                        "Failed to instantiate backend for namespace %s: %s", ns, e,
                    )

    def cleanup(self) -> int:
        """清理所有后端的过期数据。

        清理前先实例化激活 scope 的后端——新进程 _backends 为空时，若直接
        遍历会静默跳过既有 DB 中的过期数据（review round-8 issue 1）。
        """
        self._ensure_configured_backends()
        total = 0
        for backend in self._backends.values():
            total += backend.cleanup_expired()
        return total

    def close(self) -> None:
        for backend in self._backends.values():
            backend.close()
        self._backends.clear()

    def stats(self) -> dict[str, int]:
        """返回各 namespace 的统计信息。

        统计前先做一次过期清理：读多写少的 Agent（scheduled 监控、纯查询）
        写路径清理触发频率低，过期条目会持续堆积且统计虚高。在统计/管理面
        触发全量清理，保证 TTL"记忆可过期"语义在管理面一致
        （review round-6 issue 5）。清理与统计均由各后端按过期条件处理。
        cleanup() 内部会先实例化激活 scope 的后端，新进程 _backends 为空
        时统计也能反映既有 DB（review round-8 issue 1）。
        """
        self.cleanup()
        all_stats: dict[str, int] = {}
        for backend in self._backends.values():
            all_stats.update(backend.namespace_stats())
        return all_stats

    @property
    def active_scope_names(self) -> list[str]:
        return [s[0] for s in self._active_scopes]
