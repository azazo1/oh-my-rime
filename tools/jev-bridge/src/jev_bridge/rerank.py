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


def group_by_coverage(
    indices: list[int],
    lengths: dict[int, int],
    syllables: int,
) -> tuple[list[int], list[int]]:
    """按"是否覆盖整串编码"分两组.

    实测模型偏爱更常见的短词: 输入 hvjxle (hui jia le) 时它会把「回家」提到「回家了」前面,
    但用户打满整串编码通常是想要整串. 所以把候选分成"覆盖整串"与"只覆盖一部分",
    让模型只在组内调序; syllables <= 1 时全部算覆盖, 规则自然失效.
    """
    if syllables <= 1:
        return list(indices), []
    full = [index for index in indices if lengths.get(index, 0) >= syllables]
    partial = [index for index in indices if lengths.get(index, 0) < syllables]
    if not full or not partial:
        return list(indices), []
    return full, partial


def decide(
    indices: list[int],
    probabilities: dict[str, float],
    confidence: float | None,
    min_confidence: float,
    min_top_prob: float,
    coverage: tuple[list[int], list[int]] | None = None,
    margin: float = 0.0,
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

    if coverage and coverage[1]:
        # 覆盖整串的候选永远排在只覆盖一部分的前面, 各自组内按概率排
        full, partial = coverage
        ranked = sorted(full, key=lambda index: (-scores[str(index)], index)) + sorted(
            partial, key=lambda index: (-scores[str(index)], index)
        )
    else:
        ranked = sorted(indices, key=lambda index: (-scores[str(index)], index))

    # 词库顺序自带词频信息: 模型想换掉原首位时, 领先幅度不够就不换
    # (应对"没有 vs 魅友"这类拼音完全相同、只能靠频率区分的候选)
    if margin > 0 and indices and ranked and ranked[0] != indices[0]:
        advantage = scores[str(ranked[0])] - scores[str(indices[0])]
        if advantage < margin:
            log.debug(
                "换首位被词频先验挡住: %s(%.3f) vs %s(%.3f), 需要领先 %.2f",
                ranked[0],
                scores[str(ranked[0])],
                indices[0],
                scores[str(indices[0])],
                margin,
            )
            ranked = list(indices)
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
        pinyin_hint = str(req.get("pinyin_hint") or "").strip()
        if pinyin_hint:
            # 双拼展开结果: 只是提示, 说明里已声明可能不准
            state["pinyin_hint"] = pinyin_hint
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
            coverage=self._coverage(req, indices, texts),
            margin=self.config.override_margin,
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
        # 日志必须区分"模型自己想选谁"和"最终生效的首位": 前者被门限/规则挡住时两者不同,
        # 只看生效首位会误以为模型判对了 (曾经因此误判过"没有 vs 魅友"的问题).
        model_top_text = ""
        if scores:
            model_top_key = max(scores, key=lambda item: scores[item])
            model_top_text = texts[indices.index(int(model_top_key))]
        applied_top_text = texts[indices.index(order[0])] if order else ""
        log.info(
            "打分完成 backend=%s model=%s code=%s 候选=%d 置信=%.2f 顺序变化=%s "
            "模型首选=%s(%.2f) 生效首位=%s%s 延迟=%.1fms",
            result.backend,
            result.model,
            req.get("code"),
            len(indices),
            confidence,
            changed,
            model_top_text,
            scores.get(model_top_key, 0.0) if scores else 0.0,
            applied_top_text,
            "" if applied_top_text == model_top_text else "(被门限或规则挡住)",
            result.latency_ms,
        )
        return self._assemble(req, key, payload, cached_flag=False, latency_ms=result.latency_ms)

    @staticmethod
    def _coverage(req: dict, indices: list[int], texts: list[str]) -> tuple[list[int], list[int]] | None:
        """按编码音节数把候选分成"覆盖整串"与"只覆盖一部分"; 关掉该规则时返回 None."""
        if not req.get("respect_full_code", True):
            return None
        try:
            syllables = int(req.get("syllables") or 0)
        except (TypeError, ValueError):
            return None
        if syllables <= 1:
            return None
        lengths = {index: len(text) for index, text in zip(indices, texts)}
        return group_by_coverage(indices, lengths, syllables)

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
