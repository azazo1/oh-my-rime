"""sidecar 配置: 默认值, 配置文件读取, 环境变量覆盖与校验."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import config_migrations

CONFIG_VERSION = config_migrations.CURRENT_VERSION
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("JEV_CONFIG", "~/.config/rime-jev/config.toml")
).expanduser()
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}

_TEMPLATE = """# jev-bridge 配置
# 改完执行: just restart
config_version = 1

# 后端: mock (零依赖, 只验证链路) | http (Jev 兼容的 /v1/systemone)
backend = "mock"
base_url = "http://127.0.0.1:8090"
model = "laya-multilingual"
api_key = ""
# 云端后端会把上文送出本机, 必须显式置为 true 才允许访问非本机地址
allow_cloud = false

queue_dir = "~/Library/Caches/rime-jev/queue"
cache_dir = "~/Library/Caches/rime-jev/cache"
log_dir = "~/Library/Caches/rime-jev/log"

http_host = "127.0.0.1"
http_port = 8091

poll_interval_ms = 15
# 预取请求超过这个年龄就直接丢弃 (用户早就换词了, 算出来也没用)
prefetch_max_age_ms = 1500
backend_timeout_ms = 800

cache_ttl_s = 600
cache_max_entries = 2000

max_candidates = 8
# 候选顺序变化的最小置信度门限, 低于它就保持原顺序
min_confidence = 0.5
min_top_prob = 0.34
badge = "AI"
show_confidence = false

log_level = "INFO"
debug = false
"""


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

    queue_dir: str = "~/Library/Caches/rime-jev/queue"
    cache_dir: str = "~/Library/Caches/rime-jev/cache"
    log_dir: str = "~/Library/Caches/rime-jev/log"

    http_host: str = "127.0.0.1"
    http_port: int = 8091

    poll_interval_ms: int = 15
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
    def queue_path(self) -> Path:
        return Path(self.queue_dir).expanduser()

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir).expanduser()

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir).expanduser()

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

    def to_template(self) -> str:
        return _TEMPLATE


def _env_overrides(config: Config) -> Config:
    env_map = {
        "JEV_BACKEND": ("backend", str),
        "JEV_BASE_URL": ("base_url", str),
        "JEV_MODEL": ("model", str),
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
        unknown = set(raw) - {f.name for f in fields(Config)}
        if unknown:
            known = ", ".join(sorted(f.name for f in fields(Config)))
            raise ConfigError(
                f"{config_path} 存在未知配置项: {', '.join(sorted(unknown))}; 可用项: {known}"
            )
        config = config_migrations.migrate(raw)
    config = _env_overrides(config)
    config.validate()
    return config


def dump_default_config(path: Path | None = None) -> Path:
    """写出默认配置模板 (已存在则不覆盖)."""
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(_TEMPLATE, encoding="utf-8")
        config_path.chmod(0o600)
    return config_path


def as_dict(config: Config) -> dict:
    return asdict(config)
