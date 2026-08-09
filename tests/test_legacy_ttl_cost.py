"""Regression tests for the standalone legacy KV-TTL cost report."""

from __future__ import annotations

import csv

from scripts.evaluate_legacy_ttl_cost import ALGORITHMS, _write_summary_csv


def test_empty_test_support_writes_zero_coverage_csv(tmp_path) -> None:
    empty_cost = {
        "n": 0,
        "c_r_total_s": 0.0,
        "c_r_mean_s": None,
        "c_r_median_s": None,
        "c_r_p90_s": None,
        "c_m_count": 0,
        "c_m_rate": None,
        "actual_time_total_s": 0.0,
    }
    payload = {
        "test_clause_rows": 0,
        "common_support_n": 0,
        "per_algorithm": {
            algorithm: {
                "available_n": 0,
                "total_n": 0,
                "coverage": 0.0,
                "cost": empty_cost,
            }
            for algorithm in ALGORITHMS
        },
        "common_support": {
            algorithm: empty_cost for algorithm in ALGORITHMS
        },
    }
    output = tmp_path / "summary.csv"

    _write_summary_csv(payload, output)

    with output.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    common_rows = [row for row in rows if row["support"] == "common_four_algorithms"]
    assert len(common_rows) == len(ALGORITHMS)
    assert {row["coverage"] for row in common_rows} == {"0.0"}
