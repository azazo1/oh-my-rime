"""按平台推导路径的规则, 以及 Lua 侧是否用了同一套规则."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import RIME_DIR

from jev_bridge import paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """路径相关的环境变量在每个用例里都从干净状态开始."""
    for name in (
        "JEV_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "LOCALAPPDATA",
        "APPDATA",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "RIME_USER_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_macos_runtime_dir(monkeypatch) -> None:
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.platform_name() == "macos"
    assert paths.default_runtime_dir() == paths.home_dir() / "Library" / "Caches" / "rime-jev"


def test_linux_runtime_dir_prefers_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    assert paths.platform_name() == "linux"
    assert paths.default_runtime_dir() == tmp_path / "xdg-cache" / "rime-jev"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert paths.default_runtime_dir() == paths.home_dir() / ".cache" / "rime-jev"


def test_windows_runtime_dir_uses_localappdata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert paths.platform_name() == "windows"
    assert paths.default_runtime_dir() == tmp_path / "AppData" / "Local" / "rime-jev"


def test_runtime_dir_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JEV_RUNTIME_DIR", str(tmp_path / "custom"))
    assert paths.default_runtime_dir() == tmp_path / "custom"


def test_rime_user_dir_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RIME_USER_DIR", str(tmp_path / "rime"))
    assert paths.resolve_rime_user_dir() == tmp_path / "rime"
    assert paths.resolve_rime_user_dir("~/explicit") == Path("~/explicit").expanduser()


def test_rime_user_dir_prefers_existing_candidate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    candidates = paths.rime_user_dir_candidates()
    assert candidates[0] == tmp_path / "data" / "fcitx5" / "rime"
    # 都不存在时回落到第一个候选
    assert paths.resolve_rime_user_dir() == candidates[0]
    # 存在 ibus 目录时优先 fcitx5, 只建 ibus 时选中 ibus
    (tmp_path / "config" / "ibus" / "rime").mkdir(parents=True)
    assert paths.resolve_rime_user_dir() == candidates[1]
    (tmp_path / "data" / "fcitx5" / "rime").mkdir(parents=True)
    assert paths.resolve_rime_user_dir() == candidates[0]


def test_display_path_uses_tilde() -> None:
    assert paths.display_path(paths.home_dir() / "x" / "y") == "~/x/y"
    assert paths.display_path(Path("/opt/other")).startswith("/opt/")


def test_as_dict_reports_platform_and_candidates() -> None:
    payload = paths.as_dict()
    assert payload["platform"] in {"macos", "linux", "windows"}
    assert payload["queue_dir"].endswith("/queue")
    assert payload["schema_patch_target"].endswith("wanxiang.custom.yaml")
    assert payload["rime_user_dir_candidates"]


def test_lua_and_python_agree_on_runtime_dir() -> None:
    """两侧必须算出同一个根目录, 否则队列/缓存永远对不上."""
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("本机没有 lua 解释器")
    script = (
        f'package.path = "{RIME_DIR}/lua/?.lua;" .. package.path '
        'io.write(require("jev/jev_platform").runtime_dir())'
    )
    result = subprocess.run(
        [lua, "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout) == paths.default_runtime_dir()
