from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from tool_resource.clause_bridge import (
    BridgeResult,
    ExecImageRecord,
    _aggregate,
    _call_resource_profile,
    _fixed_window_cpu_peak,
    _merge_rss,
    bridge_command,
)


WINDOW = 500_000_000


def _overlap_oracle(segments, t_exec, t_end, quota):
    """The original all-segment/all-window reduction, independent of integrals."""
    clean = [
        (max(t_exec, start), min(t_end, end), cpu / (end - start))
        for start, end, cpu in segments
        if min(t_end, end) > max(t_exec, start)
    ]
    if not clean:
        return None
    last = t_end - WINDOW
    candidates = {t_exec, last}
    for start, end, _ in clean:
        candidates.update(
            min(last, max(t_exec, point))
            for point in (start, end, start - WINDOW, end - WINDOW)
        )
    return min(quota, max(
        math.fsum(rate * max(0, min(end, point + WINDOW) - max(start, point))
                  for start, end, rate in clean) / WINDOW
        for point in candidates
    ))


def test_cpu_integral_matches_overlap_oracle_with_concurrency_and_clipping():
    rng = random.Random(20261024)
    for _ in range(300):
        offset = rng.choice((0, 10**18))
        duration = rng.randint(500, 4000) * 1_000_000
        segments = []
        for _ in range(rng.randint(1, 65)):
            start = offset + rng.randint(-1000, 5000) * 1_000_000
            end = start + rng.randint(1, 1500) * 1_000_000
            segments.append((start, end, rng.randint(0, 8 * (end - start))))
        rng.shuffle(segments)
        quota = rng.choice((0.25, 1.0, 4.0, 128.0))
        expected = _overlap_oracle(segments, offset, offset + duration, quota)
        actual, reason = _fixed_window_cpu_peak(segments, offset, offset + duration, quota)
        if expected is None:
            assert (actual, reason) == (None, "insufficient_cpu_samples")
        else:
            assert reason == "ok"
            assert actual == pytest.approx(expected, rel=1e-11, abs=1e-11)


@pytest.mark.parametrize("segments,start,end,quota,expected", [
    ([(0, WINDOW, 0)], 0, WINDOW, 4.0, (0.0, "ok")),
    ([(0, WINDOW, WINDOW * 4)], 0, WINDOW, 2.0, (2.0, "ok")),
    ([(0, WINDOW - 1, 1)], 0, WINDOW - 1, 4.0,
     (None, "clause_shorter_than_500ms_ineligible_for_peak")),
    ([], 0, WINDOW, 4.0, (None, "insufficient_cpu_samples")),
    ([(-WINDOW, 0, 1)], 0, WINDOW, 4.0, (None, "insufficient_cpu_samples")),
    ([(0, 0, 1)], 0, WINDOW, 4.0, (None, "invalid_cpu_profile")),
    ([(0, WINDOW, -1)], 0, WINDOW, 4.0, (None, "invalid_cpu_profile")),
    ([(0, WINDOW, 1.0)], 0, WINDOW, 4.0, (None, "invalid_cpu_profile")),
    ([(0, WINDOW, 1)], 0, WINDOW, None, (None, "missing_or_inconsistent_quota")),
    ([(0, WINDOW, 1)], 0, WINDOW, float("nan"), (None, "missing_or_inconsistent_quota")),
    ([(0, WINDOW, 1)], 0, WINDOW, 0.0, (None, "missing_or_inconsistent_quota")),
])
def test_cpu_integral_keeps_eligibility_and_invalid_profile_results(
    segments, start, end, quota, expected,
):
    assert _fixed_window_cpu_peak(segments, start, end, quota) == expected


def test_cpu_integral_does_not_accumulate_roundoff_over_empty_gaps():
    segments = [(0, 3, 1), (0, 7, 1), (10**18, 10**18 + WINDOW, WINDOW)]
    assert _fixed_window_cpu_peak(segments, 0, 10**18 + WINDOW, 8.0) == (1.0, "ok")


def test_cpu_integral_handles_large_profiles():
    # This also exercises many coincident start/end events and candidate edges.
    segments = [(index * 20_000_000, (index + 1) * 20_000_000, 10_000_000)
                for index in range(20_000)]
    assert _fixed_window_cpu_peak(segments, 0, 400_000_000_000, 8.0) == (0.5, "ok")


def _image(pid, bins, *, reason="ok"):
    return ExecImageRecord(
        host_pid=pid, exec_seq=1, t_exec_ns=0, t_end_ns=2_020_000_000,
        bin="python", argv=("python", "synthetic.py"), terminal=True,
        cpu_windows=(), rss_bins=bins, sampled_rss_reason=reason,
        normal_exit_status=0, provenance={"quota_cores": 4.0},
    )


