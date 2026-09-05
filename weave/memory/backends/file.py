"""JSON 文件存储后端（兼容 myKG/bePM 现有存储格式）。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from weave.types import SearchResult
from weave.utils.search import search_tokens


class FileBackend:
    """JSON 文件后端，兼容 myKG/bePM 的 flat-file 存储格式。

    注意: 无事务、无并发保护。生产环境建议使用 SQLite。
    """

    def __init__(self, directory: str | Path):
        self._db_path = str(directory)
        self._dir = Path(self._db_path)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, namespace: str, access_type: str) -> Path:
        return self._dir / f"{namespace.replace(':', '_')}_{access_type}.json"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write(self, path: Path, data: list[dict[str, Any]]) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _is_expired(item: dict[str, Any], now: float | None = None) -> bool:
        """判断条目是否已过期（_expires_at 存在且 <= 当前时间）。"""
        exp = item.get("_expires_at")
        if exp is None:
            return False
        return exp <= (now if now is not None else time.time())

    # ── Stream ────────────────────────────────────────

    def stream_append(self, entry: dict[str, Any], namespace: str, ttl: float | None = None) -> str:
        path = self._get_path(namespace, "stream")
        items = self._read(path)
        entry_id = str(uuid.uuid4())
        now = time.time()
        entry["_id"] = entry_id
        entry["_created_at"] = now
        # TTL：配置了 ttl 时记录 _expires_at，读取时过滤、cleanup 时回收——
        # 与 SQLite 后端契约一致。此前 FileBackend 无 TTL 概念，接入后过期
        # 条目会永驻文件且被查询返回（review round-6 issue 2）。
        if ttl:
            entry["_expires_at"] = now + ttl
        items.append(entry)
        self._write(path, items)
        self.cleanup_expired()
        return entry_id

    def stream_last(self, n: int, namespaces: list[str] | None) -> list[dict[str, Any]]:
        now = time.time()
        all_items: list[dict[str, Any]] = []
        if not namespaces:
            # 扫描所有 stream 文件
            for f in self._dir.glob("*_stream.json"):
                items = [i for i in self._read(f) if not self._is_expired(i, now)]
                all_items.extend(items)
        else:
            for ns in namespaces:
                items = [i for i in self._read(self._get_path(ns, "stream")) if not self._is_expired(i, now)]
                all_items.extend(items)

        all_items.sort(key=lambda x: x.get("_created_at", 0))
        return all_items[-n:]

    def stream_trim(self, max_items: int, namespace: str) -> None:
        items = self._read(self._get_path(namespace, "stream"))
        if len(items) > max_items:
            self._write(self._get_path(namespace, "stream"), items[-max_items:])

    def stream_delete_after(self, watermark: float, namespace: str) -> int:
        """删除 _created_at > watermark 的 stream 条目（用于状态回滚）。

        与 SQLite 后端契约一致：watermark 为快照时刻 time.time()，据此删除
        快照后新增的条目。
        """
        path = self._get_path(namespace, "stream")
        items = self._read(path)
        kept = [i for i in items if (i.get("_created_at") or 0.0) <= watermark]
        removed = len(items) - len(kept)
        if removed > 0:
            self._write(path, kept)
        return removed

    # ── State ─────────────────────────────────────────

    def state_get(self, key: str, namespace: str) -> Any | None:
        items = self._read(self._get_path(namespace, "state"))
        now = time.time()
        for item in reversed(items):
            if self._is_expired(item, now):
                continue
            if item.get("_key") == key:
                return item.get("_value")
        return None

    def state_set(self, key: str, value: Any, namespace: str, ttl: float | None = None) -> str:
        path = self._get_path(namespace, "state")
        items = self._read(path)
        # 移除旧值
        items = [i for i in items if i.get("_key") != key]
        entry_id = str(uuid.uuid4())
        now = time.time()
        item: dict[str, Any] = {"_id": entry_id, "_key": key, "_value": value, "_created_at": now}
        # TTL：与 SQLite 后端契约一致（review round-6 issue 2）
        if ttl:
            item["_expires_at"] = now + ttl
        items.append(item)
        self._write(path, items)
        self.cleanup_expired()
        return entry_id

    def state_delete(self, key: str, namespace: str) -> None:
        path = self._get_path(namespace, "state")
        items = self._read(path)
        items = [i for i in items if i.get("_key") != key]
        self._write(path, items)

    def state_get_all(self, namespaces: list[str] | None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        now = time.time()
        if not namespaces:
            for f in self._dir.glob("*_state.json"):
                for item in self._read(f):
                    if self._is_expired(item, now):
                        continue
                    if "_key" in item:
                        result[item["_key"]] = item["_value"]
        else:
            for ns in namespaces:
                for item in self._read(self._get_path(ns, "state")):
                    if self._is_expired(item, now):
                        continue
                    if "_key" in item:
                        result[item["_key"]] = item["_value"]
        return result

    # ── Knowledge ─────────────────────────────────────

    def knowledge_add(self, content: str, namespace: str, metadata: dict[str, Any] | None = None, ttl: float | None = None) -> str:
        path = self._get_path(namespace, "knowledge")
        items = self._read(path)
        entry_id = str(uuid.uuid4())
        now = time.time()
        item: dict[str, Any] = {
            "_id": entry_id,
            "_content": content,
            "_metadata": metadata or {},
            "_created_at": now,
        }
        # TTL：与 SQLite 后端契约一致（review round-6 issue 2）
        if ttl:
            item["_expires_at"] = now + ttl
        items.append(item)
        self._write(path, items)
        self.cleanup_expired()
        return entry_id

    def knowledge_search(self, query: str, namespaces: list[str] | None, top_k: int) -> list[SearchResult]:
        results: list[tuple[float, dict[str, Any]]] = []
        terms = search_tokens(query)
        now = time.time()

        if not namespaces:
            files = list(self._dir.glob("*_knowledge.json"))
        else:
            files = [self._get_path(ns, "knowledge") for ns in namespaces]

        for f in files:
            for item in self._read(f):
                if self._is_expired(item, now):
                    continue
                content = item.get("_content", "")
                score = sum(1 for t in terms if t.lower() in content.lower()) / max(len(terms), 1)
                if score > 0:
                    results.append((score, item))

        results.sort(key=lambda x: x[0], reverse=True)
        return [
            SearchResult(id=r[1].get("_id", ""), content=r[1].get("_content", ""), score=r[0], metadata=r[1].get("_metadata", {}))
            for r in results[:top_k]
        ]

    # ── Cleanup ───────────────────────────────────────

    def cleanup_expired(self, limit: int = 100) -> int:
        """被动清理过期条目（写入时调用）。

        与 SQLite 后端契约一致：扫描所有 JSON 文件，移除已过期条目并写回，
        受 limit 有界约束（每调用最多清理 limit 条）。此前 FileBackend 无
        TTL/过期清理概念，接入后过期条目不会被查询返回、可被回收
        （review round-6 issue 2 / issue 5）。
        """
        now = time.time()
        removed = 0
        for f in list(self._dir.glob("*.json")):
            if removed >= limit:
                break
            items = self._read(f)
            kept: list[dict[str, Any]] = []
            for item in items:
                if removed < limit and self._is_expired(item, now):
                    removed += 1
                    continue
                kept.append(item)
            if len(kept) != len(items):
                self._write(f, kept)
        return removed

    # ── Namespace helpers ─────────────────────────────

    def namespace_stats(self) -> dict[str, int]:
        """返回各 namespace 的条目数（不含已过期条目，与 SQLite 一致）。

        文件名形如 {namespace.replace(':', '_')}_{access_type}.json——从文件名
        逆向还原 namespace（scope 名/scope_id 中的下划线无法区分，为
        flat-file 格式的固有局限，best-effort 还原；review round-6 issue 5）。
        """
        now = time.time()
        counts: dict[str, int] = {}
        for f in self._dir.glob("*.json"):
            name = f.name
            ns = None
            for at in ("_stream", "_state", "_knowledge"):
                suffix = at + ".json"
                if name.endswith(suffix):
                    ns = name[: -len(suffix)].replace("_", ":")
                    break
            if ns is None:
                continue
            count = sum(1 for item in self._read(f) if not self._is_expired(item, now))
            counts[ns] = counts.get(ns, 0) + count
        return counts

    def close(self) -> None:
        """关闭后端（兼容 MemoryManager.close() 契约）。

        flat-file 后端无连接需释放。此前 FileBackend 无 close()，
        DELETE /agents/{name}/memory 管理面经 MemoryManager.close() 调用时
        抛 AttributeError（review round-7 issue 2）。
        """
        return None
