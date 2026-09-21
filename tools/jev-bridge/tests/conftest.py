"""pytest 公共 fixture 与构造请求的助手."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jev_bridge.backends import build_backend
from jev_bridge.cache import DiskCache
from jev_bridge.config import Config
from jev_bridge.keys import PROMPT_VERSION, PROTOCOL_VERSION, cache_key
from jev_bridge.rerank import Reranker

TOOLS_DIR = Path(__file__).resolve().parents[1]
RIME_DIR = TOOLS_DIR.parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(
        backend="mock",
        runtime_dir=str(tmp_path / "runtime"),
        poll_interval_ms=5,
        prefetch_max_age_ms=1500,
    )
    cfg.validate()
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def cache(config: Config) -> DiskCache:
    return DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)


@pytest.fixture
def reranker(config: Config, cache: DiskCache) -> Reranker:
    return Reranker(config, cache, build_backend(config))


def make_request(
    candidates: list[str],
    *,
    code: str = "nihao",
    context: str = "今天天气不错",
    schema_id: str = "wanxiang",
    mode: str = "sync",
    request_id: str = "test0001",
    with_key: bool = True,
    prompt_version: int = PROMPT_VERSION,
    **overrides,
) -> dict:
    request = {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ts": time.time(),
        "mode": mode,
        "schema_id": schema_id,
        "code": code,
        "context": context,
        "candidates": [
            {"index": index, "text": text, "type": "sentence"}
            for index, text in enumerate(candidates)
        ],
        "question": {
            "name": "best_continuation",
            "type": "choice",
            "instructions": "pick one",
            "criteria": {str(index): text for index, text in enumerate(candidates)},
        },
        "prompt_version": prompt_version,
    }
    if with_key:
        request["cache_key"] = cache_key(schema_id, code, context, candidates, prompt_version)
    request.update(overrides)
    return request


def write_request(config: Config, request: dict, *, age_s: float = 0.0) -> Path:
    path = config.queue_path / f"{request['id']}.req.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        import os

        os.utime(path, (old, old))
    return path


def read_response(config: Config, request_id: str) -> dict:
    path = config.queue_path / f"{request_id}.res.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run_lua_suite(tmp_path: Path) -> subprocess.CompletedProcess:
    """用本机 lua 解释器跑 lua/jev 的纯逻辑测试 (不依赖 librime)."""
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("本机没有 lua 解释器, 跳过 Lua 侧测试")
    runner = Path(__file__).resolve().parent / "lua" / "run.lua"
    return subprocess.run(
        [lua, str(runner), str(RIME_DIR), str(tmp_path / "lua-suite")],
        capture_output=True,
        text=True,
        check=False,
    )


__all__ = [
    "FIXTURES",
    "RIME_DIR",
    "TOOLS_DIR",
    "make_request",
    "read_response",
    "run_lua_suite",
    "write_request",
    "sys",
]
