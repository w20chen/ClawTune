from __future__ import annotations

import pytest

from scripts.benchmark_lattice_accuracy import Baseline, quantiles, summarize
from tool_resource.runtime_kb import ClauseObservation


def test_physical_errors_zero_handling_paired_baseline_and_pinball():
    rows = [
        dict(task="a", actual=0, p50=0, p90=1, baseline_p50=1, baseline_p90=2),
        dict(task="a", actual=1, p50=2, p90=2, baseline_p50=1, baseline_p90=2),
        dict(task="b", actual=9, p50=3, p90=10, baseline_p50=5, baseline_p90=8),
        dict(task="b", actual=2, p50=None, p90=None, baseline_p50=2, baseline_p90=3),
    ]
    result = summarize(rows)
    assert result["prediction_coverage"] == .75
    assert result["mae"] == pytest.approx(7 / 3)
    assert result["median_absolute_error"] == 1
    assert result["p90_absolute_error"] == 6
    assert result["wape"] == .7
    assert result["positive_actual_count"] == 2
    assert result["within_2x"] == .5
    assert result["paired_baseline_mae"] == pytest.approx(5 / 3)
    assert result["mae_reduction_vs_baseline"] == pytest.approx(-.4)
    assert result["macro_task_mae"] == 3.25
    assert result["p90_coverage"] == 1
    assert result["p90_pinball_loss"] == pytest.approx(.1)
    assert result["paired_baseline_p90_pinball_loss"] == pytest.approx(.4)


def test_all_missing_and_all_zero_actual_are_explicit():
    assert summarize([dict(p50=None)])["prediction_coverage"] == 0
    result = summarize([dict(task="a", actual=0, p50=1, p90=None, baseline_p50=0)])
    assert result["wape"] is None
    assert result["within_2x"] is None
    assert result["mae_reduction_vs_baseline"] is None
    assert "p90_coverage" not in result


def test_baseline_uses_supported_training_groups_without_command_prefixes():
    rows = [ClauseObservation(repo="r", bin="python", argv=("python", "task.py"),
                              ts_start=i, ts_end=i+1, latency_ms=1000,
                              cpu_ns_cumulative=2_000_000_000) for i in range(5)]
    baseline = Baseline(rows)
    assert baseline.predict("r", "python", "cpu_time_seconds") == (2, 2, 5)
    assert baseline.predict("other", "python", "cpu_time_seconds") == (2, 2, 5)
    assert baseline.predict("other", "node", "cpu_time_seconds") == (2, 2, 5)
    assert baseline.predict("r", "python", "memory_peak_rss_bytes") is None
    assert quantiles([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) == (4.5, 8)
