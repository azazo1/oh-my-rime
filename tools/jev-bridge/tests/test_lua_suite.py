"""用本机 lua 解释器跑 lua/jev 的测试, 覆盖纯逻辑与文件队列传输."""

from __future__ import annotations

from pathlib import Path

from conftest import run_lua_suite


def test_lua_suite_passes(tmp_path: Path) -> None:
    result = run_lua_suite(tmp_path)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "FAIL" not in result.stdout
