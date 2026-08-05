"""
Deep Research Bench task discovery.

Downloads ``muset-ai/DeepResearch-Bench-Dataset`` (``generated_reports/
openai-deepresearch.jsonl``, 100 records of ``{id, prompt, article}``) and
writes a tasks JSON file that ``runner.py --dataset`` can consume.  Prefers a
local agent-test-bench checkout when it ships ``data/deep-research-bench/
tasks.json``; otherwise downloads the JSONL via ``huggingface_hub``.

Usage::

    # Download from HuggingFace
    python -m deep_research_bench.discover --out deep_research_bench/tasks-32.json --sample 32

    # Copy from a local dataset file (no network)
    python -m deep_research_bench.discover --dataset drb.jsonl --out tasks.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from deep_research_bench.task_source import (
    filter_tasks,
    load_tasks_from_drb_dataset,
    parse_instance_ids,
    tasks_from_records,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCH_ROOT_ENV = "AGENT_TEST_BENCH_ROOT"
DEFAULT_BENCH_ROOT = REPO_ROOT.parent / "agent-test-bench"
HF_DATASET = "muset-ai/DeepResearch-Bench-Dataset"
HF_DATA_FILES = "generated_reports/openai-deepresearch.jsonl"


def discover_from_agent_test_bench(
    bench_root: Path,
    sample: int = 0,
) -> list[dict[str, Any]]:
    """Load prepared tasks from a local agent-test-bench checkout when present."""
    tasks_path = bench_root / "data" / "deep-research-bench" / "tasks.json"
    if not tasks_path.exists():
        raise FileNotFoundError(
            f"agent-test-bench has no prepared DeepResearchBench tasks at "
            f"{tasks_path}. Use --source hf to download from HuggingFace."
        )
    loaded = load_tasks_from_drb_dataset(tasks_path)
    tasks = [task.as_dict() for task in loaded]
    return tasks[:sample] if sample > 0 else tasks


def discover_from_huggingface(
    *,
    dataset: str = HF_DATASET,
    data_files: str = HF_DATA_FILES,
    sample: int = 0,
) -> list[dict[str, Any]]:
    """Download the DRB JSONL and return normalized task records.

    Uses ``huggingface_hub.hf_hub_download`` in a subprocess (mirrors
    swe_rebench.discover) so a missing ``huggingface_hub`` gives a clear
    install hint instead of an import error in the runner.
    """
    code = f'''
import json, sys
try:
    from huggingface_hub import hf_hub_download
except ImportError:
    print(json.dumps({{"error": "huggingface_hub not installed. Run: pip install huggingface_hub"}}))
    sys.exit(1)

repo = "{dataset}"
data_files = "{data_files}"
try:
    local_path = hf_hub_download(repo, data_files, repo_type="dataset")
except Exception as e:
    print(json.dumps({{"error": f"Failed to download {{data_files}} from {{repo}}: {{e}}"}}))
    sys.exit(1)

sample_limit = {sample}
tasks = []
with open(local_path, encoding="utf-8") as fh:
    for index, line in enumerate(fh):
        line = line.strip()
        if not line:
            continue
        if sample_limit > 0 and len(tasks) >= sample_limit:
            break
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        row.setdefault("_row_index", str(index))
        tasks.append({{
            "instance_id": str(row.get("id") or row.get("instance_id") or index),
            "problem_statement": str(row.get("prompt") or row.get("problem_statement") or ""),
            "reference_answer": str(row.get("article") or row.get("reference_answer") or ""),
            "topic": row.get("topic"),
            "difficulty": row.get("difficulty"),
            "domain": row.get("domain"),
            "reference_kind": "generated_report",
        }})

if not tasks:
    print(json.dumps({{"error": f"No tasks loaded from {{data_files}}"}}))
    sys.exit(1)

print(json.dumps(tasks, ensure_ascii=False))
'''
    _log(f"Loading {dataset} ({data_files}) from HuggingFace...")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        try:
            err_data = json.loads(stdout)
            msg = err_data.get("error", stdout or stderr)
        except (json.JSONDecodeError, TypeError):
            msg = stderr or stdout or "unknown error"
        raise RuntimeError(f"Failed to load DeepResearchBench dataset: {msg}")
    try:
        tasks = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Failed to parse dataset output. stderr: {result.stderr[:500]}"
        )
    tasks = [task for task in tasks if isinstance(task, dict)]
    _log(f"Loaded {len(tasks)} tasks from {dataset}")
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover Deep Research Bench tasks and write a tasks JSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--out", default="deep-research-bench-tasks.json",
        help="Output JSON file (default: deep-research-bench-tasks.json)",
    )
    parser.add_argument("--sample", type=int, default=0,
                        help="Limit to first N tasks (0=all)")
    parser.add_argument("--skip", type=int, default=0,
                        help="Skip the first N selected tasks before --sample")
    parser.add_argument("--instance-ids", default=None,
                        help="Comma-separated instance IDs to include")
    parser.add_argument("--bench-root", default=None,
                        help="Path to agent-test-bench checkout")
    parser.add_argument("--dataset", default=None,
                        help="Local JSON/JSONL DRB file to copy (no network)")
    parser.add_argument("--source", choices=("auto", "hf"), default="auto",
                        help="auto = agent-test-bench first, then HuggingFace")
    parser.add_argument("--harness-dataset", default=HF_DATASET,
                        help="HuggingFace dataset id (default: muset-ai/DeepResearch-Bench-Dataset)")
    parser.add_argument("--data-files", default=HF_DATA_FILES,
                        help="Dataset JSONL data file (default: generated_reports/openai-deepresearch.jsonl)")

    args = parser.parse_args()

    tasks: list[dict[str, Any]] = []
    if args.dataset:
        loaded = load_tasks_from_drb_dataset(Path(args.dataset))
        tasks = [task.as_dict() for task in loaded]
    else:
        if args.source == "auto":
            bench_root = Path(args.bench_root) if args.bench_root else Path(
                os.getenv(DEFAULT_BENCH_ROOT_ENV, str(DEFAULT_BENCH_ROOT))
            )
            try:
                tasks = discover_from_agent_test_bench(bench_root)
            except (FileNotFoundError, RuntimeError) as exc:
                _log(f"[warn] Cannot load from agent-test-bench: {exc}")
        if not tasks:
            try:
                tasks = discover_from_huggingface(
                    dataset=args.harness_dataset,
                    data_files=args.data_files,
                )
            except RuntimeError as exc:
                _log(f"[error] {exc}")
                sys.exit(1)

    if not tasks:
        _log("No tasks discovered.")
        sys.exit(1)

    task_defs = tasks_from_records(tasks)
    task_defs = filter_tasks(
        task_defs,
        sample=args.sample,
        skip=max(0, args.skip),
        instance_ids=parse_instance_ids(args.instance_ids),
    )
    tasks = [task.as_dict() for task in task_defs]
    if not tasks:
        _log("No tasks matched the requested filters.")
        sys.exit(1)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _log(f"Wrote {len(tasks)} tasks to {out_path}")
    print(f"\nDiscovered {len(tasks)} tasks -> {out_path}")
    for i, task in enumerate(tasks[:5]):
        print(f"  [{i+1}] {task.get('instance_id')}  "
              f"{str(task.get('problem_statement', ''))[:100]}...")
    if len(tasks) > 5:
        print(f"  ... and {len(tasks) - 5} more")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
