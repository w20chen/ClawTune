from __future__ import annotations

import pytest

from tool_resource.kv_ttl import BucketTTLEvaluation, evaluate_bucket_ttl

BUCKETS = [1.0, 10.0, 60.0]
TTL_BY_BUCKET = [1.0, 0.0, 0.0]


def _eval(
    actual: float,
    initial: int,
    *,
    miss_penalty_s: float | None = None,
) -> BucketTTLEvaluation:
    return evaluate_bucket_ttl(
        actual_time_s=actual,
        initial_bucket_index=initial,
        buckets=BUCKETS,
        ttl_by_bucket=TTL_BY_BUCKET,
        miss_penalty_s=miss_penalty_s,
    )


def test_short_runtime_stays_in_first_bucket() -> None:
    ev = _eval(0.5, 0)
    assert ev.actual_time_s == 0.5
    assert ev.initial_bucket_index == 0
    assert ev.final_bucket_index == 0
    assert ev.num_bucket_jumps == 0
    assert ev.bucket_exhausted is False
    assert ev.ttl_s == 1.0
    assert ev.kv_retention_time_s == 0.5
    assert ev.kv_cache_miss is False
    assert ev.proxy_cost_s is None


def test_runtime_jumps_one_bucket() -> None:
    ev = _eval(5, 0)
    assert ev.final_bucket_index == 1
    assert ev.num_bucket_jumps == 1
    assert ev.bucket_exhausted is False
    assert ev.ttl_s == 1.0
    assert ev.kv_retention_time_s == 1.0
    assert ev.kv_cache_miss is True
    assert ev.proxy_cost_s is None


def test_runtime_jumps_two_buckets() -> None:
    ev = _eval(50, 0)
    assert ev.final_bucket_index == 2
    assert ev.num_bucket_jumps == 2
    assert ev.bucket_exhausted is False
    assert ev.ttl_s == 1.0
    assert ev.kv_retention_time_s == 1.0
    assert ev.kv_cache_miss is True
    assert ev.proxy_cost_s is None


def test_initial_second_bucket_no_jump_immediate_evict() -> None:
    ev = _eval(5, 1)
    assert ev.final_bucket_index == 1
    assert ev.num_bucket_jumps == 0
    assert ev.bucket_exhausted is False
    assert ev.ttl_s == 0.0
    assert ev.kv_retention_time_s == 0.0
    assert ev.kv_cache_miss is True
    assert ev.proxy_cost_s is None


def test_initial_second_bucket_exhausted() -> None:
    ev = _eval(100, 1)
    assert ev.final_bucket_index == 2
    assert ev.num_bucket_jumps == 1
    assert ev.bucket_exhausted is True
    assert ev.ttl_s == 0.0
    assert ev.kv_retention_time_s == 0.0
    assert ev.kv_cache_miss is True
    assert ev.proxy_cost_s is None


def test_proxy_cost_applies_miss_penalty() -> None:
    ev = _eval(5, 0, miss_penalty_s=2)
    assert ev.kv_retention_time_s == 1.0
    assert ev.kv_cache_miss is True
    assert ev.proxy_cost_s == 3.0


def test_proxy_cost_zero_penalty_is_still_number() -> None:
    ev = _eval(5, 0, miss_penalty_s=0)
    assert ev.proxy_cost_s == 1.0


def test_proxy_cost_no_miss_is_pure_retention() -> None:
    ev = _eval(0.5, 0, miss_penalty_s=2)
    assert ev.kv_cache_miss is False
    assert ev.proxy_cost_s == 0.5


def test_ttl_pinned_to_initial_bucket_not_final() -> None:
    """Bucket jumps must never re-set the KV TTL."""
    ev = evaluate_bucket_ttl(
        actual_time_s=20.0,
        initial_bucket_index=0,
        buckets=[1.0, 10.0, 60.0],
        ttl_by_bucket=[5.0, 2.0, 0.0],
    )
    assert ev.final_bucket_index == 2
    assert ev.num_bucket_jumps == 2
    assert ev.ttl_s == 5.0
    assert ev.kv_retention_time_s == 5.0
    assert ev.kv_cache_miss is True


def test_initial_last_bucket_with_small_runtime() -> None:
    ev = _eval(0.5, 2)
    assert ev.final_bucket_index == 2
    assert ev.num_bucket_jumps == 0
    assert ev.bucket_exhausted is False
    assert ev.ttl_s == 0.0
    assert ev.kv_retention_time_s == 0.0
    assert ev.kv_cache_miss is True


def test_single_bucket_edge() -> None:
    ev = evaluate_bucket_ttl(
        actual_time_s=2.0,
        initial_bucket_index=0,
        buckets=[1.0],
        ttl_by_bucket=[0.5],
    )
    assert ev.final_bucket_index == 0
    assert ev.num_bucket_jumps == 0
    assert ev.bucket_exhausted is True
    assert ev.ttl_s == 0.5
    assert ev.kv_retention_time_s == 0.5
    assert ev.kv_cache_miss is True


def test_boundary_equals_upper_bound_stays_in_bucket() -> None:
    ev = _eval(1.0, 0)
    assert ev.final_bucket_index == 0
    assert ev.num_bucket_jumps == 0
    assert ev.kv_retention_time_s == 1.0
    assert ev.kv_cache_miss is False


# --- input validation --------------------------------------------------------


def test_rejects_empty_buckets() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_bucket_ttl(1.0, 0, [], [])


@pytest.mark.parametrize(
    "buckets",
    [
        [0.0, 10.0],
        [-1.0, 10.0],
        [1.0, 1.0],
        [10.0, 1.0],
        [float("inf"), 10.0],
        [float("nan"), 10.0],
    ],
)
def test_rejects_invalid_buckets(buckets: list[float]) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_bucket_ttl(1.0, 0, buckets, [1.0, 1.0])


def test_rejects_ttl_length_mismatch() -> None:
    with pytest.raises(ValueError, match="same length"):
        evaluate_bucket_ttl(1.0, 0, [1.0, 10.0], [1.0])


@pytest.mark.parametrize("ttl", [-1.0, float("inf"), float("nan")])
def test_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        evaluate_bucket_ttl(1.0, 0, [1.0, 10.0], [ttl, 0.0])


@pytest.mark.parametrize("actual", [-0.1, float("inf"), float("nan")])
def test_rejects_invalid_actual_time(actual: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        evaluate_bucket_ttl(actual, 0, BUCKETS, TTL_BY_BUCKET)


@pytest.mark.parametrize("initial", [-1, 3, 4])
def test_rejects_invalid_initial_bucket_index(initial: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        evaluate_bucket_ttl(1.0, initial, BUCKETS, TTL_BY_BUCKET)


@pytest.mark.parametrize("penalty", [-0.1, float("inf"), float("nan")])
def test_rejects_invalid_miss_penalty(penalty: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        evaluate_bucket_ttl(1.0, 0, BUCKETS, TTL_BY_BUCKET, miss_penalty_s=penalty)
