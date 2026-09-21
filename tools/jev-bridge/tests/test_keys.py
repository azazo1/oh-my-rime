"""缓存键与归一化规则的测试, 并与 Lua 侧共享同一份向量."""

from __future__ import annotations

import json

from conftest import FIXTURES
from jev_bridge.keys import (
    cache_key,
    fnv1a64_hex,
    key_source,
    normalize_code,
    normalize_context,
    tail_chars,
)


def test_fnv1a64_known_vectors() -> None:
    # 官方 FNV-1a 64 位的两个标准向量
    assert fnv1a64_hex("") == "cbf29ce484222325"
    assert fnv1a64_hex("a") == "af63dc4c8601ec8c"


def test_fnv1a64_utf8_bytes() -> None:
    # 中文按 UTF-8 字节哈希, 结果必须与 Lua 侧一致
    assert fnv1a64_hex("你好") == fnv1a64_hex("你好")
    assert len(fnv1a64_hex("你好")) == 16


def test_normalize_rules() -> None:
    assert normalize_code(" Ni Hao\t") == "nihao"
    assert tail_chars("今天天气不错", 4) == "天气不错"
    assert normalize_context("  a\tb\nc  ", 10) == "a b c"
    # 先折叠空白再按码点取尾部, 最后去掉首尾空格
    assert normalize_context("前文\n后文\t", 4) == "后文"
    assert tail_chars("abc", 0) == ""


def test_key_source_layout() -> None:
    source = key_source("wanxiang", "Ni Hao", "上文", ["你", "好"])
    assert source.startswith("v1|wanxiang|nihao|上文|")
    assert source.endswith("你\x1f好")


def test_key_vectors_match_fixture() -> None:
    """两侧共享向量: 任何一侧改了归一化规则, 这个测试都会失败."""
    vectors = json.loads((FIXTURES / "key_vectors.json").read_text(encoding="utf-8"))
    assert vectors, "向量文件为空"
    for vector in vectors:
        expected = cache_key(
            vector["schema_id"],
            vector["code"],
            vector["context"],
            vector["candidates"],
            vector["prompt_version"],
        )
        assert expected == vector["cache_key"], vector["name"]
