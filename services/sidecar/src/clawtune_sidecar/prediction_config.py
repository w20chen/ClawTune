"""Configurable right-open load buckets. Units match the public protocol."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

DEFAULT_RESOURCE_BUCKETS = {
    "cpu_time_seconds": (0.01, 0.1, 1.0, 10.0, 60.0),
    "cpu_avg_cores": (0.1, 0.5, 1.0, 2.0, 4.0, 8.0),
    "cpu_peak_cores": (0.5, 1.0, 2.0, 4.0, 8.0, 16.0),
    "memory_total_peak_bytes": tuple(float(mib * 1024**2) for mib in (16, 64, 256, 1024, 4096)),
    "memory_extra_peak_bytes": tuple(float(mib * 1024**2) for mib in (16, 64, 256, 1024, 4096)),
}


def validate_edges(edges: Sequence[float]) -> tuple[float, ...]:
    if not edges or any(isinstance(x, bool) or not isinstance(x, (int, float))
                        or not math.isfinite(x) or x <= 0 for x in edges):
        raise ValueError("bucket edges must be nonempty, finite and positive")
    values = tuple(float(x) for x in edges)
    if any(a >= b for a, b in zip(values, values[1:])):
        raise ValueError("bucket edges must be strictly increasing")
    return values


def load_bucket_edges(duration_edges: Sequence[float],
                      resources: Mapping[str, Sequence[float]] | None = None) -> dict[str, tuple[float, ...]]:
    overrides = resources or {}
    if set(overrides) - set(DEFAULT_RESOURCE_BUCKETS):
        raise ValueError("unknown resource bucket target")
    return {"duration_ms": validate_edges(duration_edges), **{
        target: validate_edges(overrides.get(target, default))
        for target, default in DEFAULT_RESOURCE_BUCKETS.items()
    }}
