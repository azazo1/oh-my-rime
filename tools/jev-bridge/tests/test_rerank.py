"""请求校验与 "概率 -> 顺序" 映射规则."""

from __future__ import annotations

import pytest
from conftest import make_request

from jev_bridge.backends import build_backend
from jev_bridge.cache import DiskCache
from jev_bridge.config import Config
from jev_bridge.rerank import RequestError, Reranker, decide, derive_confidence, validate_request


def test_validate_rejects_bad_requests() -> None:
    with pytest.raises(RequestError):
        validate_request({"v": 2})
    with pytest.raises(RequestError):
        validate_request(make_request(["你"]) | {"candidates": []})
    with pytest.raises(RequestError):
        validate_request(make_request(["你"]) | {"question": {"type": "score"}})
    with pytest.raises(RequestError):
        validate_request(make_request(["你"]) | {"mode": "turbo"})
    bad = make_request(["你"])
    bad.pop("ts")
    with pytest.raises(RequestError):
        validate_request(bad)


def test_derive_confidence_matches_docs_formula() -> None:
    assert derive_confidence([1.0]) == 1.0
    assert derive_confidence([0.5, 0.5]) == 0.0
    assert derive_confidence([0.9, 0.05, 0.05]) == pytest.approx(0.85)


def test_decide_reorders_when_confident() -> None:
    order, scores, confidence, changed = decide(
        [0, 1, 2], {"0": 0.05, "1": 0.9, "2": 0.05}, None, 0.5, 0.34
    )
    assert order == [1, 0, 2]
    assert changed is True
    assert confidence == pytest.approx(0.85)
    assert scores["1"] == pytest.approx(0.9)


def test_decide_keeps_order_when_flat() -> None:
    order, _, confidence, changed = decide(
        [0, 1, 2], {"0": 0.34, "1": 0.33, "2": 0.33}, None, 0.5, 0.34
    )
    assert order == [0, 1, 2]
    assert changed is False
    assert confidence < 0.5


def test_decide_top_probability_floor() -> None:
    order, _, _, changed = decide([0, 1], {"0": 0.4, "1": 0.6}, 0.99, 0.5, 0.34)
    assert changed is True
    assert order == [1, 0]
    # 归一化后首位概率 0.667 低于 0.9 的下限时保持原顺序
    order, _, _, changed = decide([0, 1], {"0": 0.1, "1": 0.2}, 0.99, 0.5, 0.9)
    assert changed is False
    assert order == [0, 1]


def test_decide_normalizes_and_ignores_unknown() -> None:
    order, scores, _, _ = decide([0, 1], {"0": 2.0, "1": 2.0, "9": 5.0}, 0.9, 0.5, 0.0)
    assert scores == {"0": 0.5, "1": 0.5}
    assert order == [0, 1]
    # 缺失的候选按 0 处理
    order, scores, _, _ = decide([0, 1, 2], {"1": 1.0}, 0.9, 0.5, 0.0)
    assert scores == {"0": 0.0, "1": 1.0, "2": 0.0}
    assert order == [1, 0, 2]


def test_handle_uses_mock_probabilities_and_caches(reranker: Reranker) -> None:
    request = make_request(["你好", "尼豪", "拟好"], mock_probabilities={"0": 0.1, "1": 0.1, "2": 0.8})
    first = reranker.handle(request)
    assert first["ok"] is True
    assert first["order"] == [2, 0, 1]
    assert first["cached"] is False
    assert first["badge"] == "AI"

    second = reranker.handle(make_request(["你好", "尼豪", "拟好"], request_id="test0002"))
    assert second["cached"] is True
    assert second["order"] == [2, 0, 1]
    assert second["id"] == "test0002"


def test_handle_backend_error_returns_not_ok(reranker: Reranker) -> None:
    response = reranker.handle(make_request(["你好", "尼豪"], mock_error="backend_http_500"))
    assert response["ok"] is False
    assert response["error"] == "backend_http_500"
    assert response["order"] == []


def test_handle_rejects_unsupported_version(reranker: Reranker) -> None:
    response = reranker.handle(make_request(["你好"]) | {"v": 99})
    assert response["ok"] is False
    assert response["error"] == "unsupported_version"


def test_handle_trusts_client_cache_key(config: Config, cache: DiskCache) -> None:
    """Lua 与 sidecar 的键不一致时以 Lua 的键为准, 保证 Lua 直读缓存仍然有效."""
    reranker = Reranker(config, cache, build_backend(config))
    request = make_request(["你好", "尼豪"], cache_key="deadbeefdeadbeef")
    response = reranker.handle(request)
    assert response["cache_key"] == "deadbeefdeadbeef"
    assert cache.path_for("deadbeefdeadbeef").exists()


def test_handle_badge_with_confidence(reranker: Reranker) -> None:
    request = make_request(
        ["你好", "尼豪"], mock_probabilities={"0": 0.1, "1": 0.9}, show_confidence=True
    )
    response = reranker.handle(request)
    assert response["badge"].startswith("AI ")
    assert response["badge"].endswith("%")
