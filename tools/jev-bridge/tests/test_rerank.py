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


def test_pinyin_hint_goes_into_state(config: Config, cache: DiskCache) -> None:
    """双拼展开结果要传给后端, 且只在请求带了它的时候."""
    captured: dict = {}

    class CaptureBackend:
        name = "capture"

        def score(self, state, question, model):
            captured["state"] = state
            return build_backend(config).score(state, question, model)

        def raw_call(self, body: dict) -> dict:
            return {}

    reranker = Reranker(config, cache, CaptureBackend())
    reranker.handle(make_request(["拟好", "你好"], pinyin_hint="ni hao"))
    assert captured["state"]["pinyin_hint"] == "ni hao"
    assert captured["state"]["input"] == "nihao"

    captured.clear()
    reranker.handle(make_request(["大家", "打架"], code="dajia", request_id="t2"))
    assert "pinyin_hint" not in captured["state"]


def test_full_code_candidates_are_not_pushed_down(config: Config, cache: DiskCache) -> None:
    """实测模型偏爱短词: 输入 3 个音节时它会把「回家」提到「回家了」前面, 这里必须挡住."""
    from jev_bridge.backends import build_backend as _build

    class FixedBackend:
        name = "fixed"

        def __init__(self, probabilities):
            self.probabilities = probabilities

        def score(self, state, question, model):
            result = _build(config).score(state, question, model)
            result.probabilities = self.probabilities
            result.confidence = 0.9
            return result

        def raw_call(self, body: dict) -> dict:
            return {}

    # 模型给「回家」(index 1) 0.8, 「回家了」(index 0) 只有 0.1
    reranker = Reranker(
        config, cache, FixedBackend({"0": 0.1, "1": 0.8, "2": 0.1})
    )
    response = reranker.handle(
        make_request(
            ["回家了", "回家", "饹"],
            code="hvjxle",
            syllables=3,
            request_id="cov1",
        )
    )
    assert response["order"][0] == 0, "覆盖整串编码的候选不该被短候选挤下去"
    assert response["order"] == [0, 1, 2]

    # 关掉这条规则时回到纯概率排序 (换一组候选, 避免命中上一条的缓存)
    reranker2 = Reranker(config, cache, FixedBackend({"0": 0.1, "1": 0.8, "2": 0.1}))
    response2 = reranker2.handle(
        make_request(
            ["回家了呀", "回家", "饹"],
            code="hvjxle",
            syllables=3,
            respect_full_code=False,
            request_id="cov2",
        )
    )
    assert response2["order"][0] == 1, "关掉规则后应当按概率排"


def test_coverage_grouping_rules() -> None:
    from jev_bridge.rerank import group_by_coverage

    lengths = {0: 3, 1: 2, 2: 1, 3: 3}
    full, partial = group_by_coverage([0, 1, 2, 3], lengths, 3)
    assert full == [0, 3]
    assert partial == [1, 2]
    # 全都覆盖或全都不覆盖时不分组 (规则自然失效)
    assert group_by_coverage([0, 3], lengths, 3) == ([0, 3], [])
    assert group_by_coverage([1, 2], lengths, 3) == ([1, 2], [])
    assert group_by_coverage([0, 1, 2, 3], lengths, 1) == ([0, 1, 2, 3], [])


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
