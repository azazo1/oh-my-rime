"""mock 后端: 零依赖, 用确定性规则造分, 只用于验证链路.

规则 (与模型无关, 只为让重排肉眼可见):
- 候选字与上文有重叠 -> 按重叠比例加分
- 候选首字等于上文末字 -> 大幅加分 (像顺着上文往下写)
- 多字候选 -> 小幅加分; 越靠后的候选 -> 小幅减分

测试注入: state 里带 mock_probabilities 时直接采用; 带 mock_error 时按错误返回.
"""

from __future__ import annotations

import time
from typing import Any

from .base import ScoreResult


class MockBackend:
    name = "mock"

    def score(self, state: Any, question: dict, model: str) -> ScoreResult:
        started = time.perf_counter()
        model_name = model or "mock-1"
        started_ms = lambda: round((time.perf_counter() - started) * 1000, 3)  # noqa: E731

        if isinstance(state, dict) and state.get("mock_error"):
            return ScoreResult(
                ok=False,
                backend=self.name,
                model=model_name,
                latency_ms=started_ms(),
                error=str(state["mock_error"]),
            )

        if isinstance(state, dict) and isinstance(state.get("mock_probabilities"), dict):
            probabilities = {
                str(key): float(value)
                for key, value in state["mock_probabilities"].items()
            }
            return ScoreResult(
                ok=True,
                backend=self.name,
                model=model_name,
                probabilities=probabilities,
                confidence=None,
                latency_ms=started_ms(),
            )

        context = ""
        candidates: list[dict] = []
        if isinstance(state, dict):
            context = str(state.get("context") or "")
            candidates = list(state.get("candidates") or [])
        elif isinstance(state, list):
            candidates = [item for item in state if isinstance(item, dict)]

        context_chars = {char for char in context}
        last_context_char = context[-1] if context else ""
        raw: dict[str, float] = {}
        for position, candidate in enumerate(candidates):
            text = str(candidate.get("text") or "")
            key = str(candidate.get("index", position))
            score = 0.2
            if text:
                hit = sum(1 for char in text if char in context_chars)
                score += hit / len(text)
                if text[0] == last_context_char:
                    score += 3.0
            if len(text) > 1:
                score += 0.2
            score -= 0.02 * position
            raw[key] = max(score, 0.01)

        total = sum(raw.values())
        probabilities = {key: value / total for key, value in raw.items()} if total else {}
        return ScoreResult(
            ok=True,
            backend=self.name,
            model=model_name,
            probabilities=probabilities,
            confidence=None,
            latency_ms=started_ms(),
        )

    def raw_call(self, body: dict) -> dict:
        state = body.get("state")
        questions = body.get("questions") or {}
        answers: dict[str, Any] = {}
        for name, question in questions.items():
            result = self.score(state, question, body.get("model", ""))
            if not result.ok:
                answers[name] = {"type": question.get("type"), "error": result.error}
                continue
            best = max(result.probabilities.items(), key=lambda kv: kv[1], default=("", 0.0))
            answers[name] = {
                "type": question.get("type", "choice"),
                "choice": best[0],
                "probabilities": result.probabilities,
                "confidence": result.confidence,
            }
        return {"model": "mock-1", "answers": answers}
