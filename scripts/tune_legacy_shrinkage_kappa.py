r"""Sweep small shrinkage-kappa values on a legacy trace dataset.

This is an offline experiment driver. It changes the module-level kappa only
inside this process, rebuilds the lattice KB for every candidate, and does not
modify the production default.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from legacy_eval._bootstrap import ensure_paths  # noqa: E402

ensure_paths()

from legacy_eval.engine import EvalConfig, evaluate  # noqa: E402
from legacy_eval.loader import load_all  # noqa: E402
from tool_resource.kv_ttl import evaluate_bucket_ttl  # noqa: E402
from tool_resource.runtime_kb import LatencyBuckets  # noqa: E402
import tool_time.lattice_kb as lattice_module  # noqa: E402


DEFAULT_KAPPAS = (0.5, 1.0, 2.0, 3.0, 5.0)
DEFAULT_BOUNDARIES_MS = (600.0, 2_000.0, 10_000.0, 60_000.0)
DEFAULT_TTLS_S = (0.6, 2.0, 0.0, 0.0, 0.0)


def _float_list(text: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in text.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("value list cannot be empty")
    return values


def _evaluate_one(
    tasks: dict[str, Any],
    dataset: Path,
    config: EvalConfig,
    kappa: float,
    ttl_by_bucket_s: tuple[float, ...],
) -> dict[str, Any]:
    lattice_module._SHRINKAGE_KAPPA = kappa
    result = evaluate(tasks, dataset_dir=dataset, config=config)
    bucket_summary = result.summaries["shrinkage_bucket"]
    point_summary = result.summaries["shrinkage"]
    buckets = LatencyBuckets(config.bucket_edges_ms)
    boundaries_s = tuple(edge / 1_000.0 for edge in config.bucket_edges_ms)

    c_r_total_s = 0.0
    c_m_count = 0
    predicted_n = 0
    for record in result.records["shrinkage"]:
        predicted_ms = record.get("predicted_ms")
        actual_ms = record.get("actual_ms")
        if not isinstance(predicted_ms, (int, float)) or isinstance(predicted_ms, bool):
            continue
        if not isinstance(actual_ms, (int, float)) or isinstance(actual_ms, bool):
            continue
        cost = evaluate_bucket_ttl(
            float(actual_ms) / 1_000.0,
            buckets.bucket_id(float(predicted_ms)),
            boundaries_s,
            ttl_by_bucket_s,
        )
        predicted_n += 1
        c_r_total_s += cost.kv_retention_time_s
        c_m_count += int(cost.kv_cache_miss)

    return {
        "kappa": kappa,
        "n": int(bucket_summary["n"]),
        "coverage": float(bucket_summary["coverage"]),
        "bucket_accuracy": float(bucket_summary["accuracy"]),
        "f1_macro": float(bucket_summary["f1_macro"]),
        "f1_weighted": float(bucket_summary["f1_weighted"]),
        "precision_macro": float(bucket_summary["precision_macro"]),
        "recall_macro": float(bucket_summary["recall_macro"]),
        "mae_ms": float(point_summary["mae_ms"]),
        "median_abs_error_ms": float(point_summary["median_abs_error_ms"]),
        "relative_error": float(point_summary["relative_error"]),
        "c_r_total_s": c_r_total_s,
        "c_r_mean_s": c_r_total_s / predicted_n if predicted_n else None,
        "c_m_count": c_m_count,
        "c_m_rate": c_m_count / predicted_n if predicted_n else None,
        "per_bucket": bucket_summary["per_class"],
        "confusion_matrix": bucket_summary["confusion_matrix"],
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Shrinkage kappa sweep",
        "",
        f"- dataset: `{payload['dataset']}`",
        f"- kappas: `{payload['kappas']}`",
        f"- bucket boundaries (ms): `{payload['config']['bucket_edges_ms']}`",
        f"- TTL by bucket (s): `{payload['ttl_by_bucket_s']}`",
        f"- train fraction: `{payload['config']['train_frac']}`; seed: `{payload['config']['seed']}`",
        "- warning: exploratory tuning on the held-out evaluation split; select a candidate and confirm it on a fresh validation split.",
        "",
        "| kappa | bucket accuracy | macro F1 | weighted F1 | MAE (ms) | median AE (ms) | C_R total (s) | C_M count | C_M rate |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in payload["results"]:
        lines.append(
            f"| {row['kappa']:g} | {row['bucket_accuracy']:.4%} | "
            f"{row['f1_macro']:.6f} | {row['f1_weighted']:.6f} | "
            f"{row['mae_ms']:.3f} | {row['median_abs_error_ms']:.3f} | "
            f"{row['c_r_total_s']:.6f} | {row['c_m_count']} | "
            f"{row['c_m_rate']:.4%} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--kappas", type=_float_list, default=DEFAULT_KAPPAS)
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
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if any(kappa < 0.0 for kappa in args.kappas):
        raise ValueError("kappa values must be non-negative")
    if len(args.ttl_by_bucket_s) != len(args.bucket_edges_ms) + 1:
        raise ValueError("TTL list must have one entry per bucket")

    tasks = load_all(args.dataset)
    config = EvalConfig(
        train_frac=args.train_frac,
        seed=args.seed,
        bucket_edges_ms=args.bucket_edges_ms,
    )
    original_kappa = lattice_module._SHRINKAGE_KAPPA
    try:
        results = [
            _evaluate_one(
                tasks,
                args.dataset,
                config,
                kappa,
                args.ttl_by_bucket_s,
            )
            for kappa in args.kappas
        ]
    finally:
        lattice_module._SHRINKAGE_KAPPA = original_kappa

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.dataset.resolve()),
        "config": asdict(config),
        "kappas": list(args.kappas),
        "ttl_by_bucket_s": list(args.ttl_by_bucket_s),
        "results": results,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.out_dir / "report.md").write_text(_markdown(payload), encoding="utf-8")
    csv_fields = [key for key in results[0] if key not in {"per_bucket", "confusion_matrix"}]
    with (args.out_dir / "summary.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for row in results:
            writer.writerow({key: row[key] for key in csv_fields})
    print(_markdown(payload))
    print(f"wrote results to {args.out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
