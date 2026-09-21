"""磁盘缓存: TTL, 损坏文件, 淘汰."""

from __future__ import annotations

import json
import time
from pathlib import Path

from jev_bridge.cache import DiskCache


def test_put_get_roundtrip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60, max_entries=10)
    cache.put("abc", {"order": [1, 0]})
    payload = cache.get("abc")
    assert payload is not None
    assert payload["order"] == [1, 0]
    assert payload["expires_at"] > int(time.time())
    assert cache.stats.hits == 1


def test_expired_entry_is_removed(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60, max_entries=10)
    path = cache.path_for("old")
    path.write_text(json.dumps({"order": [], "expires_at": int(time.time()) - 5}), encoding="utf-8")
    assert cache.get("old") is None
    assert not path.exists()
    assert cache.stats.expired == 1


def test_corrupt_entry_is_removed(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60, max_entries=10)
    path = cache.path_for("broken")
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert cache.get("broken") is None
    assert not path.exists()
    assert cache.stats.errors == 1


def test_eviction_keeps_newest(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=600, max_entries=3)
    for index in range(6):
        cache.put(f"k{index}", {"order": [index]})
        time.sleep(0.01)
    remaining = sorted(path.stem for path in tmp_path.glob("*.json"))
    assert len(remaining) == 3
    assert remaining == ["k3", "k4", "k5"]
    assert cache.stats.evicted == 3
