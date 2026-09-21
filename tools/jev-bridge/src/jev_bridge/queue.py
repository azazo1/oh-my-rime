"""文件队列: Rime 侧 Lua 没有 socket, 只能靠 <id>.req.json / <id>.res.json 交换数据.

设计要点:
- 请求与响应都先写临时文件再 rename, 读取方不会看到半截内容;
- 同一个缓存键的并发请求会被合并, 只做一次后端调用;
- 过期的 prefetch 请求直接丢弃 (用户早就换词了, 算出来也没用);
- 孤儿文件 (超过 stale_file_s 没被读取) 定期清理.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cache import write_json_atomic
from .config import Config
from .keys import PROMPT_VERSION, cache_key, normalize_text
from .rerank import RequestError, Reranker, validate_request

log = logging.getLogger(__name__)

HEARTBEAT_NAME = "heartbeat"


@dataclass
class QueueStats:
    processed: int = 0
    coalesced: int = 0
    dropped_stale: int = 0
    dropped_invalid: int = 0
    errors: int = 0
    cleaned: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "coalesced": self.coalesced,
            "dropped_stale": self.dropped_stale,
            "dropped_invalid": self.dropped_invalid,
            "errors": self.errors,
            "cleaned": self.cleaned,
            "uptime_s": round(time.time() - self.started_at, 1),
        }


class QueueWorker:
    def __init__(self, config: Config, reranker: Reranker):
        self.config = config
        self.reranker = reranker
        self.queue_dir = config.queue_path
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.stats = QueueStats()
        self.heartbeat_path = self.queue_dir / HEARTBEAT_NAME
        self._last_heartbeat = 0

    # ------------------------------------------------------------------ 心跳

    def _write_heartbeat(self) -> None:
        """每秒写一次心跳, Rime 侧据此判断 sidecar 是否在线.

        没有心跳时 Lua 既不会投递请求 (避免队列无界增长), 也不会在 sync 模式下空等.
        """
        now = int(time.time())
        if now == self._last_heartbeat:
            return
        self._last_heartbeat = now
        try:
            self.heartbeat_path.write_text(str(now), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ 主循环

    def run_forever(self, stop_event: threading.Event) -> None:
        interval = self.config.poll_interval_ms / 1000
        log.info("队列监听中: %s (轮询 %.0fms)", self.queue_dir, self.config.poll_interval_ms)
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 单次 tick 的异常不能让 sidecar 退出
                self.stats.errors += 1
                log.exception("队列处理异常")
            stop_event.wait(interval)
        log.info("队列监听结束: %s", json.dumps(self.stats.as_dict(), ensure_ascii=False))

    def tick(self) -> int:
        self._write_heartbeat()
        self._cleanup_stale_files()
        groups: dict[str, list[tuple[str, Path]]] = {}
        for path in sorted(self.queue_dir.glob("*.req.json")):
            request = self._load(path)
            if request is None:
                continue
            try:
                validate_request(request)
            except RequestError as exc:
                self.stats.dropped_invalid += 1
                log.warning("丢弃不合法请求 %s (%s)", path.name, exc.code)
                self._unlink(path)
                continue

            if self._is_stale_prefetch(request, path):
                self.stats.dropped_stale += 1
                log.debug("丢弃过期预取 %s (code=%s)", path.name, request.get("code"))
                self._unlink(path)
                continue

            key = self._key_of(request)
            groups.setdefault(key, []).append((str(request.get("id") or ""), path))

        processed = 0
        for key, members in groups.items():
            if len(members) > 1:
                self.stats.coalesced += len(members) - 1
            first_id, first_path = members[0]
            request = self._load(first_path)
            if request is None:
                continue
            request["id"] = first_id
            response = self.reranker.handle(request)
            for request_id, path in members:
                response["id"] = request_id
                self._write_response(request_id, response)
                self._unlink(path)
            processed += 1
            self.stats.processed += 1
        return processed

    # ------------------------------------------------------------------ 内部

    def _key_of(self, request: dict) -> str:
        candidates = list(request.get("candidates") or [])[: self.config.max_candidates]
        return cache_key(
            str(request.get("schema_id") or ""),
            str(request.get("code") or ""),
            str(request.get("context") or ""),
            [normalize_text(str(candidate.get("text") or "")) for candidate in candidates],
            int(request.get("prompt_version") or PROMPT_VERSION),
        )

    def _is_stale_prefetch(self, request: dict, path: Path) -> bool:
        """用文件 mtime 而不是请求里的 ts 判断年龄: Lua 侧没有可靠的 epoch 时钟."""
        if request.get("mode") != "prefetch":
            return False
        try:
            age_ms = (time.time() - path.stat().st_mtime) * 1000
        except OSError:
            return False
        return age_ms > self.config.prefetch_max_age_ms

    def _load(self, path: Path) -> dict | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            self.stats.dropped_invalid += 1
            log.warning("请求文件不可读, 已删除: %s (%s)", path.name, exc)
            self._unlink(path)
            return None
        if not isinstance(data, dict):
            self.stats.dropped_invalid += 1
            self._unlink(path)
            return None
        return data

    def _write_response(self, request_id: str, response: dict) -> None:
        if not request_id:
            log.warning("请求缺少 id, 无法写回响应")
            return
        path = self.queue_dir / f"{request_id}.res.json"
        try:
            write_json_atomic(path, response)
        except OSError as exc:
            self.stats.errors += 1
            log.error("写响应失败 %s: %s", path.name, exc)

    def _cleanup_stale_files(self) -> None:
        deadline = time.time() - 60
        for pattern in ("*.req.json", "*.res.json"):
            for path in self.queue_dir.glob(pattern):
                try:
                    if path.stat().st_mtime < deadline:
                        path.unlink()
                        self.stats.cleaned += 1
                        log.debug("清理孤儿文件 %s", path.name)
                except OSError:
                    pass

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ 供调试

    def pending(self) -> int:
        return len(list(self.queue_dir.glob("*.req.json")))
