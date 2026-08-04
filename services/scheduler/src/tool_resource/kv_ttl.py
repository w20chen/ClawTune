"""Simplified KV-TTL cost evaluation for tool runtime time buckets.

This module is intentionally small and standalone: it does not touch the
existing bucket predictor.  The predictor emits an ``initial_bucket_index``;
this module turns the observed tool runtime into bucket-jump diagnostics plus a
lightweight KV cost proxy driven by a per-bucket TTL policy.

Bucket values are the *upper bounds* of the tool runtime intervals::

    BUCKETS = [1.0, 10.0, 60.0]

defines the intervals ``(0, 1]``, ``(1, 10]``, ``(10, 60]``, ``(60, +inf)``.
The TTL is chosen exactly once from the initial prediction bucket
(``ttl_by_bucket[initial_bucket_index]``); later bucket jumps only update the
duration state and never re-set the KV TTL.

No KV size, bandwidth, or transfer time is assumed — the only metrics emitted
are the retention time ``min(T, tau)`` and the boolean cache miss
``T > tau``, plus an optional miss penalty ``C = H + B*M`` when a
``miss_penalty_s`` is supplied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BucketTTLEvaluation:
    """Bucket-jump diagnostics and KV cost proxy for one observed tool runtime.

    Attributes:
        actual_time_s: observed tool runtime ``T``.
        initial_bucket_index: bucket predicted before execution.
        final_bucket_index: bucket reached by following bucket jumps.
        num_bucket_jumps: number of upper bounds crossed.
        bucket_exhausted: ``T`` exceeds the last bucket's upper bound.
        ttl_s: KV TTL ``tau`` fixed by the initial prediction bucket.
        kv_retention_time_s: KV resident time ``min(T, tau)``.
        kv_cache_miss: ``True`` when ``T > tau`` (KV already evicted).
        proxy_cost_s: ``retention + miss_penalty * miss``, or ``None`` when
            no ``miss_penalty_s`` was provided.
    """

    actual_time_s: float
    initial_bucket_index: int
    final_bucket_index: int
    num_bucket_jumps: int
    bucket_exhausted: bool

    ttl_s: float
    kv_retention_time_s: float
    kv_cache_miss: bool
    proxy_cost_s: float | None


def evaluate_bucket_ttl(
    actual_time_s: float,
    initial_bucket_index: int,
    buckets: list[float],
    ttl_by_bucket: list[float],
    miss_penalty_s: float | None = None,
) -> BucketTTLEvaluation:
    """Evaluate one observed tool runtime against the bucket/TTL policy.

    Args:
        actual_time_s: observed tool runtime in seconds (``>= 0``).
        initial_bucket_index: bucket index from the existing predictor; must be
            in ``[0, len(buckets))`` because the TTL is indexed by it.
        buckets: strictly increasing, positive upper bounds of the runtime
            intervals (seconds).
        ttl_by_bucket: per-bucket KV TTL in seconds (``>= 0``), same length as
            ``buckets``.  ``0.0`` evicts the KV immediately.
        miss_penalty_s: optional non-negative penalty added to the proxy cost
            on a KV miss.  When ``None``, ``proxy_cost_s`` is ``None``.

    Returns:
        A :class:`BucketTTLEvaluation`.  The TTL always comes from the initial
        prediction bucket and is never changed by later bucket jumps.
    """

    if not buckets:
        raise ValueError("buckets must be non-empty")
    if len(ttl_by_bucket) != len(buckets):
        raise ValueError("ttl_by_bucket must have the same length as buckets")
    previous = 0.0
    for edge in buckets:
        if not math.isfinite(edge) or edge <= previous:
            raise ValueError(
                "buckets must be finite, positive, and strictly increasing"
            )
        previous = edge
    for ttl in ttl_by_bucket:
        if not math.isfinite(ttl) or ttl < 0.0:
            raise ValueError("ttl_by_bucket values must be finite and non-negative")
    if not math.isfinite(actual_time_s) or actual_time_s < 0.0:
        raise ValueError("actual_time_s must be finite and non-negative")
    if not 0 <= initial_bucket_index < len(buckets):
        raise ValueError("initial_bucket_index is out of range")
    if miss_penalty_s is not None and (
        not math.isfinite(miss_penalty_s) or miss_penalty_s < 0.0
    ):
        raise ValueError("miss_penalty_s must be finite and non-negative")

    actual = float(actual_time_s)

    # Bucket jumps only update the duration state; the TTL below is fixed.
    final_index = initial_bucket_index
    num_jumps = 0
    while final_index < len(buckets) - 1 and actual > buckets[final_index]:
        final_index += 1
        num_jumps += 1

    bucket_exhausted = actual > buckets[-1]
    ttl_s = float(ttl_by_bucket[initial_bucket_index])
    kv_retention_time_s = min(actual, ttl_s)
    kv_cache_miss = actual > ttl_s
    if miss_penalty_s is None:
        proxy_cost_s: float | None = None
    else:
        proxy_cost_s = kv_retention_time_s + float(miss_penalty_s) * float(
            kv_cache_miss
        )

    return BucketTTLEvaluation(
        actual_time_s=actual,
        initial_bucket_index=initial_bucket_index,
        final_bucket_index=final_index,
        num_bucket_jumps=num_jumps,
        bucket_exhausted=bucket_exhausted,
        ttl_s=ttl_s,
        kv_retention_time_s=kv_retention_time_s,
        kv_cache_miss=kv_cache_miss,
        proxy_cost_s=proxy_cost_s,
    )
