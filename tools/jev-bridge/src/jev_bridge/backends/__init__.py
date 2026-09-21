"""评分后端: 统一接口 + 工厂."""

from __future__ import annotations

from ..config import Config
from .base import Backend, ScoreResult

__all__ = ["Backend", "ScoreResult", "build_backend"]


def build_backend(config: Config) -> Backend:
    if config.backend == "mock":
        from .mock import MockBackend

        return MockBackend()
    if config.backend == "http":
        from .jev_http import JevHttpBackend

        return JevHttpBackend(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout_ms=config.backend_timeout_ms,
            allow_cloud=config.allow_cloud,
        )
    raise ValueError(f"未知 backend: {config.backend}")
