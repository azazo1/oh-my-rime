"""请求校验与 "概率 -> 候选顺序" 的映射规则.

规则 (与 README 中的契约保持一致):
- 只对请求里前 max_candidates 个候选生效, 其余保持原相对顺序接在后面;
- 排序键 (score 降序, 原 index 升序), 稳定;
- 仅当 confidence >= min_confidence 且 max(score) >= min_top_prob 时才改变顺序,
  否则 order 等于原始顺序 (scores 仍然返回, 便于观察与排障);
- 概率缺失按 0 处理, 未知 key 忽略并记日志, 总和重新归一化.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .cache import DiskCache
from .config import Config
from .keys import PROMPT_VERSION, PROTOCOL_VERSION, cache_key, normalize_text

log = logging.getLogger(__name__)

VALID_MODES = {"sync", "prefetch"}


class RequestError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def derive_confidence(probabilities: list[float]) -> float:
    """按官方文档的口径由概率分布推导 confidence: (n * peak - 1) / (n - 1)."""
    values = [value for value in probabilities if value >= 0]
    count = len(values)
    if count <= 1:
        return 1.0
    peak = max(values)
    return max(0.0, min(1.0, (count * peak - 1) / (count - 1)))


def validate_request(req: dict) -> None:
    if not isinstance(req, dict):
        raise RequestError("bad_request", "请求不是 JSON 对象")
    if req.get("v") != PROTOCOL_VERSION:
        raise RequestError("unsupported_version", f"v={req.get('v')!r}")
    candidates = req.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RequestError("no_candidates")
    for position, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or not str(candidate.get("text") or ""):
            raise RequestError("bad_candidate", f"第 {position} 个候选缺少 text")
    question = req.get("question")
    if not isinstance(question, dict) or question.get("type") != "choice":
        raise RequestError("bad_question", "目前只支持 choice 类型问题")
    mode = req.get("mode", "prefetch")
    if mode not in VALID_MODES:
        raise RequestError("bad_mode", f"mode={mode!r}")
    if not isinstance(req.get("ts"), (int, float)):
        raise RequestError("bad_request", "缺少 ts")


def candidate_indices(candidates: list[dict]) -> list[int]:
    indices: list[int] = []
    for position, candidate in enumerate(candidates):
        raw = candidate.get("index", position)
        try:
            indices.append(int(raw))
        except (TypeError, ValueError) as exc:
            raise RequestError("bad_candidate", f"index 非法: {raw!r}") from exc
    return indices


def decide(
    indices: list[int],
    probabilities: dict[str, float],
    confidence: float | None,
    min_confidence: float,
    min_top_prob: float,
) -> tuple[list[int], dict[str, float], float, bool]:
    """返回 (order, scores, confidence, changed)."""
    scores = {str(index): 0.0 for index in indices}
    unknown = [key for key in probabilities if key not in scores]
    if unknown:
        log.debug("后端返回了未知选项: %s", ",".join(sorted(unknown)[:8]))
    for key, value in probabilities.items():
        if key in scores:
            try:
                scores[key] = max(0.0, float(value))
            except (TypeError, ValueError):
                scores[key] = 0.0

    total = sum(scores.values())
    if total > 0:
        scores = {key: value / total for key, value in scores.items()}

    ranked = sorted(indices, key=lambda index: (-scores[str(index)], index))
    top_probability = scores[str(ranked[0])] if ranked else 0.0
    resolved_confidence = (
        round(float(confidence), 4)
        if isinstance(confidence, (int, float))
        else round(derive_confidence(list(scores.values())), 4)
    )
    changed = (
        len(ranked) > 1
        and resolved_confidence >= min_confidence
        and top_probability >= min_top_prob
        and ranked != indices
    )
    order = ranked if changed else list(indices)
    return order, {key: round(value, 4) for key, value in scores.items()}, resolved_confidence, changed


def render_badge(template: str, confidence: float, show_confidence: bool) -> str:
    text = template or ""
    if show_confidence:
        text = f"{text} {round(confidence * 100)}%".strip()
    return text


class Reranker:
    """把一次请求变成一次缓存查询 + (必要时) 一次后端调用."""

    def __init__(self, config: Config, cache: DiskCache, backend: Any):
        self.config = config
        self.cache = cache
        self.backend = backend
        self._key_mismatch_logged = False

    # ---------------------------------------------------------------- 请求处理

    def handle(self, req: dict) -> dict:
        try:
            validate_request(req)
        except RequestError as exc:
            log.warning("请求不合法 (%s): %s", exc.code, exc.detail)
            return self._error_response(req, exc.code)

        candidates = list(req["candidates"])[: self.config.max_candidates]
        indices = candidate_indices(candidates)
        texts = [normalize_text(str(candidate.get("text") or "")) for candidate in candidates]
        question = dict(req["question"])
        model = str(req.get("model") or self.config.model)

        computed_key = cache_key(
            str(req.get("schema_id") or ""),
            str(req.get("code") or ""),
            str(req.get("context") or ""),
            texts,
            int(req.get("prompt_version") or PROMPT_VERSION),
        )
        provided_key = str(req.get("cache_key") or "")
        if provided_key and provided_key != computed_key and not self._key_mismatch_logged:
            self._key_mismatch_logged = True
            log.warning(
                "缓存键不一致 (Lua 与 sidecar 的归一化规则可能已分叉), 本次以 Lua 的键为准: "
                "lua=%s sidecar=%s",
                provided_key,
                computed_key,
            )
        key = provided_key or computed_key

        cached = self.cache.get(key)
        if cached is not None:
            return self._assemble(req, key, cached, cached_flag=True, latency_ms=0.0)

        state = {
            "context": str(req.get("context") or ""),
            "input": str(req.get("code") or ""),
            "candidates": [
                {"id": str(index), "text": text} for index, text in zip(indices, texts)
            ],
        }
        state.update(self._state_hooks(req))
        result = self.backend.score(state, question, model)
        if not result.ok:
            log.warning(
                "后端打分失败 (%s): code=%s candidates=%d", result.error, req.get("code"), len(indices)
            )
            return self._error_response(req, result.error or "backend_error", key=key)

        order, scores, confidence, changed = decide(
            indices,
            result.probabilities,
            result.confidence,
            self.config.min_confidence,
            self.config.min_top_prob,
        )
        badge = render_badge(
            str(req.get("badge") if req.get("badge") is not None else self.config.badge),
            confidence,
            bool(
                req.get("show_confidence")
                if req.get("show_confidence") is not None
                else self.config.show_confidence
            ),
        )
        payload = {
            # ok 必须写进缓存文件: Rime 侧直接读这个文件, 用它判断"这是一条可用结果".
            # 之前只写 HTTP 响应而漏了这里, 结果所有缓存命中都被过滤器当成错误丢掉.
            "ok": True,
            "v": PROTOCOL_VERSION,
            "prompt_version": int(req.get("prompt_version") or PROMPT_VERSION),
            "order": order,
            "scores": scores,
            "confidence": confidence,
            "order_changed": changed,
            "badge": badge,
            "backend": result.backend,
            "model": result.model,
            "cache_key": key,
            "expires_at": int(time.time()) + self.config.cache_ttl_s,
        }
        self.cache.put(key, payload)
        log.info(
            "打分完成 backend=%s model=%s code=%s 候选=%d 置信=%.2f 顺序变化=%s 延迟=%.1fms",
            result.backend,
            result.model,
            req.get("code"),
            len(indices),
            confidence,
            changed,
            result.latency_ms,
        )
        return self._assemble(req, key, payload, cached_flag=False, latency_ms=result.latency_ms)

    @staticmethod
    def _state_hooks(req: dict) -> dict:
        """调试/测试注入: state_extra 以及顶层 mock_probabilities / mock_error 并入 state.

        线上 (Rime 侧) 不会带这些字段; mock 后端认得它们, 用于在不依赖模型的情况下
        验证 "概率 -> 顺序" 的整条链路.
        """
        hooks: dict = {}
        extra = req.get("state_extra")
        if isinstance(extra, dict):
            hooks.update(extra)
        for name in ("mock_probabilities", "mock_error"):
            if name in req:
                hooks[name] = req[name]
        return hooks

    # ---------------------------------------------------------------- 响应组装

    def _assemble(
        self, req: dict, key: str, payload: dict, cached_flag: bool, latency_ms: float
    ) -> dict:
        return {
            "v": PROTOCOL_VERSION,
            "id": str(req.get("id") or ""),
            "ok": True,
            "cached": cached_flag,
            "backend": payload.get("backend", ""),
            "model": payload.get("model", ""),
            "latency_ms": latency_ms,
            "expires_at": int(payload.get("expires_at", 0)),
            "order": list(payload.get("order") or []),
            "scores": dict(payload.get("scores") or {}),
            "confidence": payload.get("confidence", 0.0),
            "order_changed": bool(payload.get("order_changed", False)),
            "badge": str(payload.get("badge") or ""),
            "cache_key": key,
            "error": None,
        }

    def _error_response(self, req: dict, error: str, key: str = "") -> dict:
        return {
            "v": PROTOCOL_VERSION,
            "id": str((req or {}).get("id") or ""),
            "ok": False,
            "cached": False,
            "backend": "",
            "model": "",
            "latency_ms": 0.0,
            "expires_at": 0,
            "order": [],
            "scores": {},
            "confidence": 0.0,
            "order_changed": False,
            "badge": "",
            "cache_key": key,
            "error": error,
        }
