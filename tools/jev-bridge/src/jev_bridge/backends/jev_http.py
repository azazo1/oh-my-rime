"""Jev 兼容后端: POST <base_url>/v1/systemone.

同时覆盖两种部署:
- 本机 localjev-mlx (http://127.0.0.1:8090, Laya 权重 + MLX)
- 云端 TypeSafe Jev (https://api.typesafe.ai/v1/systemone)

请求体: {"state": ..., "model": ..., "questions": {name: {...}}}
响应体: {"model": ..., "answers": {name: {"type": "choice", "choice": ...,
        "probabilities": {...}, "confidence": ...}}}
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .base import ScoreResult

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class JevHttpBackend:
    def __init__(
        self, base_url: str, api_key: str = "", timeout_ms: int = 800, allow_cloud: bool = False
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = max(timeout_ms, 1) / 1000
        self.allow_cloud = allow_cloud
        host = (urlparse(self.base_url).hostname or "").lower()
        self.is_cloud = host not in LOCAL_HOSTS
        self.name = f"http:{host}"

    # ------------------------------------------------------------------ 内部

    def _post(self, path: str, body: dict) -> tuple[int, dict | None, str | None]:
        if self.is_cloud and not self.allow_cloud:
            return 0, None, "cloud_disabled"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = response.read().decode("utf-8", "replace")
                return response.status, json.loads(payload), None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            log.warning("后端 HTTP %s: %s", exc.code, detail)
            return exc.code, None, f"backend_http_{exc.code}"
        except urllib.error.URLError as exc:
            log.warning("后端连接失败: %s", exc)
            return 0, None, "backend_unreachable"
        except (TimeoutError, json.JSONDecodeError) as exc:
            log.warning("后端响应异常: %s", exc)
            return 0, None, "bad_response"

    # ------------------------------------------------------------------ 接口

    def score(self, state: Any, question: dict, model: str) -> ScoreResult:
        started = time.perf_counter()
        name = str(question.get("name") or "best_continuation")
        payload = {
            "state": state,
            "model": model or "jev-latest",
            "questions": {name: {key: value for key, value in question.items() if key != "name"}},
        }
        status, body, error = self._post("/v1/systemone", payload)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if error:
            return ScoreResult(
                ok=False, backend=self.name, model=model, latency_ms=latency_ms, error=error
            )

        answer = ((body or {}).get("answers") or {}).get(name) or {}
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or not probabilities:
            return ScoreResult(
                ok=False,
                backend=self.name,
                model=str((body or {}).get("model") or model),
                latency_ms=latency_ms,
                error="bad_response",
                raw=body,
            )
        confidence = answer.get("confidence")
        return ScoreResult(
            ok=True,
            backend=self.name,
            model=str((body or {}).get("model") or model),
            probabilities={str(k): float(v) for k, v in probabilities.items()},
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            latency_ms=latency_ms,
            raw=body,
        )

    def raw_call(self, body: dict) -> dict:
        status, payload, error = self._post("/v1/systemone", body)
        if error:
            raise RuntimeError(error)
        return payload or {}
