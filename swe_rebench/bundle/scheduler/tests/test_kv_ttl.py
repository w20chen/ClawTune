from __future__ import annotations

import pytest

from tool_resource.kv_ttl import (
    BucketTTLEvaluation,
    evaluate_bucket_ttl,
    eviction_time_for_bucket_ttl,
)


BOUNDARIES = [0.1, 0.5, 2.0, 10.0]
TTL_BY_BUCKET = [0.1, 0.5, 2.0, 0.0, 0.0]


def _eval(actual: float, initial: int) -> BucketTTLEvaluation:
    return evaluate_bucket_ttl(actual, initial, BOUNDARIES, TTL_BY_BUCKET)


def test_paper_example_finishes_before_dynamic_eviction() -> None:
    ev = _eval(1.2, 1)
    assert ev.final_bucket_index == 2
    assert ev.num_bucket_jumps == 1
    assert ev.kv_eviction_time_s == 2.0
    assert ev.kv_retention_time_s == 1.2
    assert ev.kv_cache_miss is False


def test_paper_example_runs_past_dynamic_eviction() -> None:
    ev = _eval(6.0, 1)
    assert ev.final_bucket_index == 3
    assert ev.num_bucket_jumps == 2
    assert ev.kv_eviction_time_s == 2.0
    assert ev.kv_retention_time_s == 2.0
    assert ev.kv_cache_miss is True


def test_bucket_jump_replaces_initial_ttl() -> None:
    ev = evaluate_bucket_ttl(3.0, 0, [1.0, 10.0, 60.0], [5.0, 2.0, 0.0, 0.0])
    assert ev.ttl_s == 5.0
    assert ev.kv_eviction_time_s == 2.0
    assert ev.kv_retention_time_s == 2.0
    assert ev.kv_cache_miss is True


def test_boundary_selects_new_bucket_before_evaluating_ttl() -> None:
    eviction = eviction_time_for_bucket_ttl(0, [1.0, 10.0], [1.0, 10.0, 0.0])
    assert eviction == 10.0


def test_runtime_equal_to_eviction_boundary_is_not_a_miss() -> None:
    ev = evaluate_bucket_ttl(10.0, 0, [1.0, 10.0], [1.0, 10.0, 0.0])
    assert ev.final_bucket_index == 2
    assert ev.num_bucket_jumps == 2
    assert ev.bucket_exhausted is True
    assert ev.kv_eviction_time_s == 10.0
    assert ev.kv_retention_time_s == 10.0
    assert ev.kv_cache_miss is False


def test_zero_ttl_in_initial_bucket_evicts_immediately() -> None:
    ev = _eval(0.5, 3)
    assert ev.kv_eviction_time_s == 0.0
    assert ev.kv_retention_time_s == 0.0
    assert ev.kv_cache_miss is True


def test_open_ended_bucket_has_own_ttl() -> None:
    ev = evaluate_bucket_ttl(20.0, 2, [1.0, 10.0], [1.0, 10.0, 15.0])
    assert ev.bucket_exhausted is True
    assert ev.kv_eviction_time_s == 15.0
    assert ev.kv_retention_time_s == 15.0
    assert ev.kv_cache_miss is True


def test_ttl_before_later_bucket_start_evicts_at_bucket_entry() -> None:
    assert eviction_time_for_bucket_ttl(0, [1.0, 10.0], [5.0, 0.5, 0.0]) == 1.0


def test_rejects_empty_boundaries() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_bucket_ttl(1.0, 0, [], [0.0])


@pytest.mark.parametrize(
    "boundaries",
    [
        [0.0, 10.0],
        [-1.0, 10.0],
        [1.0, 1.0],
        [10.0, 1.0],
        [float("inf"), 10.0],
        [float("nan"), 10.0],
    ],
)
def test_rejects_invalid_boundaries(boundaries: list[float]) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_bucket_ttl(1.0, 0, boundaries, [1.0, 1.0, 0.0])


def test_rejects_ttl_length_mismatch() -> None:
    with pytest.raises(ValueError, match="one value per bucket"):
        evaluate_bucket_ttl(1.0, 0, [1.0, 10.0], [1.0, 0.0])


@pytest.mark.parametrize("ttl", [-1.0, float("inf"), float("nan")])
def test_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        evaluate_bucket_ttl(1.0, 0, [1.0], [ttl, 0.0])


@pytest.mark.parametrize("actual", [-0.1, float("inf"), float("nan")])
def test_rejects_invalid_actual_time(actual: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        evaluate_bucket_ttl(actual, 0, [1.0], [1.0, 0.0])


@pytest.mark.parametrize("initial", [-1, 2, 3])
def test_rejects_invalid_initial_bucket_index(initial: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        evaluate_bucket_ttl(1.0, initial, [1.0], [1.0, 0.0])