@pytest.mark.parametrize("after,second", [(0.0, 100.0), (1.0, 99.0)])
def test_sparse_rss_never_holds_old_peak_across_missing_interval(after, second):
    images = [_image(1, ((0, 11, 100.0), (100, 11, after))),
              _image(2, ((50, 22, second),))]
    peak, reason = _merge_rss(images)
    # A can fall immediately after bin 0, before B rises at bin 50.
    possible_actual_peak = max(100.0, after + second)
    assert peak == 100.0
    assert peak <= possible_actual_peak
    assert reason == "observed_sparse_lower_bound"


def test_sparse_rss_does_not_add_asynchronous_samples_in_same_bin():
    # A=100 at t=1ms and B=90 at t=19ms need never overlap despite sharing bin 0.
    images = [_image(1, ((0, 11, 100.0),)), _image(2, ((0, 22, 90.0),))]
    assert _merge_rss(images) == (100.0, "observed_sparse_lower_bound")


def test_sparse_rss_shared_mm_duplicate_samples_do_not_fake_dense_coverage():
    images = [_image(1, ((0, 11, 100.0),)), _image(2, ((0, 11, 110.0),))]
    assert _merge_rss(images) == (110.0, "observed_sparse_lower_bound")


def test_sparse_rss_in_one_mm_makes_whole_aggregate_conservative():
    images = [_image(1, ((0, 11, 100.0), (1, 11, 10.0))),
              _image(2, ((0, 22, 90.0),))]
    assert _merge_rss(images) == (100.0, "observed_sparse_lower_bound")


def test_sparse_rss_keeps_collector_boundary_coverage_quality():
    image = _image(1, ((0, 11, 100.0), (1, 11, 90.0)),
                   reason="observed_sparse_lower_bound")
    assert _merge_rss([image]) == (100.0, "observed_sparse_lower_bound")


def test_dense_rss_preserves_live_aligned_held_sample_aggregation():
    images = [_image(1, ((0, 11, 10.0), (2, 11, 20.0))),
              _image(2, ((1, 22, 30.0), (3, 22, 40.0))),
              _image(3, ((1, 11, 10.0), (2, 11, 15.0)))]
    # At bin 2, mm11=20 plus held mm22=30; duplicate mm11 is not counted twice.
    assert _merge_rss(images) == (50.0, "ok")


@pytest.mark.parametrize("bins,expected", [
    (None, (None, "missing_rss_profile")),
    ((), (None, "insufficient_rss_samples")),
    (((0, 11, float("nan")),), (None, "invalid_rss_profile")),
    (((0, 11, -1.0),), (None, "invalid_rss_profile")),
    (((0, 11, 0.0),), (0.0, "observed_sparse_lower_bound")),
])
def test_rss_missing_invalid_and_zero_observations(bins, expected):
    assert _merge_rss([_image(1, bins)]) == expected


def test_sparse_rss_availability_and_quality_survive_clause_and_call_reduction():
    images = [_image(1, ((0, 11, 100.0),)), _image(2, ((0, 22, 90.0),))]
    clause = _aggregate("synthetic", {"bin": "python", "argv": ["python", "synthetic.py"]},
                        (1, 2), images, "synthetic ownership", 0.0)
    assert clause.observation.sampled_peak_rss_mb == 100.0
    profile = _call_resource_profile(images, [clause], data_valid=True, protocol_timeout=False)
    assert profile["sampled_peak_rss_mb"] == 100.0
    for availability, provenance in ((clause.availability, clause.provenance),
                                    (profile["availability"], profile["provenance"])):
        assert availability["memory"] == "ok"
        assert provenance["merged_rss_reason"] == "observed_sparse_lower_bound"
        assert provenance["memory_reduction"] == "max_observed_single_mm"
    schema_path = Path(__file__).resolve().parents[3] / "contracts/clause-telemetry.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(profile, {"$ref": "#/$defs/callResource", "$defs": schema["$defs"]})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**profile, "provenance": {"memory_reduction": "invented"}},
                            {"$ref": "#/$defs/callResource", "$defs": schema["$defs"]})


def test_dense_call_provenance_describes_sampled_aggregate():
    image = _image(1, ((0, 11, 100.0), (1, 11, 90.0)))
    clause = _aggregate("synthetic", {"bin": "python", "argv": ["python", "synthetic.py"]},
                        (1,), [image], "synthetic ownership", 0.0)
    assert clause.provenance["merged_rss_reason"] == "ok"
    assert clause.provenance["memory_reduction"] == "sum_aligned_distinct_live_mm_then_max"


