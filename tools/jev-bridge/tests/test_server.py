"""HTTP 调试端点与配置校验."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import make_request

from jev_bridge.cache import DiskCache
from jev_bridge.config import Config, ConfigError, load_config
from jev_bridge.config_migrations import ConfigMigrationError
from jev_bridge.server import build_server


@pytest.fixture
def server(config: Config):
    config.http_port = 0  # 交给系统分配空闲端口
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    bridge = build_server(config, cache)
    bridge.start()
    port = bridge._httpd.server_address[1]  # type: ignore[union-attr]
    yield bridge, f"http://127.0.0.1:{port}"
    bridge.stop()


def test_health_endpoint(server) -> None:
    _, base = server
    with urllib.request.urlopen(f"{base}/health", timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["ok"] is True
    assert payload["backend"] == "mock"
    assert "queue_dir" in payload


def test_rerank_endpoint(server) -> None:
    _, base = server
    request = make_request(["你好", "尼豪"], mock_probabilities={"0": 0.05, "1": 0.95})
    body = json.dumps(request).encode("utf-8")
    http_request = urllib.request.Request(
        f"{base}/v1/rerank", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(http_request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["ok"] is True
    assert payload["order"] == [1, 0]


def test_systemone_passthrough_with_mock(server) -> None:
    _, base = server
    body = json.dumps(
        {
            "state": {"context": "", "candidates": [{"id": "0", "text": "你好"}]},
            "model": "mock-1",
            "questions": {"q": {"type": "choice", "criteria": {"0": "你好"}}},
        }
    ).encode("utf-8")
    http_request = urllib.request.Request(
        f"{base}/v1/systemone", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(http_request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["answers"]["q"]["choice"] == "0"


def test_unknown_route_returns_404(server) -> None:
    _, base = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/nope", timeout=3)
    assert excinfo.value.code == 404


def test_config_rejects_cloud_without_permission(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version = 1\nbackend = "http"\nbase_url = "https://api.typesafe.ai"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text(
        'config_version = 1\nbackend = "http"\n'
        'base_url = "https://api.typesafe.ai"\nallow_cloud = true\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.is_cloud is True


def test_config_rejects_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("config_version = 1\nnope = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_config_requires_version(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('backend = "mock"\n', encoding="utf-8")
    with pytest.raises(ConfigMigrationError):
        load_config(path)


def test_config_future_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("config_version = 99\n", encoding="utf-8")
    with pytest.raises(ConfigMigrationError):
        load_config(path)
