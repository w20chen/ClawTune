"""CLI entry point for the legacy evaluation.

Usage::

    python -m legacy_eval --dataset <dataset-root>
    python -m legacy_eval --dataset <dataset-root> \\
        --train-frac 0.8 --seed 42 --out legacy_eval/.runtime/report.json
    python -m legacy_eval --dataset <dataset-root> --max-train-tasks 4 --max-test-tasks 2  # smoke

Run from anywhere; the sidecar source path is bootstrapped automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from legacy_eval._bootstrap import ensure_paths

ensure_paths()

from legacy_eval.engine import EvalConfig, evaluate, write_json_report  # noqa: E402
from legacy_eval.loader import load_all  # noqa: E402
from legacy_eval.report import write_markdown_report  # noqa: E402

def _parse_edges(text: str) -> tuple[float, ...]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("bucket edges cannot be empty")
    return tuple(float(part) for part in parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legacy_eval",
        description=(
            "Evaluate ClawTune's tool-resource prediction algorithms on an "
            "external (legacy-format) trace dataset with a random train/test split."
        ),
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="legacy dataset root",
    )
    parser.add_argument(
        "--train-frac",
        type=float,
        default=0.8,
        help="training fraction of tasks (default: 0.8)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="random split seed (default: 42)",
    )
    parser.add_argument(
        "--bucket-edges",
        type=_parse_edges,
        default=(100.0, 500.0, 2_000.0, 10_000.0),
        help="comma-separated latency bucket edges in ms (default: 100,500,2000,10000)",
    )
    parser.add_argument(
        "--max-train-tasks",
        type=int,
        default=None,
        help="cap training tasks (smoke tests)",
    )
    parser.add_argument(
        "--max-test-tasks",
        type=int,
        default=None,
        help="cap test tasks (smoke tests)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output JSON report path (default: legacy_eval/.runtime/<ts>/report.json)",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="output Markdown summary path (default: alongside --out)",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        help="also print the per-algorithm summary table to stdout",
    )
    parser.add_argument(
        "--task-list",
        type=Path,
        default=None,
        help="optional JSON file with a 'tasks' list of task ids to include",
    )
    parser.add_argument(
        "--export-kb",
        type=Path,
        default=None,
        help=(
            "train on the split and export the 3 cold-start KB snapshots to "
            "this directory (default project seed: traces/tool-resource)"
        ),
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="skip the test-split evaluation/report (export-only; requires --export-kb)",
    )
    return parser


def _task_filter(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        tasks = data["tasks"]
    elif isinstance(data, list):
        tasks = data
    else:
        raise ValueError("--task-list must be a list or {'tasks': [...]}")
    return [str(item) for item in tasks if isinstance(item, str)]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    task_ids = _task_filter(args.task_list)

    tasks = load_all(args.dataset, task_ids=task_ids)
    if not tasks:
        print(f"error: no tasks loaded from {args.dataset}", file=sys.stderr)
        return 1

    config = EvalConfig(
        train_frac=args.train_frac,
        seed=args.seed,
        bucket_edges_ms=args.bucket_edges,
        max_train_tasks=args.max_train_tasks,
        max_test_tasks=args.max_test_tasks,
    )

    export_result = None
    if args.export_kb is not None:
        from legacy_eval.export import export_cold_start_kb

        export_result = export_cold_start_kb(
            tasks,
            config=config,
            out_dir=args.export_kb,
        )
        print(
            "cold-start KB export:\n"
            + json.dumps(export_result.to_json_obj(), indent=2, sort_keys=True)
        )
    if args.skip_eval:
        if export_result is None:
            print("--skip-eval requires --export-kb", file=sys.stderr)
            return 2
        return 0

    result = evaluate(tasks, dataset_dir=args.dataset, config=config)

    out_path = args.out
    if out_path is None:
        stamp = result.meta["generated_at_utc"].replace(":", "").replace("+", "_")
        run_dir = Path(__file__).resolve().parent / ".runtime" / stamp
        out_path = run_dir / "report.json"
    write_json_report(result, out_path)
    md_path = args.markdown or out_path.with_name("report.md")
    write_markdown_report(result, md_path)

    if args.print_summary:
        print(json.dumps(result.summaries, indent=2, sort_keys=True))
    print(f"wrote JSON report -> {out_path}")
    print(f"wrote Markdown    -> {md_path}")
    print(
        f"split: {len(result.train_ids)} train / {len(result.test_ids)} test "
        f"(seed={config.seed})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
