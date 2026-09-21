"""本地 HTTP 端点, 只用于调试与手工联调 (Rime 侧走文件队列).

GET  /health       进程与后端状态
GET  /stats        队列与缓存计数
POST /v1/rerank    直接吃队列请求体, 返回队列响应体 (不过队列)
POST /v1/systemone 原样转发给后端 (便于与云端 Jev / localjev-mlx 对比)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .backends import build_backend
from .cache import DiskCache
from .config import Config
from .queue import QueueWorker
from .rerank import Reranker

log = logging.getLogger(__name__)

MAX_BODY = 1 << 20


class _Handler(BaseHTTPRequestHandler):
    server_version = f"jev-bridge/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 工具

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 父类接口
        log.debug("http %s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802 - 父类接口
        state = self.server.bridge_state  # type: ignore[attr-defined]
        if self.path.startswith("/health"):
            self._send(200, state.health())
        elif self.path.startswith("/stats"):
            self._send(200, state.stats())
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - 父类接口
        state = self.server.bridge_state  # type: ignore[attr-defined]
        body = self._read_body()
        if body is None:
            self._send(400, {"ok": False, "error": "bad_request"})
            return
        if self.path.startswith("/v1/rerank"):
            self._send(200, state.reranker.handle(body))
        elif self.path.startswith("/v1/systemone"):
            try:
                self._send(200, state.backend.raw_call(body))
            except Exception as exc:  # noqa: BLE001 - 调试端点, 直接把错误回给调用方
                self._send(502, {"ok": False, "error": str(exc)})
        else:
            self._send(404, {"ok": False, "error": "not_found"})


class BridgeServer:
    def __init__(self, config: Config, backend, cache: DiskCache, worker: QueueWorker):
        self.config = config
        self.backend = backend
        self.cache = cache
        self.worker = worker
        self.reranker: Reranker = worker.reranker
        self.started_at = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 状态

    def health(self) -> dict:
        return {
            "ok": True,
            "version": __version__,
            "backend": self.backend.name,
            "base_url": self.config.base_url,
            "model": self.config.model,
            "allow_cloud": self.config.allow_cloud,
            "queue_dir": str(self.config.queue_path),
            "pending": self.worker.pending(),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def stats(self) -> dict:
        return {
            "ok": True,
            "queue": self.worker.stats.as_dict(),
            "cache": self.cache.stats.as_dict(),
        }

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer(
            (self.config.http_host, self.config.http_port), _Handler
        )
        self._httpd.daemon_threads = True
        self._httpd.bridge_state = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="jev-bridge-http", daemon=True
        )
        self._thread.start()
        log.info(
            "HTTP 调试端点: http://%s:%d/health",
            self.config.http_host,
            self.config.http_port,
        )

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def build_server(config: Config, cache: DiskCache) -> BridgeServer:
    backend = build_backend(config)
    reranker = Reranker(config, cache, backend)
    worker = QueueWorker(config, reranker)
    return BridgeServer(config, backend, cache, worker)
