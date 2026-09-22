"""小样本评测: 在给定 case 集上量首选率, 用来对比不同 checkpoint / 提示词.

指标口径:
- baseline: 原顺序的首位候选就是期望词 (即"不重排也对"的比例);
- model: 模型打分最高的候选是期望词 (只看概率, 不看门限);
- applied: 经过 sidecar 的顺序映射后 (含置信度门限) 首位是期望词;
- 另外记录改对 / 改错的条数, 用来判断重排到底在帮忙还是帮倒忙.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .keys import PROMPT_VERSION, PROTOCOL_VERSION, cache_key
from .rerank import Reranker

# 与 lua/jev/defaults.lua 的 M.instructions 必须一致; 由 tests/fixtures/shared_prompt.json
# 两侧共同断言, 改一处而忘了另一处会直接测试失败.
DEFAULT_INSTRUCTIONS = (
    "The user is typing Chinese, and the text before the cursor is given as context. "
    "Choose which candidate the user most likely intends next. "
    "Prefer the candidate that reads naturally after the context "
    "and that accounts for the whole typed code rather than only its beginning; "
    "the code is a keyboard string and pinyin_hint is a best-effort expansion of it, "
    "so neither of them is reliable evidence on its own."
)


@dataclass
class CaseResult:
    name: str
    expected: str
    candidates: list[str]
    baseline_ok: bool
    model_ok: bool
    applied_ok: bool
    confidence: float
    latency_ms: float
    scores: dict[str, float] = field(default_factory=dict)
    skipped: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "expected": self.expected,
            "baseline_ok": self.baseline_ok,
            "model_ok": self.model_ok,
            "applied_ok": self.applied_ok,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "skipped": self.skipped,
        }


def load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path} 里没有 cases")
    for case in cases:
        for key in ("context", "code", "candidates", "expected"):
            if key not in case:
                raise ValueError(f"case 缺字段 {key}: {case}")
        if case["expected"] not in case["candidates"]:
            raise ValueError(f"expected 不在候选里: {case['name']}")
    return cases


def build_request(case: dict, index: int, instructions: str, schema_id: str = "wanxiang") -> dict:
    candidates = [{"index": position, "text": text} for position, text in enumerate(case["candidates"])]
    return {
        "v": PROTOCOL_VERSION,
        "id": f"eval{index:04d}",
        "ts": time.time(),
        "mode": "prefetch",
        "schema_id": schema_id,
        "code": case["code"],
        "context": case["context"],
        "candidates": candidates,
        "question": {
            "name": "best_continuation",
            "type": "choice",
            "instructions": instructions,
            "criteria": {str(position): text for position, text in enumerate(case["candidates"])},
        },
        "prompt_version": PROMPT_VERSION,
        "cache_key": cache_key(
            schema_id, case["code"], case["context"], case["candidates"], PROMPT_VERSION
        ),
    }


def run_case(reranker: Reranker, case: dict, index: int, instructions: str) -> CaseResult:
    request = build_request(case, index, instructions)
    started = time.perf_counter()
    response = reranker.handle(request)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    candidates = case["candidates"]
    expected = case["expected"]
    baseline_ok = candidates[0] == expected
    if not response.get("ok"):
        return CaseResult(
            name=case.get("name", f"case-{index}"),
            expected=expected,
            candidates=candidates,
            baseline_ok=baseline_ok,
            model_ok=False,
            applied_ok=baseline_ok,
            confidence=0.0,
            latency_ms=latency_ms,
            skipped=response.get("error") or "error",
        )
    scores = response["scores"]
    model_top = max(scores.items(), key=lambda item: (item[1], -int(item[0])))[0]
    model_ok = candidates[int(model_top)] == expected
    order = response["order"]
    applied_ok = candidates[order[0]] == expected if order else baseline_ok
    return CaseResult(
        name=case.get("name", f"case-{index}"),
        expected=expected,
        candidates=candidates,
        baseline_ok=baseline_ok,
        model_ok=model_ok,
        applied_ok=applied_ok,
        confidence=float(response.get("confidence") or 0.0),
        latency_ms=latency_ms,
        scores=scores,
    )


def summarize(results: list[CaseResult], tag: str = "") -> dict[str, Any]:
    total = len(results)
    if not total:
        return {"tag": tag, "cases": 0}
    skipped = [result for result in results if result.skipped]
    scored = [result for result in results if not result.skipped]
    latencies = sorted(result.latency_ms for result in scored)

    def _pct(count: int) -> float:
        return round(100 * count / len(scored), 1) if scored else 0.0

    def _pct_at(ratio: float) -> float:
        if not latencies:
            return 0.0
        return round(latencies[min(len(latencies) - 1, int(len(latencies) * ratio))], 1)

    fixes = sum(1 for r in scored if r.model_ok and not r.baseline_ok)
    breaks = sum(1 for r in scored if r.baseline_ok and not r.applied_ok)
    return {
        "tag": tag,
        "cases": total,
        "skipped": len(skipped),
        "baseline_top1_pct": _pct(sum(1 for r in scored if r.baseline_ok)),
        "model_top1_pct": _pct(sum(1 for r in scored if r.model_ok)),
        "applied_top1_pct": _pct(sum(1 for r in scored if r.applied_ok)),
        "fixed": fixes,
        "broken": breaks,
        "avg_confidence": round(
            sum(r.confidence for r in scored) / len(scored), 3
        )
        if scored
        else 0.0,
        "latency_p50_ms": _pct_at(0.5),
        "latency_p95_ms": _pct_at(0.95),
    }


def render_report(results: list[CaseResult], summary: dict, verbose: bool = True) -> str:
    lines = []
    if verbose:
        lines.append(f"{'case':<14}{'baseline':<10}{'model':<8}{'applied':<9}{'conf':<7}{'ms':<8}expected")
        for result in results:
            if result.skipped:
                lines.append(f"{result.name:<14}{'-':<10}{'skip:' + result.skipped}")
                continue
            mark = lambda ok: "ok" if ok else "--"  # noqa: E731
            lines.append(
                f"{result.name:<14}{mark(result.baseline_ok):<10}{mark(result.model_ok):<8}"
                f"{mark(result.applied_ok):<9}{result.confidence:<7.2f}"
                f"{result.latency_ms:<8.1f}{result.expected}"
            )
        lines.append("")
    lines.append(json.dumps(summary, ensure_ascii=False))
    return "\n".join(lines)
