"""sidecar 配置: 默认值, 配置文件读取, 环境变量覆盖与校验."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import config_migrations, paths

CONFIG_VERSION = config_migrations.CURRENT_VERSION
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("JEV_CONFIG", "~/.config/rime-jev/config.toml")
).expanduser()
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}
EXAMPLE_FILE_NAME = "config.toml.example"


class ConfigError(Exception):
    """配置文件不合法或与运行环境冲突."""


@dataclass
class Config:
    config_version: int = CONFIG_VERSION
    backend: str = "mock"
    base_url: str = "http://127.0.0.1:8090"
    model: str = "laya-multilingual"
    api_key: str = ""
    allow_cloud: bool = False

    # 队列/缓存/日志共同的父目录; 默认按平台推导, 一般不需要在配置里写
    runtime_dir: str = field(default_factory=lambda: str(paths.default_runtime_dir()))

    http_host: str = "127.0.0.1"
    http_port: int = 20006

    poll_interval_ms: int = 5
    prefetch_max_age_ms: int = 1500
    backend_timeout_ms: int = 800

    cache_ttl_s: int = 600
    cache_max_entries: int = 2000

    max_candidates: int = 8
    min_confidence: float = 0.5
    min_top_prob: float = 0.34
    badge: str = "AI"
    show_confidence: bool = False

    log_level: str = "INFO"
    debug: bool = False

    @property
    def runtime_path(self) -> Path:
        return Path(self.runtime_dir).expanduser()

    @property
    def queue_path(self) -> Path:
        return self.runtime_path / "queue"

    @property
    def cache_path(self) -> Path:
        return self.runtime_path / "cache"

    @property
    def log_path(self) -> Path:
        return self.runtime_path / "log"

    @property
    def backend_host(self) -> str:
        from urllib.parse import urlparse

        return (urlparse(self.base_url).hostname or "").lower()

    @property
    def is_cloud(self) -> bool:
        return self.backend_host not in LOCAL_HOSTS

    def validate(self) -> None:
        if self.backend not in {"mock", "http"}:
            raise ConfigError(f"未知 backend: {self.backend} (可选 mock / http)")
        if self.backend == "http" and not self.base_url:
            raise ConfigError("backend = http 时必须提供 base_url")
        if self.backend == "http" and self.is_cloud and not self.allow_cloud:
            raise ConfigError(
                f"base_url 指向非本机地址 ({self.backend_host}), 但 allow_cloud = false; "
                "确认要把上文发送到该服务后再显式打开 allow_cloud"
            )
        if self.poll_interval_ms < 1:
            raise ConfigError("poll_interval_ms 至少为 1")
        if self.prefetch_max_age_ms < 1:
            raise ConfigError("prefetch_max_age_ms 至少为 1")

    def ensure_dirs(self) -> None:
        for path in (self.queue_path, self.cache_path, self.log_path):
            path.mkdir(parents=True, exist_ok=True)


def _env_overrides(config: Config) -> Config:
    env_map = {
        "JEV_BACKEND": ("backend", str),
        "JEV_BASE_URL": ("base_url", str),
        "JEV_MODEL": ("model", str),
        "JEV_RUNTIME_DIR": ("runtime_dir", str),
        "JEV_ALLOW_CLOUD": ("allow_cloud", _as_bool),
        "JEV_HTTP_HOST": ("http_host", str),
        "JEV_HTTP_PORT": ("http_port", int),
        "JEV_LOG_LEVEL": ("log_level", str),
        "JEV_DEBUG": ("debug", _as_bool),
    }
    for env_key, (field_name, cast) in env_map.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        setattr(config, field_name, cast(raw))
    if os.environ.get("TYPESAFE_API_KEY"):
        config.api_key = os.environ["TYPESAFE_API_KEY"]
    return config


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: Path | None = None) -> Config:
    """读取配置; 文件不存在时返回默认值 (backends = mock, 可离线验证链路)."""
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    config = Config()
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        # 未知项与版本检查都交给 migrate: 旧版本的字段需要先迁移掉
        config = config_migrations.migrate(raw)
    config = _env_overrides(config)
    config.validate()
    return config


def example_config_path() -> Path:
    """仓库里的示例配置, 也是 `jev-bridge init-config` 的唯一来源."""
    return Path(__file__).resolve().parents[2] / EXAMPLE_FILE_NAME


def dump_default_config(path: Path | None = None) -> Path:
    """把示例配置复制成用户配置 (已存在则不覆盖)."""
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if config_path.exists():
        return config_path
    example = example_config_path()
    if not example.exists():
        raise ConfigError(
            f"找不到示例配置 {example}; 请在 tools/jev-bridge 目录下运行, 或手工创建 {config_path}"
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path


def as_dict(config: Config) -> dict:
    return asdict(config)
