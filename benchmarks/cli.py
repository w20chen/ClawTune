from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from .bootstrap import ROOT
from .adapters import NAMES, load, select


def _offline_console_summary(report: dict, output: Path) -> dict:
    if "datasets" in report:
        return {
            "schema": report["schema"],
            "output": str(output),
            "train_tasks": report["train_tasks"],
            "test_tasks": report["test_tasks"],
            "repository_count": len(report.get("repositories", ())),
            "datasets": {
                name: _offline_console_summary(value, output / name)
                for name, value in report["datasets"].items()
            },
            "pmu": report["pmu"],
        }
    sampling = {}
    for split in ("train", "test"):
        rows = list(report.get("resource_sampling", {}).get(split, {}).values())
        available = [row for row in rows if row.get("available")]
        sampling[split] = {
            "tasks": len(rows),
            "available_tasks": len(available),
            "unavailable_tasks": len(rows) - len(available),
            "min_ms": min((row["min_ms"] for row in available), default=None),
            "max_ms": max((row["max_ms"] for row in available), default=None),
        }
    return {
        "schema": report["schema"],
        "output": str(output),
        "train_tasks": report["train_tasks"],
        "test_tasks": report["test_tasks"],
        "split_registry_path": report["split_registry_path"],
        "split_registry_status": report["split_registry_status"],
        "split_sha256": report["split_sha256"],
        "repository_count": len(report.get("repositories", ())),
        "repositories_with_test_tasks": sum(
            repo.get("test_tasks", 0) > 0 for repo in report.get("repositories", ())
        ),
        "resource_sampling": sampling,
        "metrics": report["metrics"],
        "pmu": report["pmu"],
    }


