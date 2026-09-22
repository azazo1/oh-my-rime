"""磁盘缓存: 一个键一个 JSON 文件, 带 TTL 与条目数上限.

沿用 Rime 侧的约定: 缓存文件写在 cache_dir/<key>.json, 内容为 sidecar 响应体
(不含 id), 因此 Lua 可以直接读它而完全不经过 sidecar.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    expired: int = 0
    evicted: int = 0
    errors: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def as_dict(self) -> dict:
        data = {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "expired": self.expired,
            "evicted": self.evicted,
            "errors": self.errors,
        }
        data["hit_rate"] = self.hit_rate
        return data


def write_json_atomic(path: Path, payload: dict) -> None:
    """先写临时文件再 rename, 保证读取方永远看不到半截文件."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class DiskCache:
    def __init__(self, directory: Path, ttl_s: int = 600, max_entries: int = 2000):
        self.directory = Path(directory)
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.stats = CacheStats()
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- 后端身份

    def _identity_file(self) -> Path:
        return self.directory / ".backend"

    def drop_if_backend_changed(self, identity: str) -> int:
        """后端/模型换了就清空缓存, 返回清掉的条数.

        缓存键由 Rime 侧算 (只含上文/编码/候选/提示词版本), 不含模型身份, 所以换 checkpoint
        之后旧分数会继续被命中; 与其把身份塞进跨语言键里, 不如在换后端时直接作废旧结果.
        """
        path = self._identity_file()
        previous = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if previous == identity:
            return 0
        removed = self.clear()
        try:
            path.write_text(identity, encoding="utf-8")
        except OSError as exc:
            log.warning("写后端身份标记失败: %s", exc)
        if previous:
            log.info("后端从 %s 换成 %s, 已清掉 %d 条缓存", previous, identity, removed)
        return removed

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self.path_for(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            self.stats.misses += 1
            return None
        except (json.JSONDecodeError, OSError) as exc:
            self.stats.errors += 1
            log.warning("缓存文件损坏, 已删除: %s (%s)", path.name, exc)
            path.unlink(missing_ok=True)
            self.stats.misses += 1
            return None

        if payload.get("expires_at", 0) <= int(time.time()):
            self.stats.expired += 1
            self.stats.misses += 1
            path.unlink(missing_ok=True)
            return None

        self.stats.hits += 1
        try:
            os.utime(path, None)
        except OSError:
            pass
        return payload

    def put(self, key: str, payload: dict) -> None:
        payload = dict(payload)
        payload.setdefault("expires_at", int(time.time()) + self.ttl_s)
        try:
            write_json_atomic(self.path_for(key), payload)
            self.stats.writes += 1
        except OSError as exc:
            self.stats.errors += 1
            log.error("写缓存失败: %s (%s)", key, exc)
            return
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        try:
            entries = sorted(
                self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime
            )
        except OSError:
            return
        overflow = len(entries) - self.max_entries
        for path in entries[: max(0, overflow)]:
            try:
                path.unlink()
                self.stats.evicted += 1
            except OSError:
                pass

    def clear(self) -> int:
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
