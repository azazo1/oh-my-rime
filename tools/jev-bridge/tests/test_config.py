"""配置示例文件与默认值的一致性, 以及配置迁移的边界."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from jev_bridge.config import (
    Config,
    ConfigError,
    dump_default_config,
    example_config_path,
    load_config,
)


def test_example_config_exists() -> None:
    assert example_config_path().exists(), "必须随仓库提供 config.toml.example"


def test_default_http_port() -> None:
    assert Config().http_port == 20006


def test_default_backend_port() -> None:
    """Laya 后端默认在 20007 (需要后端自己监听同一端口)."""
    assert Config().base_url == "http://127.0.0.1:20007"


def test_example_matches_builtin_defaults(monkeypatch) -> None:
    """示例文件必须与代码里的默认值完全一致, 否则复制出来的配置会有意外差异."""
    for name in (
        "JEV_BACKEND",
        "JEV_BASE_URL",
        "JEV_MODEL",
        "JEV_ALLOW_CLOUD",
        "JEV_RUNTIME_DIR",
        "JEV_HTTP_HOST",
        "JEV_HTTP_PORT",
        "JEV_LOG_LEVEL",
        "JEV_DEBUG",
        "TYPESAFE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    from_file = load_config(example_config_path())
    assert asdict(from_file) == asdict(Config())


def test_example_does_not_pin_runtime_dir() -> None:
    """示例里 runtime_dir 必须是注释掉的, 否则换平台复制出来就是错的."""
    text = example_config_path().read_text(encoding="utf-8")
    for line in text.splitlines():
        if "runtime_dir" in line:
            assert line.lstrip().startswith("#"), line


def test_v1_config_is_migrated_to_runtime_dir(tmp_path: Path) -> None:
    """v1 的三个目录收敛成 v2 的单一 runtime_dir."""
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version = 1\nbackend = "mock"\n'
        f'queue_dir = "{tmp_path}/runtime/queue"\n'
        f'cache_dir = "{tmp_path}/runtime/cache"\n'
        f'log_dir = "{tmp_path}/runtime/log"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.runtime_dir == str(tmp_path / "runtime")
    assert config.queue_path == tmp_path / "runtime" / "queue"
    assert config.cache_path == tmp_path / "runtime" / "cache"


def test_v1_config_with_diverged_dirs_falls_back_to_queue_parent(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version = 1\n'
        f'queue_dir = "{tmp_path}/a/queue"\n'
        f'cache_dir = "{tmp_path}/b/cache"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.runtime_dir == str(tmp_path / "a")


def test_init_config_copies_example(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    written = dump_default_config(target)
    assert written == target
    assert target.read_text(encoding="utf-8") == example_config_path().read_text(encoding="utf-8")
    assert load_config(target) == Config()
    # 已存在时不覆盖
    target.write_text('config_version = 1\nbackend = "mock"\n', encoding="utf-8")
    dump_default_config(target)
    assert "backend" in target.read_text(encoding="utf-8")


def test_init_config_reports_missing_example(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jev_bridge.config.example_config_path", lambda: tmp_path / "not-there.toml"
    )
    with pytest.raises(ConfigError):
        dump_default_config(tmp_path / "config.toml")
