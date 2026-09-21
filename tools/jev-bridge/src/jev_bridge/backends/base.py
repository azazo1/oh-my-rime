"""后端接口定义."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ScoreResult:
    """一次打分调用的结果; 失败时 ok=False 且 error 给出错误码."""

    ok: bool
    backend: str
    model: str = ""
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    latency_ms: float = 0.0
    error: str | None = None
    raw: dict[str, Any] | None = None


class Backend(Protocol):
    name: str

    def score(self, state: Any, question: dict, model: str) -> ScoreResult:
        """对 state 提出一个 choice 问题, 返回各选项概率."""

    def raw_call(self, body: dict) -> dict:
        """原样转发 /v1/systemone 请求体, 仅 http 后端支持."""
