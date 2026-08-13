"""
Deep Research Bench Batch Runner
================================

Orchestrates batch execution of DeepResearchBench research tasks with OpenClaw
+ sidecar trace collection.  Each task is a research question run by the
OpenClaw agent whose tools execute in a very basic Docker sandbox image (no
SWE-Bench task image, no /testbed export).  Telemetry is the read/edit/web-tool
style (sandbox-container / per-PID docker-exec); the relaxed required-telemetry
gate checks LLM + resource-sampled tool spans and does not require eBPF
exec clause artifacts.

Usage::

    # 1. Prepare the runtime assets (once)
    python -m deep_research_bench.runner prepare

    # 2. Run tasks from a DeepResearchBench dataset
    python -m deep_research_bench.runner run --dataset ./tasks.json --sample 5

    # all-in-one via the ClawTune CLI (Linux host):
    python3 scripts/clawtune.py drb --sample 1 --parallelism 1
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swe_rebench.docker import ContainerResult
from swe_rebench.prepare import build_runtime_assets, runtime_assets_need_rebuild
from swe_rebench.runner import (
    _agent_diagnostics,
    _count_lines,
    _inspect_tool_resource_artifacts,
    _inspect_trace,
    _nested_get,
    _reset_task_trace_dir,
    _resource_summary,
    _smoke_summary,
    _task_artifacts,
)

from deep_research_bench.config import DRBConfig
from deep_research_bench.host_runner import (
    _ensure_basic_image,
    run_drb_task,
)
from deep_research_bench.task_source import (
    DRBTask,
    filter_tasks,
    load_tasks_from_drb_dataset,
    load_tasks_from_simple_list,
    parse_instance_ids,
)


# ── Report ───────────────────────────────────────────────────────

@dataclass
class BatchReport:
    config_path: str
    total_tasks: int
    completed: int = 0
    failed: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    aborted: bool = False
    abort_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config_path,
            "benchmark": "deep-research-bench",
            "total_tasks": self.total_tasks,
            "completed": self.completed,
            "failed": self.failed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 1),
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "results": self.results,
        }


def _result_dict(config: DRBConfig, result: ContainerResult) -> dict[str, Any]:
    """Build the report entry for one task result (mirrors swe_rebench.runner)."""
    trace_inspection = [_inspect_trace(tf, result.task_id) for tf in result.trace_files]
    resource_summary = _resource_summary(trace_inspection)
    tool_resource_artifacts = _inspect_tool_resource_artifacts(result.trace_dir)
    artifacts = _task_artifacts(result.trace_dir)
    smoke = _smoke_summary(artifacts)
    return {
        "task_id": result.task_id,
        "image": result.image,
        "exit_code": result.exit_code,
        "error": result.error,
        "trace_dir": str(result.trace_dir) if result.trace_dir else None,
        "trace_files": [str(tf) for tf in result.trace_files],
        "trace_lines": sum(_count_lines(tf) for tf in result.trace_files),
        "trace_inspection": trace_inspection,
        "resource_summary": resource_summary,
        "tool_resource_artifacts": tool_resource_artifacts,
        "artifacts": artifacts,
        "smoke": smoke,
        "agent_diagnostics": _drb_agent_diagnostics(
            trace_inspection, artifacts, smoke
        ),
        "duration_seconds": round(result.duration_seconds, 1),
    }


def _drb_agent_diagnostics(
    trace_inspection: list[dict[str, Any]],
    artifacts: dict[str, Any],
    smoke: dict[str, Any],
) -> dict[str, Any]:
    """swe_rebench agent diagnostics minus the code-patch expectation.

    Research tasks produce a text answer, not a repository patch, so the swe
    ``no_patch`` failure is not applicable.
    """
    diagnostics = _agent_diagnostics(trace_inspection, artifacts, smoke)
    if diagnostics.get("failure_kind") == "no_patch":
        diagnostics["failure_kind"] = None
        diagnostics["failure"] = None
    return diagnostics


def _drb_required_telemetry_error(
    config: DRBConfig,
    result: dict[str, Any],
) -> str | None:
    """Relaxed required-telemetry gate for research tasks.

    Requires at least one LLM span and one resource-sampled tool span in the
    v6 trace.  eBPF exec-clause telemetry is never required: research
    tools are read/edit/web style, measured via the sandbox-container /
    per-PID scope.

    Sampling is required only for sandbox-executed (launcher-mode) tool spans.
    Research runs can legitimately invoke host-side in-process tools (for
    example OpenClaw's ``session_status``) that never enter the sandbox and
    therefore carry no sandbox resource sampling; requiring 100% of every tool
    span to be sampled would misclassify an otherwise healthy run as FAIL.
    """
    if not config.gate_required:
        return None
    resources = result.get("resource_summary")
    trace_inspection = result.get("trace_inspection")
    if not isinstance(resources, dict) or not isinstance(trace_inspection, list):
        return "required resource telemetry audit is missing"
    tool_spans = int(resources.get("tool_span_ends", 0))
    if tool_spans == 0:
        return "required resource telemetry found no tool spans"
    sampled_spans = int(resources.get("resource_sampled_tool_span_ends", 0))
    launcher_spans = int(resources.get("launcher_tool_span_ends", 0))
    if launcher_spans and sampled_spans < launcher_spans:
        return (
            "required resource telemetry is incomplete: "
            f"sampled {sampled_spans}/{launcher_spans} launcher tool spans"
        )
    if sampled_spans == 0:
        return "required resource telemetry found no sampled tool spans"
    if not any(bool(item.get("has_llm_span")) for item in trace_inspection):
        return "required telemetry found no LLM spans"
    return None


def _result_trace_dir(config: DRBConfig, task: DRBTask) -> Path:
    safe_id = task.instance_id.replace("/", "_").replace(":", "_")
    return config.output.trace_root / safe_id


def run_batch(
    config: DRBConfig,
    tasks: list[DRBTask],
    runtime_assets_dir: Path,
    *,
    export_after: bool = False,
) -> BatchReport:
    """Run a batch of DeepResearchBench tasks and return a summary report."""
    duplicate_ids = sorted(
        task_id
        for task_id, count in collections.Counter(
            task.instance_id for task in tasks
        ).items()
        if count > 1
    )
    if duplicate_ids:
        raise ValueError(
            "duplicate task instance_id values are unsafe for concurrent "
            f"runtime isolation: {duplicate_ids}"
        )
    parallelism = max(1, config.batch.parallelism)
    _log(f"\n{'='*60}")
    mode_label = "serial execution" if parallelism == 1 else f"parallelism={parallelism}"
    _log(f"DRB batch run: {len(tasks)} tasks, {mode_label}")
    _log(f"Trace root: {config.output.trace_root}")
    _log(f"{'='*60}\n")

    swe_cfg = config.to_swe_runner_config()
    # Pull the basic sandbox image once before per-task accounting starts.
    _ensure_basic_image(config, swe_cfg)
    _log(f"Basic sandbox image: {config.sandbox.image}")

    report = BatchReport(
        config_path=str(config.config_path or ""),
        total_tasks=len(tasks),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    start_wall = time.monotonic()
    completed_count = 0
    failed_count = 0

    def run_task(task: DRBTask) -> ContainerResult:
        trace_dir = _result_trace_dir(config, task)
        # Remove stale per-task artifacts (including trace files left by a
        # previous run of the same task id) before a fresh run.  Without this,
        # a re-run can pick up old shell-not-executable spans from a prior
        # broken run and misclassify an otherwise successful run as FAIL.
        _reset_task_trace_dir(
            config.output.trace_root,
            trace_dir,
            docker_cleanup_image=config.sandbox.image,
            docker_platform=config.docker.platform,
        )
        task_started = time.monotonic()
        try:
            return run_drb_task(
                task=task,
                trace_dir=trace_dir,
                config=config,
                swe_cfg=swe_cfg,
                runtime_assets_dir=runtime_assets_dir,
            )
        except Exception as exc:
            # Host-sandbox cleanup failures must not hide telemetry that the
            # sidecar already produced for this task.
            return ContainerResult(
                task_id=task.instance_id,
                image=config.sandbox.image,
                exit_code=-1,
                error=str(exc),
                trace_dir=trace_dir,
                trace_files=sorted(trace_dir.glob("*.jsonl")),
                duration_seconds=time.monotonic() - task_started,
            )

    def handle_result(result: ContainerResult, task: DRBTask) -> None:
        nonlocal completed_count, failed_count
        result_dict = _result_dict(config, result)
        telemetry_error = _drb_required_telemetry_error(config, result_dict)
        agent_error = _nested_get(result_dict, ("agent_diagnostics", "failure"))
        telemetry_not_evaluable = bool(
            config.gate_required
            and isinstance(agent_error, str)
            and int(
                _nested_get(result_dict, ("resource_summary", "tool_span_ends"))
                or 0
            )
            == 0
        )
        result_dict["telemetry_audit"] = {
            "required": config.gate_required,
            "mode": "relaxed",
            "status": (
                "not_evaluable"
                if telemetry_not_evaluable
                else "failed"
                if telemetry_error is not None
                else ("passed" if config.gate_required else "not_required")
            ),
            "error": None if telemetry_not_evaluable else telemetry_error,
            "not_evaluable_reason": telemetry_error if telemetry_not_evaluable else None,
        }
        resources = result_dict.get("resource_summary")
        if telemetry_error is None and config.gate_required and isinstance(resources, dict):
            tool_spans = int(resources.get("tool_span_ends", 0))
            sandbox_attributed = int(
                resources.get("shared_sandbox_tool_span_ends", 0)
            ) + int(resources.get("docker_exec_pid_tool_span_ends", 0)) + int(
                resources.get("cgroup_tool_span_ends", 0)
            )
            if tool_spans and sandbox_attributed == tool_spans:
                result_dict["telemetry_audit"]["note"] = (
                    "tool spans are sandbox-container/per-PID attributed "
                    "(research read/edit/web tools), not eBPF exec clauses"
                )
                _log(
                    "       note: tool spans are sandbox-container/per-PID "
                    "attributed (research read/edit/web tools)"
                )
        primary_error = (
            str(agent_error)
            if isinstance(agent_error, str) and agent_error
            else telemetry_error
        )
        if primary_error is not None and result.exit_code == 0 and not result.error:
            result.exit_code = -1
            result.error = primary_error
            result_dict["exit_code"] = result.exit_code
            result_dict["error"] = result.error
        report.results.append(result_dict)

        if result.exit_code == 0 and not result.error:
            completed_count += 1
            status = "OK"
        else:
            failed_count += 1
            status = "FAIL"
        progress = completed_count + failed_count
        _log(
            f"[{progress}/{len(tasks)}] {status} "
            f"task={result.task_id} "
            f"exit={result.exit_code} "
            f"traces={len(result.trace_files)} "
            f"lines={result_dict['trace_lines']} "
            f"time={result.duration_seconds:.0f}s"
        )
        if result.error:
            _log(f"       error: {result.error}")

    try:
        if parallelism == 1:
            for task in tasks:
                handle_result(run_task(task), task)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=parallelism
            ) as executor:
                futures = {executor.submit(run_task, task): task for task in tasks}
                for future in concurrent.futures.as_completed(futures):
                    task = futures[future]
                    handle_result(future.result(), task)
    except KeyboardInterrupt:
        raise

    report.completed = completed_count
    report.failed = failed_count
    report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.duration_seconds = time.monotonic() - start_wall

    _log(f"\n{'='*60}")
    _log(
        f"Done: {completed_count} OK, {failed_count} FAIL, "
        f"{report.duration_seconds:.0f}s total"
    )
    _log(f"{'='*60}\n")

    report_path = config.output.report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _log(f"Report written to {report_path}")

    if export_after:
        _export_traces(config, report)
    return report


def _export_traces(config: DRBConfig, report: BatchReport) -> None:
    flat = config.output.flat_export_dir
    if flat is None:
        return
    flat.mkdir(parents=True, exist_ok=True)
    copied = 0
    for result in report.results:
        trace_dir = result.get("trace_dir")
        if not trace_dir:
            continue
        for trace_file in sorted(Path(trace_dir).glob("*.jsonl")):
            shutil.copy2(trace_file, flat / f"{result['task_id']}__{trace_file.name}")
            copied += 1
    _log(f"Exported {copied} trace file(s) to {flat}")


# ── CLI helpers ──────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _detect_repo_root() -> Path:
    path = Path(__file__).resolve()
    for _ in range(6):
        if (path / "AGENTS.md").exists():
            return path
        path = path.parent
    return Path.cwd()


def _resolve_path(value: str, repo_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _resolve_config_path(config_arg: str | None, repo_root: Path, default_config: Path) -> Path:
    if config_arg:
        candidate = Path(config_arg)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.exists():
            return candidate
        example = repo_root / "deep_research_bench" / "config.example.yaml"
        if candidate == example:
            raise FileNotFoundError(f"Config file not found: {candidate}")
        _log(f"Warning: config file not found at {candidate}; falling back to example config {example}")
        return example
    return default_config


def _apply_batch_overrides(
    config: DRBConfig,
    *,
    task_timeout_seconds: int | None = None,
    agent_timeout_seconds: int | None = None,
    parallelism: int | None = None,
) -> None:
    for option, value in (
        ("--task-timeout-seconds", task_timeout_seconds),
        ("--agent-timeout-seconds", agent_timeout_seconds),
    ):
        if value is not None and value < 0:
            raise ValueError(f"{option} must be >= 0")
    if task_timeout_seconds is not None:
        config.batch.task_timeout_seconds = task_timeout_seconds
    if agent_timeout_seconds is not None:
        config.batch.agent_timeout_seconds = agent_timeout_seconds
    if parallelism is not None:
        if parallelism < 1:
            raise ValueError("--parallelism must be >= 1")
        config.batch.parallelism = parallelism


def _require_llm_api_key(config: DRBConfig) -> None:
    if config.llm.api_key:
        return
    _log(
        "ERROR: no LLM API key configured. Set LLM_API_KEY or configure "
        "llm.api_key / llm.api_key_file in the Deep Research Bench config."
    )
    sys.exit(1)


def _print_report_json(report: BatchReport, *, enabled: bool) -> None:
    if enabled:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))


def _load_tasks(args: argparse.Namespace, repo_root: Path) -> list[DRBTask]:
    """Load tasks from whichever source was specified."""
    if getattr(args, "tasks", None):
        path = _resolve_path(args.tasks, repo_root)
        if not path.exists():
            raise FileNotFoundError(
                f"Tasks file not found: {path}\n"
                f"Generate one with: python -m deep_research_bench.discover --out {path}"
            )
        return load_tasks_from_simple_list(path)

    if getattr(args, "dataset", None):
        path = _resolve_path(args.dataset, repo_root)
        if not path.exists():
            raise FileNotFoundError(
                f"Dataset file not found: {path}\n"
                f"Generate one with: python -m deep_research_bench.discover --out {path}"
            )
        return load_tasks_from_drb_dataset(path)

    bundled = repo_root / "deep_research_bench" / "tasks.json"
    if bundled.exists():
        _log(f"Using bundled DeepResearchBench smoke task source: {bundled}")
        return load_tasks_from_drb_dataset(bundled)
    return []


# ── CLI ──────────────────────────────────────────────────────────

def main() -> None:
    repo_root = _detect_repo_root()
    default_config = repo_root / "deep_research_bench" / "config.example.yaml"

    parser = argparse.ArgumentParser(
        description=(
            "Deep Research Bench batch runner with OpenClaw + sidecar trace "
            "collection (research agent in a very basic Docker sandbox)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default=default_config,
        help=f"Path to config YAML (default: {default_config})",
    )
    sub = parser.add_subparsers(dest="command", help="Sub-command")

    def add_config_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config", default=None,
            help=f"Path to config YAML (default: {default_config})",
        )

    prep = sub.add_parser("prepare", help="Build the runtime assets")
    add_config_arg(prep)
    prep.add_argument("--runtime-assets-dir", default=None,
                      help="Override runtime-assets output directory")

    run_p = sub.add_parser("run", help="Run DeepResearchBench tasks")
    add_config_arg(run_p)
    run_p.add_argument("--prepare", action="store_true", dest="do_prepare",
                       help="Run prepare step before executing tasks")
    run_p.add_argument("--dataset", default=None,
                       help="Path to DeepResearchBench dataset JSON/JSONL file")
    run_p.add_argument("--tasks", default=None,
                       help="Path to simple JSON task list")
    run_p.add_argument("--sample", type=int, default=None,
                       help="Run exactly the first N selected tasks (error if fewer exist)")
    run_p.add_argument("--skip", type=int, default=0,
                       help="Skip the first N selected tasks before --sample")
    run_p.add_argument("--instance-ids", default=None,
                       help="Comma-separated instance IDs to run, preserving the given order")
    run_p.add_argument("--task-timeout-seconds", "--timeout-seconds",
                       type=int, default=None,
                       help="Hard wall-clock limit for each task in seconds")
    run_p.add_argument("--agent-timeout-seconds", type=int, default=None,
                       help="Limit only the OpenClaw agent phase (0 disables)")
    run_p.add_argument("--parallelism", type=int, default=None,
                       help="Number of tasks to run concurrently")
    run_p.add_argument(
        "--gate-required",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Require the relaxed LLM + tool-span telemetry gate (default: config)",
    )
    run_p.add_argument("--export", action="store_true",
                       help="Export traces to flat directory after run")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Print tasks without running them")
    run_p.add_argument(
        "--json",
        action="store_true",
        dest="print_report_json",
        help="Print the complete report JSON to stdout",
    )

    cln = sub.add_parser("cleanup", help="(No-op: containers are auto-removed)")
    add_config_arg(cln)

    args = parser.parse_args()
    config_path = _resolve_config_path(args.config, repo_root, default_config)
    if not args.command:
        parser.print_help()
        return

    config = DRBConfig.from_yaml(config_path, repo_root=repo_root)

    if args.command == "prepare":
        runtime_assets_dir = Path(args.runtime_assets_dir) if args.runtime_assets_dir else None
        if runtime_assets_dir is not None:
            config.runtime_assets.output_dir = str(runtime_assets_dir)
        build_runtime_assets(config.to_swe_runner_config())
        return

    if args.command == "run":
        try:
            _apply_batch_overrides(
                config,
                task_timeout_seconds=args.task_timeout_seconds,
                agent_timeout_seconds=args.agent_timeout_seconds,
                parallelism=args.parallelism,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if args.gate_required is not None:
            config.gate_required = args.gate_required

        runtime_assets_dir = repo_root / config.runtime_assets.output_dir
        should_prepare = args.do_prepare or (
            not args.dry_run and runtime_assets_need_rebuild(
                config.to_swe_runner_config(), runtime_assets_dir
            )
        )
        if should_prepare:
            _log("Preparing runtime assets...")
            build_runtime_assets(config.to_swe_runner_config())

        tasks = _load_tasks(args, repo_root)
        tasks = filter_tasks(
            tasks,
            sample=args.sample,
            skip=max(0, args.skip),
            instance_ids=parse_instance_ids(args.instance_ids),
        )

        if args.sample is not None and args.sample > 0 and len(tasks) < args.sample:
            _log(
                f"ERROR: --sample {args.sample} requires {args.sample} matching "
                f"tasks, but the selected source contains only {len(tasks)}."
            )
            sys.exit(1)

        if not tasks:
            _log(
                "ERROR: no tasks loaded. Provide --dataset, --tasks, or run "
                "`python -m deep_research_bench.discover` first."
            )
            sys.exit(1)

        _log(f"Loaded {len(tasks)} tasks")
        timeout_label = (
            f"{config.batch.task_timeout_seconds}s"
            if config.batch.task_timeout_seconds > 0
            else "disabled"
        )
        _log(f"Per-task timeout: {timeout_label}")

        if args.dry_run:
            for i, task in enumerate(tasks):
                _log(f"  [{i+1}] {task.instance_id}  {task.problem_statement[:120]}...")
            return

        _require_llm_api_key(config)

        report = run_batch(config, tasks, runtime_assets_dir, export_after=args.export)
        _print_report_json(report, enabled=args.print_report_json)
        if report.failed > 0:
            sys.exit(1)

    elif args.command == "cleanup":
        _log("Containers are auto-removed (--rm). Nothing to clean up.")


if __name__ == "__main__":
    main()
