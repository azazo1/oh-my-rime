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
        "JEV_HTTP_HOST",
        "JEV_HTTP_PORT",
        "JEV_LOG_LEVEL",
        "JEV_DEBUG",
        "TYPESAFE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    from_file = load_config(example_config_path())
    assert asdict(from_file) == asdict(Config())


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
