"""Metrics for empirical resource forecasts."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def ecdf_quantile(values: Sequence[float], quantile: float) -> float:
    """Return the type-1 inverse-ECDF quantile."""

    samples = _finite_vector(values, "values")
    _validate_quantile(quantile)
    ordered = np.sort(samples)
    index = math.ceil(quantile * len(ordered)) - 1
    return float(ordered[index])


def pinball_loss(observation: float, prediction: float, quantile: float) -> float:
    """Return quantile loss for one observation and prediction."""

    _validate_quantile(quantile)
    if not math.isfinite(observation) or not math.isfinite(prediction):
        raise ValueError("observation and prediction must be finite")
    error = observation - prediction
    return max(quantile * error, (quantile - 1.0) * error)


def empirical_crps(values: Sequence[float], observation: float) -> float:
    """Exact CRPS of an empirical distribution in O(n log n)."""

    samples = np.sort(_finite_vector(values, "values"))
    if not math.isfinite(observation):
        raise ValueError("observation must be finite")
    n = len(samples)
    weights = 2.0 * np.arange(1, n + 1, dtype=float) - n - 1.0
    pairwise_half_mean = float(np.dot(weights, samples) / (n * n))
    return float(np.mean(np.abs(samples - observation))) - pairwise_half_mean


def interval_coverage(
    observations: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
) -> float:
    """Fraction of observations inside inclusive forecast intervals."""

    observed = _finite_vector(observations, "observations")
    lows = _finite_vector(lower, "lower")
    highs = _finite_vector(upper, "upper")
    if observed.shape != lows.shape or observed.shape != highs.shape:
        raise ValueError("observations and interval bounds must have equal length")
    if np.any(lows > highs):
        raise ValueError("lower interval bounds must not exceed upper bounds")
    return float(np.mean((lows <= observed) & (observed <= highs)))


def _finite_vector(values: Sequence[float], name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or array.size == 0:
        raise ValueError(f"{name} must be a non-empty one-dimensional sequence")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite values")
    return array


def _validate_quantile(quantile: float) -> None:
    if not math.isfinite(quantile) or not 0.0 < quantile < 1.0:
        raise ValueError(f"quantile must lie in (0, 1), got {quantile}")


__all__ = [
    "ecdf_quantile",
    "empirical_crps",
    "interval_coverage",
    "pinball_loss",
]