@pytest.mark.parametrize("data_valid,timeout,quality", [
    (True, True, "protocol_timeout"),
    (False, False, "invalid_call_mapping"),
    (False, True, "invalid_call_mapping"),
])
def test_partial_call_quality_does_not_clear_observed_cpu_or_rss(data_valid, timeout, quality):
    image = replace(_image(1, ((0, 11, 100.0),)),
                    cpu_segments=((0, 2_020_000_000, 1_010_000_000),))
    clause = _aggregate("synthetic", {"bin": "python", "argv": ["python", "synthetic.py"]},
                        (1,), [image], "synthetic ownership", 0.0,
                        protocol_timeout_terminated=timeout)
    profile = _call_resource_profile([image], [clause], data_valid=data_valid, protocol_timeout=timeout)
    assert profile["peak_cpu_cores"] == pytest.approx(0.5)
    assert profile["sampled_peak_rss_mb"] == 100.0
    assert profile["availability"] == dict.fromkeys(("cpu", "memory"), f"unknown:{quality}")
    assert profile["provenance"]["merged_rss_reason"] == "observed_sparse_lower_bound"
    assert clause.observation.latency_ms == 2020.0
    assert clause.observation.cpu_peak_cores == pytest.approx(0.5)
    assert clause.observation.sampled_peak_rss_mb == 100.0
    if timeout:
        assert clause.availability["latency"] == "unknown:protocol_timeout"
        assert clause.availability["cpu_time"] == "unknown:protocol_timeout"
        assert clause.status["reason"] == "protocol_timeout"
    bridge = BridgeResult(bridged=[clause], no_runtime_exec=[], coverage_gaps=[],
                          unobserved_builtins=[], static_clause_count=1,
                          data_valid=data_valid, invalid_reasons=[])
    assert bridge.observations == [clause.observation]


def test_partial_call_without_profiles_keeps_missing_metrics_distinct_from_zero():
    profile = _call_resource_profile([], [], data_valid=True, protocol_timeout=True)
    assert profile["peak_cpu_cores"] is None and profile["sampled_peak_rss_mb"] is None
    assert profile["availability"] == {"cpu": "unknown:protocol_timeout", "memory": "unknown:protocol_timeout"}


@pytest.mark.parametrize("quality,reason", [
    ({"loss_count": 1}, "nonzero_loss"),
    ({"attribution_gap_count": 1}, "attribution_gap"),
])
def test_collector_quality_gaps_retain_the_mapped_partial_measurements(monkeypatch, quality, reason):
    monkeypatch.setattr("tool_resource.clause_bridge.parse_command_clauses", lambda command: {
        "clauses": [{"bin": "python", "argv": ["python", "synthetic.py"]}],
        "control_edges": [], "parse_failed": False,
    })
    image = replace(_image(1, ((0, 11, 100.0),)),
                    cpu_segments=((0, 2_020_000_000, 1_010_000_000),))
    result = bridge_command(command="python synthetic.py", repo="synthetic", exec_images=[image],
                            entry_pid=1, fork_parent={}, **quality)
    assert not result.data_valid
    assert reason in {gap.kind for gap in result.invalid_reasons}
    assert len(result.observations) == 1
    assert result.observations[0].cpu_peak_cores == pytest.approx(0.5)
    assert result.observations[0].sampled_peak_rss_mb == 100.0
    assert result.call_resource["peak_cpu_cores"] == pytest.approx(0.5)
    assert result.call_resource["sampled_peak_rss_mb"] == 100.0
    assert result.call_resource["availability"]["cpu"] == "unknown:invalid_call_mapping"


def test_clause_artifact_declares_partial_outcome_and_timeout_bound_fields():
    path = Path(__file__).resolve().parents[3] / "contracts/clause-telemetry.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    call_schema = {"$ref": "#/$defs/call", "$defs": schema["$defs"]}
    call = {"tool_call_id": "partial", "command": "synthetic", "telemetry_quality": "invalid",
            "eligible_for_kb": False, "clauses": [], "censored": True,
            "confirmed_timeout": True, "timeout_duration_lower_bound_ms": 1000.0}
    jsonschema.validate(call, call_schema)
    for patch in ({"censored": "true"}, {"confirmed_timeout": 1},
                  {"timeout_duration_lower_bound_ms": -1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**call, **patch}, call_schema)
