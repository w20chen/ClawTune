r"""Evaluate dynamic KV-TTL residency and miss costs on a legacy dataset.

The four latency algorithms are replayed through ``legacy_eval`` using its
static, seeded train/test protocol.  Their predictions are converted to an
initial latency bucket and evaluated by the same pure TTL function used by the
scheduler.

Example (PowerShell)::

    python scripts/evaluate_legacy_ttl_cost.py `
      --dataset <dataset-root> `
      --out-dir legacy_eval\.runtime\ttl-cost-swe277
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from legacy_eval._bootstrap import ensure_paths  # noqa: E402

ensure_paths()

from legacy_eval.engine import (  # noqa: E402
    BUCKET_TRACK,
    LATTICE_TRACKS,
    EvalConfig,
    evaluate,
)
from legacy_eval.loader import load_all  # noqa: E402
from tool_resource.kv_ttl import evaluate_bucket_ttl  # noqa: E402
from tool_resource.metrics import ecdf_quantile  # noqa: E402
from tool_resource.runtime_kb import LatencyBuckets  # noqa: E402


ALGORITHMS = (BUCKET_TRACK, *LATTICE_TRACKS)
DEFAULT_BOUNDARIES_MS = (100.0, 500.0, 2_000.0, 10_000.0)
DEFAULT_TTLS_S = (0.1, 0.5, 2.0, 0.0, 0.0)


def _float_list(text: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in text.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("value list cannot be empty")
    return values


def _predicted_bucket(
    algorithm: str,
    record: Mapping[str, Any],
    buckets: LatencyBuckets,
) -> int | None:
    if algorithm == BUCKET_TRACK:
        value = record.get("predicted_bucket")
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    value = record.get("predicted_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return buckets.bucket_id(float(value))


def _record_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("repo"),
        record.get("task_id"),
        record.get("bin"),
        tuple(record.get("argv") or ()),
        record.get("actual_ms"),
    )


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    residency = [float(row["c_r_s"]) for row in rows]
    misses = sum(int(bool(row["c_m"])) for row in rows)
    actual = [float(row["actual_time_s"]) for row in rows]
    if not rows:
        return {
            "n": 0,
            "c_r_total_s": 0.0,
            "c_r_mean_s": None,
            "c_r_median_s": None,
            "c_r_p90_s": None,
            "c_m_count": 0,
            "c_m_rate": None,
            "actual_time_total_s": 0.0,
        }
    return {
        "n": len(rows),
        "c_r_total_s": sum(residency),
        "c_r_mean_s": statistics.fmean(residency),
        "c_r_median_s": statistics.median(residency),
        "c_r_p90_s": ecdf_quantile(residency, 0.9),
        "c_m_count": misses,
        "c_m_rate": misses / len(rows),
        "actual_time_total_s": sum(actual),
    }


def _fmt(value: Any, digits: int = 6) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def _markdown(payload: Mapping[str, Any]) -> str:
    policy = payload["policy"]
    lines = [
        "# Legacy dynamic KV-TTL cost evaluation",
        "",
        f"- dataset: `{payload['dataset']}`",
        f"- split: train fraction `{payload['config']['train_frac']}`, seed `{payload['config']['seed']}`",
        f"- finite bucket boundaries (s): `{policy['bucket_boundaries_s']}`",
        f"- TTL by bucket (s): `{policy['ttl_by_bucket_s']}`",
        f"- test clause rows: `{payload['test_clause_rows']}`",
        f"- common-support rows: `{payload['common_support_n']}`",
        "",
        "## Per-algorithm available support",
        "",
        "| algorithm | available / total | coverage | C_R total (s) | C_R mean (s) | C_R p90 (s) | C_M count | C_M rate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for algorithm in ALGORITHMS:
        item = payload["per_algorithm"][algorithm]
        cost = item["cost"]
        lines.append(
            f"| {algorithm} | {item['available_n']} / {item['total_n']} | "
            f"{item['coverage']:.4f} | {cost['c_r_total_s']:.6f} | "
            f"{_fmt(cost['c_r_mean_s'])} | {_fmt(cost['c_r_p90_s'])} | "
            f"{cost['c_m_count']} | {_fmt(cost['c_m_rate'], 4)} |"
        )
    lines.extend(
        [
            "",
            "## Common support across all four algorithms",
            "",
            "| algorithm | n | C_R total (s) | C_R mean (s) | C_R p90 (s) | C_M count | C_M rate |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for algorithm in ALGORITHMS:
        cost = payload["common_support"][algorithm]
        lines.append(
            f"| {algorithm} | {cost['n']} | {cost['c_r_total_s']:.6f} | "
            f"{_fmt(cost['c_r_mean_s'])} | {_fmt(cost['c_r_p90_s'])} | "
            f"{cost['c_m_count']} | {_fmt(cost['c_m_rate'], 4)} |"
        )
    lines.extend(
        [
            "",
            "`C_R = min(T, D)` and `C_M = 1[T > D]`. Unavailable predictions are excluded from the per-algorithm table; the second table uses exactly the same rows for every algorithm.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_summary_csv(payload: Mapping[str, Any], path: Path) -> None:
    fields = [
        "support",
        "algorithm",
        "n",
        "total_n",
        "coverage",
        "c_r_total_s",
        "c_r_mean_s",
        "c_r_median_s",
        "c_r_p90_s",
        "c_m_count",
        "c_m_rate",
        "actual_time_total_s",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for algorithm in ALGORITHMS:
            item = payload["per_algorithm"][algorithm]
            writer.writerow(
                {
                    "support": "algorithm_available",
                    "algorithm": algorithm,
                    "n": item["available_n"],
                    "total_n": item["total_n"],
                    "coverage": item["coverage"],
                    **item["cost"],
                }
            )
            writer.writerow(
                {
                    "support": "common_four_algorithms",
                    "algorithm": algorithm,
                    "n": payload["common_support_n"],
                    "total_n": payload["test_clause_rows"],
                    "coverage": payload["common_support_n"]
                    / payload["test_clause_rows"],
                    **payload["common_support"][algorithm],
                }
            )


def evaluate_costs(
    dataset: Path,
    *,
    train_frac: float,
    seed: int,
    boundaries_ms: tuple[float, ...],
    ttl_by_bucket_s: tuple[float, ...],
    max_train_tasks: int | None,
    max_test_tasks: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if len(ttl_by_bucket_s) != len(boundaries_ms) + 1:
        raise ValueError("TTL list must have one value per bucket, including the tail")

    tasks = load_all(dataset)
    config = EvalConfig(
        train_frac=train_frac,
        seed=seed,
        bucket_edges_ms=boundaries_ms,
        max_train_tasks=max_train_tasks,
        max_test_tasks=max_test_tasks,
    )
    result = evaluate(tasks, dataset_dir=dataset, config=config)
    buckets = LatencyBuckets(boundaries_ms)
    boundaries_s = tuple(value / 1000.0 for value in boundaries_ms)

    track_records = {algorithm: result.records[algorithm] for algorithm in ALGORITHMS}
    lengths = {algorithm: len(records) for algorithm, records in track_records.items()}
    if len(set(lengths.values())) != 1:
        raise RuntimeError(f"algorithm record counts are not aligned: {lengths}")
    total_n = next(iter(lengths.values()), 0)

    predicted: dict[str, list[int | None]] = {}
    for algorithm, records in track_records.items():
        predicted[algorithm] = [
            _predicted_bucket(algorithm, record, buckets) for record in records
        ]
    common_indices = {
        index
        for index in range(total_n)
        if all(predicted[algorithm][index] is not None for algorithm in ALGORITHMS)
    }

    detail_rows: list[dict[str, Any]] = []
    rows_by_algorithm: dict[str, list[dict[str, Any]]] = {
        algorithm: [] for algorithm in ALGORITHMS
    }
    common_by_algorithm: dict[str, list[dict[str, Any]]] = {
        algorithm: [] for algorithm in ALGORITHMS
    }
    baseline = track_records[BUCKET_TRACK]
    for index in range(total_n):
        identity = _record_identity(baseline[index])
        for algorithm in ALGORITHMS:
            record = track_records[algorithm][index]
            if _record_identity(record) != identity:
                raise RuntimeError(f"unaligned algorithm record at row {index}: {algorithm}")
            initial_bucket = predicted[algorithm][index]
            if initial_bucket is None:
                continue
            actual_time_s = float(record["actual_ms"]) / 1000.0
            cost = evaluate_bucket_ttl(
                actual_time_s,
                initial_bucket,
                boundaries_s,
                ttl_by_bucket_s,
            )
            row = {
                "row_index": index,
                "common_support": index in common_indices,
                "algorithm": algorithm,
                "repo": record.get("repo"),
                "task_id": record.get("task_id"),
                "bin": record.get("bin"),
                "argv": record.get("argv"),
                "actual_time_s": actual_time_s,
                "initial_bucket_index": initial_bucket,
                "kv_eviction_time_s": cost.kv_eviction_time_s,
                "c_r_s": cost.kv_retention_time_s,
                "c_m": int(cost.kv_cache_miss),
            }
            detail_rows.append(row)
            rows_by_algorithm[algorithm].append(row)
            if index in common_indices:
                common_by_algorithm[algorithm].append(row)

    per_algorithm = {}
    for algorithm in ALGORITHMS:
        available_n = len(rows_by_algorithm[algorithm])
        per_algorithm[algorithm] = {
            "total_n": total_n,
            "available_n": available_n,
            "unavailable_n": total_n - available_n,
            "coverage": available_n / total_n if total_n else 0.0,
            "cost": _summary(rows_by_algorithm[algorithm]),
        }
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset.resolve()),
        "protocol": result.meta.get("protocol"),
        "config": asdict(config),
        "policy": {
            "bucket_boundaries_s": list(boundaries_s),
            "ttl_by_bucket_s": list(ttl_by_bucket_s),
            "boundary_order": "advance_bucket_before_evaluating_ttl",
            "c_r": "min(T,D)",
            "c_m": "1[T>D]",
        },
        "loaded_task_count": len(tasks),
        "train_task_count": len(result.train_ids),
        "test_task_count": len(result.test_ids),
        "test_clause_rows": total_n,
        "common_support_n": len(common_indices),
        "legacy_eval_counts": result.counts,
        "per_algorithm": per_algorithm,
        "common_support": {
            algorithm: _summary(common_by_algorithm[algorithm])
            for algorithm in ALGORITHMS
        },
    }
    return payload, detail_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--bucket-edges-ms",
        type=_float_list,
        default=DEFAULT_BOUNDARIES_MS,
    )
    parser.add_argument(
        "--ttl-by-bucket-s",
        type=_float_list,
        default=DEFAULT_TTLS_S,
    )
    parser.add_argument("--max-train-tasks", type=int, default=None)
    parser.add_argument("--max-test-tasks", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload, detail_rows = evaluate_costs(
        args.dataset,
        train_frac=args.train_frac,
        seed=args.seed,
        boundaries_ms=args.bucket_edges_ms,
        ttl_by_bucket_s=args.ttl_by_bucket_s,
        max_train_tasks=args.max_train_tasks,
        max_test_tasks=args.max_test_tasks,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary_json = args.out_dir / "ttl_cost_summary.json"
    summary_csv = args.out_dir / "ttl_cost_summary.csv"
    report_md = args.out_dir / "ttl_cost_report.md"
    detail_jsonl = args.out_dir / "ttl_cost_records.jsonl"
    summary_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_summary_csv(payload, summary_csv)
    report_md.write_text(_markdown(payload), encoding="utf-8")
    with detail_jsonl.open("w", encoding="utf-8") as fh:
        for row in detail_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(_markdown(payload))
    print(f"wrote results to {args.out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