def parser():
    cli = argparse.ArgumentParser(description="ClawTune: online benchmark simulation, fixed-trace offline evaluation, KB inspection")
    sub = cli.add_subparsers(dest="command", required=True)
    bench = sub.add_parser("benchmark", help="Run a peer dataset with online KB learning")
    bench.add_argument("--benchmark", choices=NAMES, default="swe-rebench")
    bench.add_argument("--list", action="store_true", help="List all peer benchmarks without starting a runtime")
    bench.add_argument("--dataset", "--tasks", type=Path, help="JSON/JSONL tasks; Terminal Bench also accepts a task directory")
    bench.add_argument("--category", default="multi_turn_base", help="BFCL native category when --dataset is omitted")
    bench.add_argument("--resume", type=Path, help="Resume a run at a fully saved task boundary")
    bench.add_argument("--sample", type=int, help="First N selected tasks, not random sampling")
    bench.add_argument("--skip", type=int, default=0)
    bench.add_argument("--repo")
    bench.add_argument("--instance-ids")
    bench.add_argument("--seed", type=Path, default=ROOT / "seeds/demo-v1", help="Immutable seed bundle")
    bench.add_argument("--config", type=Path, help="Runner YAML including model configuration")
    bench.add_argument("--output", type=Path, help="New run directory; existing directories are never overwritten")
    bench.add_argument(
        "--parallelism",
        type=int,
        help=(
            "Maximum concurrent tasks; defaults to batch.parallelism in the "
            "runner config"
        ),
    )
    bench.add_argument("--task-timeout-seconds", type=int)
    bench.add_argument("--agent-timeout-seconds", type=int)
    bench.add_argument("--dry-run", action="store_true", help="Validate and show selected tasks/seed without Docker or an LLM")
    off = sub.add_parser("offline", help="Split fixed-format traces, train, freeze, evaluate")
    off.add_argument("--dataset", type=Path, required=True)
    off.add_argument("--benchmark", choices=NAMES, help="Fallback identity for legacy traces, or select one dataset")
    off.add_argument("--seed", type=int, default=42)
    off.add_argument("--train-fraction", type=float, default=.8,
                     help="Per-group training fraction, greater than 0 and less than 1 (default: 0.8)")
    off.add_argument("--rss-unit", choices=("MB", "MiB"), required=True, help="Explicit historical trace RSS unit")
    off.add_argument("--output", type=Path)
    off.add_argument("--split-cache-dir", type=Path,
                     help="Persistent fixed split registry (default: .runtime/offline/splits)")
    kb = sub.add_parser("kb", help="Inspect committed KB ownership and generation")
    kb.add_argument("action", choices=("status",))
    kb.add_argument("--path", type=Path)
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.command == "benchmark":
            if args.parallelism is not None and args.parallelism < 1:
                raise ValueError("parallelism must be a positive integer")
            if args.list:
                print("\n".join(NAMES))
                return 0
            if args.resume:
                if (
                    args.dataset
                    or args.sample is not None
                    or args.skip
                    or args.repo
                    or args.instance_ids
                    or args.output
                    or args.parallelism is not None
                ):
                    raise ValueError(
                        "resume uses its saved tasks and parallelism; "
                        "selection/output options cannot be combined"
                    )
                manifest = json.loads((args.resume / "run.json").read_text(encoding="utf-8"))
                from .adapters import Task
                tasks = [Task(**item) for item in manifest["tasks"]]
                args.benchmark = manifest["benchmark"]
                source = None
            else:
                tasks = None
                source = args.dataset
            if source is None and tasks is None and args.benchmark != "bfcl":
                external = Path(os.getenv("AGENT_TEST_BENCH_ROOT", str(ROOT.parent / "agent-test-bench")))
                candidates = [external / "data" / args.benchmark / "tasks.json"]
                if args.benchmark == "swe-rebench":
                    candidates.append(ROOT / "swe_rebench/tasks.json")
                elif args.benchmark == "deep-research-bench":
                    candidates.append(ROOT / "deep_research_bench/tasks.json")
                source = next((path for path in candidates if path.is_file()), None)
            if tasks is None:
                if source is None and args.benchmark == "bfcl":
                    from .backends import ensure_bfcl
                    ensure_bfcl()
                    try:
                        from bfcl_eval.utils import load_dataset_entry
                    except ImportError as exc:
                        raise ValueError("BFCL requires its installed dependencies and BFCL_REPO_PATH, or --dataset with processed entries") from exc
                    from .adapters import ADAPTERS
                    tasks = [ADAPTERS["bfcl"]({"_bfcl_entry": entry, "_bfcl_category": args.category}) for entry in load_dataset_entry(args.category)]
                elif source is not None:
                    tasks = load(args.benchmark, source.resolve())
                else:
                    raise ValueError(f"{args.benchmark}: supply --dataset with the task source")
                tasks = select(tasks, sample=args.sample, skip=args.skip, repo=args.repo, ids=args.instance_ids)
            if source and source.is_dir() and args.output and args.output.resolve().is_relative_to(source.resolve()):
                raise ValueError("run output must be outside the read-only task dataset")
            from clawtune_kb import validate_seed
            validate_seed(args.seed)
            if args.dry_run:
                print(json.dumps({"benchmark": args.benchmark, "mode": "online", "kb_frozen": False,
                    "parallelism_override": args.parallelism,
                    "seed": str(args.seed.resolve()), "tasks": [{"id": task.task_id, "group": task.group,
                    "executor": task.kind, "image": task.image} for task in tasks]}, indent=2))
                return 0
            from .runner import run
            config = args.config or ROOT / "configs/benchmark.yaml"
            if args.config is None and not config.is_file():
                config = ROOT / ("deep_research_bench" if args.benchmark == "deep-research-bench" else "swe_rebench") / "config.yaml"
            if not config.is_file():
                raise ValueError(f"config missing: {config}; run setup or pass --config")
            result = run(tasks, config_path=config.resolve(), seed=args.seed.resolve(), output=args.output,
                         resume=args.resume, task_timeout=args.task_timeout_seconds,
                         agent_timeout=args.agent_timeout_seconds, parallelism=args.parallelism)
            return 0 if result["status"] == "completed" else 1
        if args.command == "offline":
            from offline.runner import run
            output = args.output or ROOT / ".runtime/offline" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            result = run(args.dataset, output, benchmark=args.benchmark, seed=args.seed,
                         rss_unit=args.rss_unit, split_cache_dir=args.split_cache_dir,
                         train_fraction=args.train_fraction)
            print(json.dumps(_offline_console_summary(result, output), indent=2))
            return 0
        from clawtune_kb import user_state_dir
        from clawtune_kb.store import committed_state
        path = args.path or user_state_dir() / "kb"
        print(json.dumps(committed_state(path), indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        cli.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
