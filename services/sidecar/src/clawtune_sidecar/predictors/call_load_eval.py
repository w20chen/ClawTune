"""Read-only, call-level scoring for the versioned load protocol.

Evaluation does not train or calibrate a KB. Callers must supply causally held
out calls, compatible actual metrics, and label partial/censored data invalid.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from typing import Any

from clawtune_sidecar.contracts.load_prediction import CallLoadPrediction, TARGET_DEFINITIONS


def evaluate_calls(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    samples = {target: [] for target in TARGET_DEFINITIONS}
    actual_counts = dict.fromkeys(TARGET_DEFINITIONS, 0)
    total = 0
    for record in records:
        prediction = CallLoadPrediction.model_validate(record["prediction"])
        if record.get("scope") != "tool_call" or record.get("lifecycle") != prediction.lifecycle:
            raise ValueError("evaluation requires matching tool-call lifecycle")
        total += 1
        for target, definition in TARGET_DEFINITIONS.items():
            actual = record.get("actual", {}).get(target)
            if actual is None or actual.get("valid") is not True:
                continue
            if actual.get("metric_definition") != definition:
                raise ValueError(f"incompatible actual metric for {target}")
            value = actual.get("value")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("actual measurements must be finite and nonnegative")
            actual_counts[target] += 1
            estimate = prediction.targets[target]
            if estimate.status != "available":
                continue
            delta = value - estimate.p90
            observed_bucket = bisect_right(estimate.buckets.edges, value)
            probabilities = estimate.buckets.probabilities
            samples[target].append({
                "avg_absolute_error": abs(estimate.avg - value),
                "p50_absolute_error": abs(estimate.p50 - value),
                "p90_coverage": float(value <= estimate.p90),
                "p90_pinball_loss": .9 * delta if delta >= 0 else -.1 * delta,
                "bucket_brier_score": sum((p - float(i == observed_bucket)) ** 2
                                          for i, p in enumerate(probabilities)),
            })
    return {"call_count": total, "targets": {
        target: {"valid_actual_count": actual_counts[target], "predicted_count": len(rows),
                 "availability": len(rows) / actual_counts[target] if actual_counts[target] else None,
                 **{metric: statistics.mean(row[metric] for row in rows) if rows else None
                    for metric in ("avg_absolute_error", "p50_absolute_error", "p90_coverage",
                                   "p90_pinball_loss", "bucket_brier_score")}}
        for target, rows in samples.items()
    }}
