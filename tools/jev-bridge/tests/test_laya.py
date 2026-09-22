"""隔离启动本机 Laya 后端的辅助逻辑 (不真正下载权重/不联网)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jev_bridge import laya


def _env_text(env: dict) -> str:
    return " ".join(f"{key}={value}" for key, value in env.items())


def test_default_repo_is_the_multilingual_checkpoint() -> None:
    """实测中文效果与延迟都更好, 所以默认用它; 想换回英文版用 --repo."""
    assert laya.DEFAULT_REPO == "aac6fef/laya-multilingual-mlx"
    env = laya.process_env(Path("/tmp/x"), 8090)
    assert env["LOCALJEV_REPO"] == laya.DEFAULT_REPO
    overridden = laya.process_env(Path("/tmp/x"), 8090, repo="convaiinnovations/laya")
    assert overridden["LOCALJEV_REPO"] == "convaiinnovations/laya"


def test_tool_command_is_isolated() -> None:
    command = laya.tool_command(8090)
    assert command[1:3] == ["tool", "run"]
    assert "--from" in command
    assert any("localjev-mlx[laya-mlx]" in part for part in command)
    assert command[-2:] == ["--judge-port", "8090"]
    # 不能碰项目 venv, 也不能用 install.sh 那套
    assert "install.sh" not in " ".join(command)


def test_process_env_keeps_state_inside_runtime_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HF_HOME", raising=False)
    env = laya.process_env(tmp_path, 8090)
    assert env["LOCALJEV_JUDGE_PORT"] == "8090"
    assert env["LOCALJEV_BIND"] == "127.0.0.1"
    assert env["USE_TF"] == "0"
    # 权重位置跟随 huggingface-hub 的默认值, 我们不重定向
    assert "HF_HOME" not in env
    assert laya.hf_cache_dir() == Path("~/.cache/huggingface").expanduser()
    # install.sh 会写的那两个位置都不该出现
    assert ".localjev-mlx" not in _env_text(env)
    assert ".config/localjev-mlx" not in _env_text(env)


def test_sanitize_no_proxy_drops_bracketed_ipv6() -> None:
    """httpx 会把 NO_PROXY 里的 [::1] 当成带端口的主机并抛错, 权重就下载不了."""
    raw = "127.0.0.1,.local,localhost,[::1],::1"
    assert laya.sanitize_no_proxy(raw) == "127.0.0.1,.local,localhost,::1"
    assert laya.sanitize_no_proxy("") == ""
    assert laya.sanitize_no_proxy(",, [::1] ,") == ""


def test_process_env_sanitizes_both_cases(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    monkeypatch.setenv("no_proxy", "[fe80::1],127.0.0.1")
    env = laya.process_env(tmp_path, 8090)
    assert env["NO_PROXY"] == "localhost"
    assert env["no_proxy"] == "127.0.0.1"


def test_process_env_passes_subfolder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LOCALJEV_SUBFOLDER", raising=False)
    assert "LOCALJEV_SUBFOLDER" not in laya.process_env(tmp_path, 8090)
    env = laya.process_env(tmp_path, 8090, subfolder="multilingual")
    assert env["LOCALJEV_SUBFOLDER"] == "multilingual"


def test_process_env_respects_user_hf_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    env = laya.process_env(tmp_path, 8090)
    assert env["HF_HOME"] == str(tmp_path / "hf")
    assert laya.hf_cache_dir() == tmp_path / "hf"


def test_pid_roundtrip(tmp_path: Path) -> None:
    assert laya.read_pid(tmp_path) is None
    laya.pid_file(tmp_path).write_text(str(os.getpid()), encoding="utf-8")
    assert laya.read_pid(tmp_path) == os.getpid()
    laya.pid_file(tmp_path).write_text("不是数字", encoding="utf-8")
    assert laya.read_pid(tmp_path) is None


def test_is_alive_handles_missing_process() -> None:
    assert laya.is_alive(None) is False
    assert laya.is_alive(os.getpid()) is True
    # 一个几乎不可能存在的 pid
    assert laya.is_alive(999_999_999) is False


def test_status_reports_not_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(laya, "health", lambda port, timeout_s=2.0: (0, None))
    status = laya.status(tmp_path, 8090)
    assert status.running is False
    assert status.as_dict()["running"] is False


def test_status_reports_running_when_health_ok(tmp_path: Path, monkeypatch) -> None:
    laya.pid_file(tmp_path).write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(
        laya, "health", lambda port, timeout_s=2.0: (200, {"status": "OK", "model": "x"})
    )
    status = laya.status(tmp_path, 8090)
    assert status.running is True
    assert status.pid == os.getpid()
    assert status.health == {"status": "OK", "model": "x"}


def test_status_reports_loading_when_process_alive_but_health_not_ready(
    tmp_path: Path, monkeypatch
) -> None:
    laya.pid_file(tmp_path).write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(laya, "health", lambda port, timeout_s=2.0: (503, None))
    status = laya.status(tmp_path, 8090)
    assert status.running is True
    assert "加载" in (status.error or "")


def test_stop_without_pidfile_is_noop(tmp_path: Path) -> None:
    assert laya.stop(tmp_path) is False


def test_stop_kills_recorded_process(tmp_path: Path) -> None:
    """用一个真的子进程验证停止逻辑, 不涉及模型."""
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    laya.pid_file(tmp_path).write_text(str(process.pid), encoding="utf-8")
    try:
        assert laya.stop(tmp_path, timeout_s=5.0) is True
        assert process.poll() is not None
        assert not laya.pid_file(tmp_path).exists()
    finally:
        process.kill()


def test_wait_until_ready_times_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(laya, "health", lambda port, timeout_s=2.0: (503, None))
    monkeypatch.setattr(laya.time, "sleep", lambda _s: None)
    ready, body = laya.wait_until_ready(8090, wait_s=0.01)
    assert ready is False and body is None


def test_wait_until_ready_returns_health(monkeypatch) -> None:
    monkeypatch.setattr(
        laya, "health", lambda port, timeout_s=2.0: (200, {"status": "OK"})
    )
    ready, body = laya.wait_until_ready(8090, wait_s=1.0)
    assert ready is True
    assert body == {"status": "OK"}
