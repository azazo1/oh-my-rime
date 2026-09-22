"""命令行解析本身也要有测试: 之前的 `--repo` 默认值被同名局部变量遮蔽过, 一跑 CLI 就崩."""

from __future__ import annotations

import pytest

from jev_bridge.__main__ import build_parser
from jev_bridge import laya


def test_parser_builds_and_lists_commands() -> None:
    parser = build_parser()
    actions = [action.dest for action in parser._actions]
    assert "command" in actions
    subparsers = next(a for a in parser._actions if a.dest == "command")
    names = set(subparsers.choices)
    for expected in (
        "serve",
        "once",
        "simulate",
        "rerank",
        "bench",
        "health",
        "paths",
        "logs",
        "deploy-rime",
        "laya",
        "eval",
        "patch-config",
        "unpatch-config",
        "init-config",
        "show-config",
        "install-agent",
        "uninstall-agent",
        "version",
    ):
        assert expected in names, expected


def test_laya_defaults_survive_parsing() -> None:
    args = build_parser().parse_args(["laya", "--status"])
    assert args.func.__name__ == "cmd_laya"
    assert args.repo == laya.DEFAULT_REPO
    assert args.python == laya.DEFAULT_PYTHON
    assert args.status is True


def test_eval_defaults_survive_parsing() -> None:
    args = build_parser().parse_args(["eval", "--fresh", "--tag", "x"])
    assert args.func.__name__ == "cmd_eval"
    assert args.fresh is True
    assert args.tag == "x"
    assert args.cases.endswith("benchmarks/chinese_cases.json")


def test_patch_config_rime_dir_defaults_to_none() -> None:
    args = build_parser().parse_args(["patch-config"])
    assert args.rime_dir is None  # 由 paths.resolve_rime_user_dir 决定


@pytest.mark.parametrize("command", ["serve", "logs", "paths", "deploy-rime", "eval"])
def test_commands_without_extra_args(command: str) -> None:
    args = build_parser().parse_args([command])
    assert callable(args.func)
