"""KV-TTL evaluation for right-open tool-runtime buckets.

The functions in this module are pure and independent of the predictor and
sidecar.  A caller only needs an observed (or reference) runtime, an initial
bucket prediction, finite bucket boundaries, and one TTL per bucket.  This
makes the same policy implementation reusable for offline dataset evaluation.

``bucket_boundaries_s = [0.1, 0.5, 2.0, 10.0]`` defines five buckets::

    [0, 0.1), [0.1, 0.5), [0.5, 2), [2, 10), [10, +inf)

TTL values are absolute times relative to tool start.  At a boundary, the new
bucket is selected before its TTL is evaluated.  Consequently, a TTL equal to
the old bucket's upper boundary does not evict the KV from the old bucket.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BucketTTLEvaluation:
    """Runtime bucket diagnostics and KV residency/miss costs for one call."""

    actual_time_s: float
    initial_bucket_index: int
    final_bucket_index: int
    num_bucket_jumps: int
    bucket_exhausted: bool

    # Kept for protocol compatibility: this is tau[k_0], not the dynamic D.
    ttl_s: float
    kv_eviction_time_s: float
    kv_retention_time_s: float
    kv_cache_miss: bool


def _validate_policy(
    initial_bucket_index: int,
    bucket_boundaries_s: Sequence[float],
    ttl_by_bucket_s: Sequence[float],
) -> None:
    if not bucket_boundaries_s:
        raise ValueError("bucket_boundaries_s must be non-empty")

    previous = 0.0
    for boundary in bucket_boundaries_s:
        if not math.isfinite(boundary) or boundary <= previous:
            raise ValueError(
                "bucket_boundaries_s must be finite, positive, and strictly increasing"
            )
        previous = boundary

    bucket_count = len(bucket_boundaries_s) + 1
    if len(ttl_by_bucket_s) != bucket_count:
        raise ValueError(
            "ttl_by_bucket_s must contain one value per bucket "
            f"({bucket_count} values for {len(bucket_boundaries_s)} boundaries)"
        )
    for ttl in ttl_by_bucket_s:
        if not math.isfinite(ttl) or ttl < 0.0:
            raise ValueError("ttl_by_bucket_s values must be finite and non-negative")
    if not 0 <= initial_bucket_index < bucket_count:
        raise ValueError("initial_bucket_index is out of range")


def eviction_time_for_bucket_ttl(
    initial_bucket_index: int,
    bucket_boundaries_s: Sequence[float],
    ttl_by_bucket_s: Sequence[float],
) -> float:
    """Return the dynamic KV eviction time ``D`` relative to tool start.

    This directly implements::

        k(t) = max(k_0, max{k | b_k <= t})
        D = inf{t >= 0 | tau[k(t)] == 0 or t >= tau[k(t)]}

    The finite boundaries are ``b_1, ..., b_K`` and the TTL sequence therefore
    has ``K + 1`` entries, including one for the open-ended final bucket.
    """

    _validate_policy(
        initial_bucket_index,
        bucket_boundaries_s,
        ttl_by_bucket_s,
    )

    bucket_count = len(ttl_by_bucket_s)
    for bucket_index in range(initial_bucket_index, bucket_count):
        interval_start = (
            0.0
            if bucket_index == initial_bucket_index
            else float(bucket_boundaries_s[bucket_index - 1])
        )
        interval_end = (
            float(bucket_boundaries_s[bucket_index])
            if bucket_index < len(bucket_boundaries_s)
            else math.inf
        )
        ttl = float(ttl_by_bucket_s[bucket_index])

        if ttl == 0.0:
            return interval_start

        candidate = max(interval_start, ttl)
        # The interval is right-open.  At equality, advance first and apply the
        # next bucket's TTL instead of evicting under the previous bucket.
        if candidate < interval_end:
            return candidate

    # The final interval is open-ended and all TTLs are finite, so the loop
    # necessarily returns.  Retain the guard to make that invariant explicit.
    raise AssertionError("open-ended final bucket did not produce an eviction time")


def evaluate_bucket_ttl(
    actual_time_s: float,
    initial_bucket_index: int,
    buckets: Sequence[float],
    ttl_by_bucket: Sequence[float],
) -> BucketTTLEvaluation:
    """Evaluate ``C_R=min(T,D)`` and ``C_M=1[T>D]`` for one runtime.

    ``buckets`` contains finite bucket boundaries and ``ttl_by_bucket`` has one
    entry per resulting bucket, including the open-ended final bucket. The
    result deliberately keeps residency and cache miss separate and makes no
    assumptions about KV size, transfer bandwidth, or recovery cost.
    """

    if not math.isfinite(actual_time_s) or actual_time_s < 0.0:
        raise ValueError("actual_time_s must be finite and non-negative")
    eviction_time_s = eviction_time_for_bucket_ttl(
        initial_bucket_index,
        buckets,
        ttl_by_bucket,
    )
    actual = float(actual_time_s)
    observed_bucket_index = bisect_right(buckets, actual)
    final_bucket_index = max(initial_bucket_index, observed_bucket_index)
    num_bucket_jumps = final_bucket_index - initial_bucket_index
    bucket_exhausted = observed_bucket_index == len(buckets)

    kv_retention_time_s = min(actual, eviction_time_s)
    kv_cache_miss = actual > eviction_time_s

    return BucketTTLEvaluation(
        actual_time_s=actual,
        initial_bucket_index=initial_bucket_index,
        final_bucket_index=final_bucket_index,
        num_bucket_jumps=num_bucket_jumps,
        bucket_exhausted=bucket_exhausted,
        ttl_s=float(ttl_by_bucket[initial_bucket_index]),
        kv_eviction_time_s=eviction_time_s,
        kv_retention_time_s=kv_retention_time_s,
        kv_cache_miss=kv_cache_miss,
    )
