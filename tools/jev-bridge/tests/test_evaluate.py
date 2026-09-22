"""评测脚本的聚合口径, 以及默认提示词与 Lua 侧的一致性."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURES

from jev_bridge.evaluate import (
    DEFAULT_INSTRUCTIONS,
    CaseResult,
    build_request,
    load_cases,
    render_report,
    summarize,
)

CASES = Path(__file__).resolve().parents[1] / "benchmarks" / "chinese_cases.json"


def _result(name: str, *, baseline: bool, model: bool, applied: bool, latency: float = 10.0):
    return CaseResult(
        name=name,
        expected="对",
        candidates=["对", "错"],
        baseline_ok=baseline,
        model_ok=model,
        applied_ok=applied,
        confidence=0.8,
        latency_ms=latency,
    )


def test_default_instructions_match_lua_fixture() -> None:
    fixture = json.loads((FIXTURES / "shared_prompt.json").read_text(encoding="utf-8"))
    assert DEFAULT_INSTRUCTIONS == fixture["instructions"]


def test_shipped_cases_are_valid() -> None:
    cases = load_cases(CASES)
    assert len(cases) >= 10
    # expected 必须真的在候选里, 且不能全部排在首位 (否则 baseline 就满分了)
    assert all(case["expected"] in case["candidates"] for case in cases)
    first_ok = sum(1 for case in cases if case["candidates"][0] == case["expected"])
    assert 0 < first_ok < len(cases)


def test_build_request_shape() -> None:
    case = {"context": "今天", "code": "tianqi", "candidates": ["天气", "天启"], "expected": "天气"}
    request = build_request(case, 3, DEFAULT_INSTRUCTIONS)
    assert request["id"] == "eval0003"
    assert request["mode"] == "prefetch"
    assert request["question"]["type"] == "choice"
    assert request["question"]["criteria"] == {"0": "天气", "1": "天启"}
    assert request["cache_key"]


def test_summarize_counts_fixes_and_breaks() -> None:
    results = [
        _result("a", baseline=True, model=True, applied=True),
        _result("b", baseline=False, model=True, applied=True),  # 改对
        _result("c", baseline=True, model=False, applied=False),  # 改错
        _result("d", baseline=False, model=False, applied=False),
    ]
    summary = summarize(results, tag="t")
    assert summary["cases"] == 4
    assert summary["baseline_top1_pct"] == 50.0
    assert summary["model_top1_pct"] == 50.0
    assert summary["applied_top1_pct"] == 50.0
    assert summary["fixed"] == 1
    assert summary["broken"] == 1


def test_summarize_handles_skipped() -> None:
    results = [
        _result("a", baseline=True, model=True, applied=True),
        CaseResult(
            name="b",
            expected="对",
            candidates=["对"],
            baseline_ok=True,
            model_ok=False,
            applied_ok=True,
            confidence=0.0,
            latency_ms=1.0,
            skipped="bad_response",
        ),
    ]
    summary = summarize(results)
    assert summary["skipped"] == 1
    assert summary["cases"] == 2
    assert summary["applied_top1_pct"] == 100.0


def test_load_cases_rejects_missing_expected(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [{"context": "c", "code": "k", "candidates": ["对"]}]}))
    with pytest.raises(ValueError):
        load_cases(path)


def test_render_report_is_readable() -> None:
    results = [_result("a", baseline=True, model=True, applied=True)]
    text = render_report(results, summarize(results, tag="english"))
    assert "case" in text and "baseline" in text
    assert '"tag": "english"' in text
