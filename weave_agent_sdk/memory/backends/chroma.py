"""ChromaDB 向量存储后端（可选，用于语义搜索）。

需要: pip install chromadb
默认 embedding: all-MiniLM-L6-v2（本地，无需 API key）
可配置 OpenAI embedding（需 OPENAI_API_KEY）
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from weave_agent_sdk.types import SearchResult

logger = logging.getLogger(__name__)


class ChromaBackend:
    """ChromaDB 后端，提供语义向量搜索。

    用法:
        backend = ChromaBackend(path="./data/vectors/", embedding_model="text-embedding-3-small")
        results = backend.knowledge_search("冷启动问题", namespaces=["default:global:knowledge"])
    """

    # 能力声明：chroma 仅支持 knowledge（向量语义搜索）；stream/state 需结构化存储
    # （MemoryManager 据此在配置到 stream/state 时告警并回退 sqlite，见 docs/issues/010）。
    SUPPORTED_ACCESS_TYPES = frozenset({"knowledge"})

    def __init__(self, path: str | Path, embedding_model: str | None = None):
        self._path = str(path)
        self._embedding_model = embedding_model or "all-MiniLM-L6-v2"
        self._client = None
        self._collection = None
        self._warned_cleanup = False
        self._warned_stats = False

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import chromadb
            except ImportError:
                raise ImportError(
                    "chromadb package required. Install with: pip install chromadb"
                )
            self._client = chromadb.PersistentClient(path=self._path)
        return self._client

    def _get_collection(self, namespace: str) -> Any:
        safe_name = namespace.replace(":", "_").replace("/", "_")
        try:
            return self.client.get_or_create_collection(name=safe_name)
        except Exception:
            return self.client.create_collection(name=safe_name)

    @staticmethod
    def _is_expired(metadata: dict[str, Any] | None, now: float | None = None) -> bool:
        """判断条目是否已过期（metadata 中 _expires_at 存在且 <= 当前时间）。

        与 SQLite/File 后端契约一致：_expires_at 为 None 表示永不过期。
        """
        exp = (metadata or {}).get("_expires_at")
        if exp is None:
            return False
        return exp <= (now if now is not None else time.time())

    def knowledge_add(self, content: str, namespace: str, metadata: dict[str, Any] | None = None, ttl: float | None = None) -> str:
        """写入一条知识条目。

        Args:
            content: 知识文本
            namespace: 命名空间（决定 collection）
            metadata: 附加元数据
            ttl: 过期时间（秒）。此前 ChromaBackend 无 ttl 形参，而
                SQLiteKnowledgeMemory.add 无条件以 4 个位置参数调用
                knowledge_add(content, namespace, metadata, ttl)——配置
                `backend: chroma` 的 knowledge scope 后写入立即抛
                TypeError（review round-7 issue 1）。ttl 记录到 metadata
                的 _expires_at，查询时按过期条件过滤，与 SQLite/File
                后端契约一致。
        """
        collection = self._get_collection(namespace)
        entry_id = str(uuid.uuid4())
        meta = dict(metadata or {})
        if ttl:
            meta["_expires_at"] = time.time() + ttl
        collection.add(
            ids=[entry_id],
            documents=[content],
            metadatas=[meta],
        )
        return entry_id

    def knowledge_search(self, query: str, namespaces: list[str] | None, top_k: int) -> list[SearchResult]:
        if not namespaces:
            # 搜索所有 collection
            namespaces = [c.name for c in self.client.list_collections()]

        results: list[SearchResult] = []
        now = time.time()
        for ns in namespaces:
            collection = self._get_collection(ns)
            try:
                query_result = collection.query(query_texts=[query], n_results=top_k)
                ids = query_result.get("ids", [[]])[0]
                docs = query_result.get("documents", [[]])[0]
                distances = query_result.get("distances", [[]])[0]
                metadatas = query_result.get("metadatas", [[]])[0]

                for i, entry_id in enumerate(ids):
                    # 过滤已过期条目（metadata._expires_at），保证 TTL
                    # "每条记忆可配过期时间"语义在 chroma 后端一致
                    # （review round-7 issue 1）。
                    if self._is_expired(metadatas[i] if i < len(metadatas) else {}, now):
                        continue
                    score = 1.0 - min(distances[i], 1.0) if i < len(distances) else 0.0
                    results.append(SearchResult(
                        id=entry_id,
                        content=docs[i] if i < len(docs) else "",
                        score=score,
                        metadata=metadatas[i] if i < len(metadatas) else {},
                    ))
            except Exception as e:
                logger.warning(
                    "ChromaBackend.knowledge_search failed for namespace %s: %s", ns, e
                )

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    # ── MemoryManager 公共接口契约 ─────────────────────
    # round-7 fix 2 使 chroma 首次可经 MemoryManager 实例化后，manager 的
    # cleanup()/stats()/close() 会对每个后端调用这三个方法；此前 ChromaBackend
    # 均缺失，配置 `backend: chroma` 后管理面（status() / GET /memory /
    # DELETE /memory）抛 AttributeError（review round-7 issue 2）。

    def close(self) -> None:
        """释放 chroma client 引用。

        PersistentClient 的各版本 API 不一，统一释放引用即可。
        """
        self._client = None

    def cleanup_expired(self, limit: int = 100) -> int:
        """清理过期条目（与 SQLite/File 后端契约一致的占位实现）。

        Chroma collection 向量条目的批量删除依赖 chromadb 版本差异较大的
        API，此处返回 0 并告警——过期条目已在 knowledge_search 查询时按
        metadata._expires_at 过滤，读路径语义正确（review round-7 issue 2）。
        """
        if not self._warned_cleanup:
            self._warned_cleanup = True
            logger.warning(
                "ChromaBackend.cleanup_expired() is a no-op; expired chroma "
                "entries are filtered at query time but not physically deleted."
            )
        return 0

    def namespace_stats(self) -> dict[str, int]:
        """返回各 namespace 的条目数（Chroma 未实现，返回空并告警）。

        MemoryManager.stats() 对每个后端调用 namespace_stats()——此前
        ChromaBackend 缺此方法，配置 `backend: chroma` 后 status() /
        GET /agents/{name}/memory 抛 AttributeError（review round-7 issue 2）。
        """
        if not self._warned_stats:
            self._warned_stats = True
            logger.warning(
                "ChromaBackend.namespace_stats() is not implemented; chroma "
                "namespaces are excluded from memory stats."
            )
        return {}
