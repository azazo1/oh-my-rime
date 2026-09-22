"""文件队列: 同键合并, 过期预取丢弃, 非法请求处理, 孤儿文件清理."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from conftest import make_request, read_response, write_request

from jev_bridge.backends.base import ScoreResult
from jev_bridge.cache import DiskCache
from jev_bridge.config import Config
from jev_bridge.queue import QueueWorker
from jev_bridge.rerank import Reranker


class CountingBackend:
    """包一层 mock 后端, 统计真实调用次数, 用来验证同键合并."""

    name = "counting"

    def __init__(self) -> None:
        from jev_bridge.backends.mock import MockBackend

        self.inner = MockBackend()
        self.calls = 0

    def score(self, state, question, model) -> ScoreResult:
        self.calls += 1
        result = self.inner.score(state, question, model)
        result.backend = self.name
        return result

    def raw_call(self, body: dict) -> dict:
        return self.inner.raw_call(body)


def build_worker(config: Config) -> tuple[QueueWorker, CountingBackend]:
    backend = CountingBackend()
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    worker = QueueWorker(config, Reranker(config, cache, backend))
    return worker, backend


def test_tick_processes_and_coalesces(config: Config) -> None:
    worker, backend = build_worker(config)
    write_request(config, make_request(["你好", "尼豪"], request_id="a1"))
    write_request(config, make_request(["你好", "尼豪"], request_id="a2"))
    processed = worker.tick()
    assert processed == 1
    assert backend.calls == 1
    assert worker.stats.coalesced == 1
    for request_id in ("a1", "a2"):
        response = read_response(config, request_id)
        assert response["ok"] is True
        assert response["order"] == [0, 1]
        assert response["id"] == request_id
    # 请求文件处理完即删, 响应文件留给 Lua 来取
    assert not list(config.queue_path.glob("*.req.json"))


def test_stale_prefetch_is_dropped(config: Config) -> None:
    worker, backend = build_worker(config)
    write_request(config, make_request(["你好", "尼豪"], mode="prefetch", request_id="late"), age_s=5)
    assert worker.tick() == 0
    assert backend.calls == 0
    assert worker.stats.dropped_stale == 1
    assert not list(config.queue_path.glob("*.req.json"))
    assert not list(config.queue_path.glob("*.res.json"))


def test_fresh_prefetch_waits_for_the_quiet_period(config: Config) -> None:
    """真 debounce: 预取请求要安静 prefetch_debounce_ms 之后才打分, 期间会被更新的请求取代."""
    worker, backend = build_worker(config)
    write_request(config, make_request(["你好", "尼豪"], mode="prefetch", request_id="fresh"))
    assert worker.tick() == 0, "刚写入的预取不该立刻打分"
    assert backend.calls == 0
    assert worker.stats.waited_debounce == 1
    # 请求文件要留着, 等安静期过去
    assert len(list(config.queue_path.glob("*.req.json"))) == 1

    # 安静期过去之后才处理
    path = next(config.queue_path.glob("*.req.json"))
    age = (config.prefetch_debounce_ms + 50) / 1000
    os.utime(path, (time.time() - age, time.time() - age))
    assert worker.tick() == 1
    assert backend.calls == 1
    assert worker.stats.waited_debounce == 1

    # 同步请求不受安静期影响 (同样的候选会命中上一步写下的缓存, 所以后端调用数不变)
    write_request(config, make_request(["你好", "尼豪"], mode="sync", request_id="sync-now"))
    assert worker.tick() == 1
    assert worker.stats.processed == 2
    assert backend.calls == 1


def test_invalid_request_is_dropped(config: Config) -> None:
    worker, backend = build_worker(config)
    broken = make_request(["你好"])
    broken["candidates"] = []
    write_request(config, broken)
    assert worker.tick() == 0
    assert backend.calls == 0
    assert worker.stats.dropped_invalid == 1


def test_unparsable_file_is_removed(config: Config) -> None:
    worker, _ = build_worker(config)
    path = config.queue_path / "junk.req.json"
    path.write_text("这不是 JSON", encoding="utf-8")
    assert worker.tick() == 0
    assert not path.exists()


def test_orphan_files_are_cleaned(config: Config) -> None:
    worker, _ = build_worker(config)
    orphan = config.queue_path / "old.res.json"
    orphan.write_text(json.dumps({"ok": True}), encoding="utf-8")
    old = time.time() - 120
    import os

    os.utime(orphan, (old, old))
    worker.tick()
    assert not orphan.exists()
    assert worker.stats.cleaned == 1


def test_tick_writes_heartbeat(config: Config) -> None:
    """心跳是 Rime 侧判断 sidecar 是否在线的唯一依据."""
    worker, _ = build_worker(config)
    heartbeat = config.queue_path / "heartbeat"
    assert not heartbeat.exists()
    worker.tick()
    assert heartbeat.exists()
    stamp = int(heartbeat.read_text(encoding="utf-8"))
    assert abs(time.time() - stamp) < 5


def test_pending_counts_requests(config: Config) -> None:
    worker, _ = build_worker(config)
    write_request(config, make_request(["你好", "尼豪"]))
    assert worker.pending() == 1


def test_response_file_is_parseable_by_lua_reader(config: Config) -> None:
    """响应必须是紧凑 JSON 且字段齐全, Lua 侧解析器只认这个子集."""
    worker, _ = build_worker(config)
    write_request(config, make_request(["你好", "尼豪", "拟好"], request_id="shape"))
    worker.tick()
    response = read_response(config, "shape")
    for field in ("v", "id", "ok", "order", "scores", "confidence", "badge", "cache_key"):
        assert field in response
    assert isinstance(response["order"], list)
    assert isinstance(response["scores"], dict)
    assert isinstance(response["ok"], bool)
