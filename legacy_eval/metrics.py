"""Aggregate metrics for the legacy evaluation.

Each evaluation record is a flat dict with a documented shape; the functions
here turn lists of records into one summary dict per algorithm.  All metric
helpers are pure and depend only on ``tool_resource.metrics`` (reused, not
modified).
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from legacy_eval._bootstrap import ensure_paths

ensure_paths()

from tool_resource.metrics import ecdf_quantile, pinball_loss  # noqa: E402


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _pair_rows(records: Iterable[Mapping[str, Any]], *, actual_key: str, predicted_key: str) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for record in records:
        actual = _float_or_none(record.get(actual_key))
        predicted = _float_or_none(record.get(predicted_key))
        if actual is None or predicted is None:
            continue
        rows.append((actual, predicted))
    return rows


def _unavailable_counts(records: Sequence[Mapping[str, Any]], *, predicted_key: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        if record.get(predicted_key) is not None:
            continue
        reason = record.get("unavailable_reason")
        counts[str(reason or "unknown")] += 1
    return counts


def summarize_bucket(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summary for ``clause_latency_bucket`` records.

    Record fields: ``actual_bucket``, ``predicted_bucket`` (or None),
    ``probability_by_bucket`` (list or None), ``actual_ms``.
    """

    total = len(records)
    predicted = [r for r in records if r.get("predicted_bucket") is not None]
    coverage = len(predicted) / total if total else 0.0
    accuracy = None
    if predicted:
        correct = sum(
            1
            for r in predicted
            if r.get("actual_bucket") == r.get("predicted_bucket")
        )
        accuracy = correct / len(predicted)
    brier = None
    if predicted:
        scores: list[float] = []
        for r in predicted:
            probs = r.get("probability_by_bucket")
            actual_bucket = r.get("actual_bucket")
            if not isinstance(probs, (list, tuple)) or not probs:
                continue
            if isinstance(actual_bucket, bool) or not isinstance(actual_bucket, int):
                continue
            one_hot = [0.0] * len(probs)
            if 0 <= actual_bucket < len(probs):
                one_hot[actual_bucket] = 1.0
            scores.append(
                sum((float(p) - o) ** 2 for p, o in zip(probs, one_hot, strict=True))
            )
        if scores:
            brier = sum(scores) / len(scores)
    return {
        "n": total,
        "coverage": coverage,
        "accuracy": accuracy,
        "brier_score": brier,
        "unavailable_reasons": dict(_unavailable_counts(records, predicted_key="predicted_bucket")),
        "actual_bucket_distribution": dict(
            Counter(r.get("actual_bucket") for r in records)
        ),
    }


def summarize_point(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summary for lattice point-prediction records.

    Record fields: ``actual_ms``, ``predicted_ms`` (or None).
    """

    total = len(records)
    predicted = [r for r in records if r.get("predicted_ms") is not None]
    coverage = len(predicted) / total if total else 0.0
    rows = _pair_rows(predicted, actual_key="actual_ms", predicted_key="predicted_ms")
    result: dict[str, Any] = {
        "n": total,
        "coverage": coverage,
        "unavailable_reasons": dict(_unavailable_counts(records, predicted_key="predicted_ms")),
    }
    if rows:
        actuals = [a for a, _ in rows]
        predictions = [p for _, p in rows]
        errors = [abs(a - p) for a, p in rows]
        relative = [
            abs(a - p) / a if a > 0.0 else abs(a - p) for a, p in rows
        ]
        result.update(
            {
                "mae_ms": statistics.fmean(errors),
                "median_abs_error_ms": statistics.median(errors),
                "relative_error": statistics.fmean(relative),
                "pinball_05": statistics.fmean(
                    [pinball_loss(a, p, 0.5) for a, p in rows]
                ),
                "mean_predicted_ms": statistics.fmean(predictions),
                "mean_actual_ms": statistics.fmean(actuals),
                "predicted_p90_ms": ecdf_quantile(predictions, 0.9),
                "actual_p90_ms": ecdf_quantile(actuals, 0.9),
            }
        )
    return result


def summarize_quantile(
    records: Sequence[Mapping[str, Any]],
    *,
    quantile: float = 0.9,
) -> dict[str, Any]:
    """Summary for continuous p90 records.

    Record fields: ``actual``, ``predicted`` (or None).
    """

    total = len(records)
    usable = _pair_rows(records, actual_key="actual", predicted_key="predicted")
    coverage = len(usable) / total if total else 0.0
    result: dict[str, Any] = {
        "n": total,
        "coverage": coverage,
        "quantile": quantile,
        "unavailable_reasons": dict(_unavailable_counts(records, predicted_key="predicted")),
    }
    if usable:
        actuals = [a for a, _ in usable]
        predictions = [p for _, p in usable]
        result.update(
            {
                "pinball_q": statistics.fmean(
                    [pinball_loss(a, p, quantile) for a, p in usable]
                ),
                "mean_predicted": statistics.fmean(predictions),
                "mean_actual": statistics.fmean(actuals),
                "predicted_q": ecdf_quantile(predictions, quantile),
                "actual_q": ecdf_quantile(actuals, quantile),
            }
        )
    return result


__all__ = ["summarize_bucket", "summarize_point", "summarize_quantile"]
