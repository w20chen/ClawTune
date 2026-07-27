"""
SWE-Rebench Batch Runner
========================

Orchestrates batch execution of swe-rebench tasks with OpenClaw + sidecar
trace collection inside Docker containers.

Usage::

    # 1. Prepare the runtime bundle (once)
    python -m swe_rebench.runner prepare

    # 2. Run tasks from a swe-bench dataset
    python -m swe_rebench.runner run --dataset ./swe-bench.json --sample 5

    # 3. Collect and export traces
    python -m swe_rebench.runner collect

    # 4. Clean up
    python -m swe_rebench.runner cleanup

Or all-in-one::

    python -m swe_rebench.runner run \\
        --prepare \\
        --dataset ./swe-bench.json \\
        --sample 10 \\
        --export
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swe_rebench.config import RunnerConfig
from swe_rebench.docker import ContainerResult, get_docker_client, pull_image, run_container
from swe_rebench.host_sandbox import (
    _chmod_and_retry,
    _reset_directory_with_docker,
    run_host_sandbox_task,
)
from swe_rebench.prepare import build_bundle, bundle_needs_rebuild
from swe_rebench.task_source import (
    TaskDef,
    create_single_task,
    filter_tasks,
    load_tasks_from_swebench_dataset,
    parse_instance_ids,
)


# ── Report helpers ────────────────────────────────────────────────

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config_path,
            "total_tasks": self.total_tasks,
            "completed": self.completed,
            "failed": self.failed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 1),
            "results": self.results,
        }


def _result_dict(r: ContainerResult) -> dict[str, Any]:
    trace_inspection = [_inspect_trace(tf, r.task_id) for tf in r.trace_files]
    resource_summary = _resource_summary(trace_inspection)
    artifacts = _task_artifacts(r.trace_dir)
    smoke = _smoke_summary(artifacts)
    return {
        "task_id": r.task_id,
        "image": r.image,
        "exit_code": r.exit_code,
        "error": r.error,
        "trace_dir": str(r.trace_dir) if r.trace_dir else None,
        "trace_files": [str(tf) for tf in r.trace_files],
        "trace_lines": sum(_count_lines(tf) for tf in r.trace_files),
        "trace_inspection": trace_inspection,
        "resource_summary": resource_summary,
        "artifacts": artifacts,
        "smoke": smoke,
        "duration_seconds": round(r.duration_seconds, 1),
    }


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for _ in open(path, encoding="utf-8"))
    except Exception:
        return 0


def _inspect_trace(path: Path, task_id: str) -> dict[str, Any]:
    """Return lightweight sanity checks for an OpenClaw trace export."""
    report: dict[str, Any] = {
        "path": str(path),
        "line_count": 0,
        "record_types": {},
        "has_task_id": False,
        "has_tool_span": False,
        "has_llm_span": False,
        "tool_span_ends": 0,
        "launcher_tool_span_ends": 0,
        "launcher_cgroup_tool_span_ends": 0,
        "launcher_attributed_tool_span_ends": 0,
        "unattributed_launcher_tool_span_ends": 0,
        "cgroup_tool_span_ends": 0,
        "process_tree_tool_span_ends": 0,
        "attributed_tool_span_ends": 0,
        "failed_tool_span_ends": 0,
        "status_exit_code_disagreements": 0,
        "launcher_tool_resource_span_ends": 0,
        "launcher_tool_resource_eligible_span_ends": 0,
        "warnings": [],
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report["warnings"].append(f"cannot read trace: {exc}")
        return report

    report["line_count"] = len(lines)
    for line in lines:
        if not line.strip():
            continue
        if task_id and task_id in line:
            report["has_task_id"] = True
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            report["warnings"].append("invalid JSON line")
            continue
        record_type = record.get("record_type") or record.get("type") or "unknown"
        record_types = report["record_types"]
        record_types[record_type] = record_types.get(record_type, 0) + 1
        span_name = str(record.get("name") or "")
        kind = str(record.get("kind") or "")
        if kind == "tool" or "tool" in span_name or record.get("action_type") == "tool_exec":
            report["has_tool_span"] = True
            if record_type == "span_end":
                report["tool_span_ends"] += 1
                status_code = _nested_get(record, ("status", "code"))
                output_exit_code = _extract_trace_exit_code(record.get("output"))
                resources = record.get("resources") if isinstance(record.get("resources"), dict) else {}
                if resources.get("scope") == "cgroup":
                    report["cgroup_tool_span_ends"] += 1
                if resources.get("scope") == "process_tree":
                    report["process_tree_tool_span_ends"] += 1
                if resources.get("attribution_status") in {"attributed", "partially_attributed"}:
                    report["attributed_tool_span_ends"] += 1
                if status_code == "ok" and output_exit_code not in (None, 0):
                    report["status_exit_code_disagreements"] += 1
                if status_code in {"error", "timeout", "cancelled"} or output_exit_code not in (None, 0):
                    report["failed_tool_span_ends"] += 1
                if _nested_get(record, ("execution", "mode")) == "launcher":
                    report["launcher_tool_span_ends"] += 1
                    tool_resource = _nested_get(record, ("execution", "tool_resource"))
                    if isinstance(tool_resource, dict):
                        report["launcher_tool_resource_span_ends"] += 1
                        if tool_resource.get("kb_observations_added", 0):
                            report["launcher_tool_resource_eligible_span_ends"] += 1
                    if resources.get("scope") == "cgroup":
                        report["launcher_cgroup_tool_span_ends"] += 1
                    if resources.get("attribution_status") in {"attributed", "partially_attributed"}:
                        report["launcher_attributed_tool_span_ends"] += 1
                    if (
                        resources.get("attribution_status") == "unattributed"
                    ):
                        report["unattributed_launcher_tool_span_ends"] += 1
        if kind == "llm" or "model" in span_name or record.get("action_type") == "llm_call":
            report["has_llm_span"] = True

    if not report["has_task_id"]:
        report["warnings"].append("trace does not contain TASK_INSTANCE_ID")
    if not report["has_tool_span"]:
        report["warnings"].append("trace has no tool span/action")
    if report["launcher_tool_span_ends"] and not report["launcher_attributed_tool_span_ends"]:
        report["warnings"].append("launcher tool spans have no resource attribution")
    if report["launcher_tool_span_ends"] and not report["launcher_cgroup_tool_span_ends"]:
        report["warnings"].append("launcher tool spans have no cgroup resource samples")
    if report["failed_tool_span_ends"]:
        report["warnings"].append("trace contains failed tool spans")
    if report["status_exit_code_disagreements"]:
        report["warnings"].append("tool span status disagrees with non-zero exit code")
    if report["launcher_tool_span_ends"] and not report["launcher_tool_resource_span_ends"]:
        report["warnings"].append("launcher tool spans have no Stage-2 tool-resource telemetry")
    return report


def _resource_summary(trace_inspection: list[dict[str, Any]]) -> dict[str, Any]:
    tool_span_ends = sum(int(item.get("tool_span_ends", 0)) for item in trace_inspection)
    launcher_tool_span_ends = sum(int(item.get("launcher_tool_span_ends", 0)) for item in trace_inspection)
    launcher_cgroup_tool_span_ends = sum(
        int(item.get("launcher_cgroup_tool_span_ends", 0))
        for item in trace_inspection
    )
    launcher_attributed_tool_span_ends = sum(
        int(item.get("launcher_attributed_tool_span_ends", 0))
        for item in trace_inspection
    )
    attributed_tool_span_ends = sum(int(item.get("attributed_tool_span_ends", 0)) for item in trace_inspection)
    cgroup_tool_span_ends = sum(int(item.get("cgroup_tool_span_ends", 0)) for item in trace_inspection)
    unattributed_launcher_tool_span_ends = sum(
        int(item.get("unattributed_launcher_tool_span_ends", 0))
        for item in trace_inspection
    )
    launcher_tool_resource_span_ends = sum(
        int(item.get("launcher_tool_resource_span_ends", 0))
        for item in trace_inspection
    )
    launcher_tool_resource_eligible_span_ends = sum(
        int(item.get("launcher_tool_resource_eligible_span_ends", 0))
        for item in trace_inspection
    )
    return {
        "tool_span_ends": tool_span_ends,
        "launcher_tool_span_ends": launcher_tool_span_ends,
        "launcher_attributed_tool_span_ends": launcher_attributed_tool_span_ends,
        "launcher_cgroup_tool_span_ends": launcher_cgroup_tool_span_ends,
        "launcher_tool_resource_span_ends": launcher_tool_resource_span_ends,
        "launcher_tool_resource_eligible_span_ends": launcher_tool_resource_eligible_span_ends,
        "attributed_tool_span_ends": attributed_tool_span_ends,
        "cgroup_tool_span_ends": cgroup_tool_span_ends,
        "unattributed_launcher_tool_span_ends": unattributed_launcher_tool_span_ends,
        "cgroup_coverage_ratio": (
            round(launcher_cgroup_tool_span_ends / launcher_tool_span_ends, 3)
            if launcher_tool_span_ends
            else None
        ),
        "launcher_attribution_ratio": (
            round(launcher_attributed_tool_span_ends / launcher_tool_span_ends, 3)
            if launcher_tool_span_ends
            else None
        ),
        "launcher_tool_resource_ratio": (
            round(launcher_tool_resource_span_ends / launcher_tool_span_ends, 3)
            if launcher_tool_span_ends
            else None
        ),
    }


def _nested_get(value: dict[str, Any], keys: tuple[str, ...]) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _extract_trace_exit_code(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    direct = value.get("exit_code")
    if isinstance(direct, int):
        return direct
    result = value.get("result")
    if not isinstance(result, dict):
        return None
    for key in ("exit_code", "exitCode"):
        item = result.get(key)
        if isinstance(item, int):
            return item
    details = result.get("details")
    if isinstance(details, dict):
        for key in ("exit_code", "exitCode"):
            item = details.get(key)
            if isinstance(item, int):
                return item
    return None


def _task_artifacts(trace_dir: Path | None) -> dict[str, Any]:
    """Summarize smoke-test artifacts emitted by the task container."""
    if trace_dir is None:
        return {}
    result: dict[str, Any] = {}
    for name in (
        "task_manifest.json",
        "agent-cwd.txt",
        "agent_prompt.txt",
        "agent-stdout.txt",
        "agent-stderr.txt",
        "repo_status.txt",
        "model.patch",
        "result_summary.json",
        "cgroup_probe.json",
        "tool_resource_preflight.json",
        "phase3.log",
        "sidecar.log",
        "container.log",
    ):
        path = trace_dir / name
        if not path.exists():
            continue
        item: dict[str, Any] = {
            "path": str(path),
            "bytes": path.stat().st_size,
        }
        if name == "model.patch":
            item["has_diff"] = path.stat().st_size > 0
        if name == "result_summary.json":
            try:
                item["summary"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                item["warning"] = f"cannot parse result summary: {exc}"
        if name in {"agent-cwd.txt", "agent-stdout.txt", "agent-stderr.txt", "repo_status.txt"}:
            item["preview"] = _preview_text(path)
        result[name] = item
    proxy_debug = sorted(trace_dir.glob("llm_proxy_debug_*.json"))
    if proxy_debug:
        result["llm_proxy_debug"] = [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "preview": _preview_text(path),
            }
            for path in proxy_debug[-3:]
        ]
    return result


def _preview_text(path: Path, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def _smoke_summary(artifacts: dict[str, Any]) -> dict[str, Any]:
    result_summary = (
        artifacts.get("result_summary.json", {})
        .get("summary", {})
    )
    has_patch = bool(result_summary.get("has_patch"))
    testbed_exists = bool(result_summary.get("testbed_exists"))
    agent_exit_code = result_summary.get("agent_exit_code")
    cwd = artifacts.get("agent-cwd.txt", {}).get("preview", "").strip()
    return {
        "success": has_patch,
        "reason": "patch produced" if has_patch else "no patch produced",
        "agent_exit_code": agent_exit_code,
        "testbed_exists": testbed_exists,
        "agent_cwd": cwd,
        "has_patch": has_patch,
        "patch_bytes": result_summary.get("patch_bytes", 0),
    }


# ── Lock for thread-safe logging ──────────────────────────────────

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


# ── Core orchestration ────────────────────────────────────────────

def run_batch(
    config: RunnerConfig,
    tasks: list[TaskDef],
    bundle_dir: Path,
    *,
    export_after: bool = False,
) -> BatchReport:
    """Run a batch of tasks and return a summary report."""
    _log(f"\n{'='*60}")
    _log(f"Batch run: {len(tasks)} tasks, parallelism={config.batch.parallelism}")
    _log(f"Trace root: {config.output.trace_root}")
    _log(f"{'='*60}\n")

    client = get_docker_client(config.docker)
    report = BatchReport(
        config_path="",
        total_tasks=len(tasks),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    start_wall = time.monotonic()

    # Pre-pull images in parallel (best-effort)
    _pre_pull_images(client, tasks, config.docker.pull_policy)

    max_workers = max(1, config.batch.parallelism)
    completed_count = 0
    failed_count = 0
    futures_map: dict[Any, TaskDef] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for task in tasks:
            trace_dir = _task_trace_dir(config, task)
            future = executor.submit(
                _run_one,
                client, task, bundle_dir, trace_dir, config,
            )
            futures_map[future] = task

        for future in as_completed(futures_map):
            task = futures_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = ContainerResult(
                    task_id=task.instance_id,
                    image=task.image,
                    exit_code=-1,
                    error=str(exc),
                )

            result_dict = _result_dict(result)
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

    report.completed = completed_count
    report.failed = failed_count
    report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.duration_seconds = time.monotonic() - start_wall

    _log(f"\n{'='*60}")
    _log(f"Done: {completed_count} OK, {failed_count} FAIL, {report.duration_seconds:.0f}s total")
    _log(f"{'='*60}\n")

    # Write report
    report_path = config.output.report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _log(f"Report written to {report_path}")

    # Export traces if requested
    if export_after:
        _export_traces(config, report)

    return report


def _run_one(
    client: Any,
    task: TaskDef,
    bundle_dir: Path,
    trace_dir: Path,
    config: RunnerConfig,
) -> ContainerResult:
    """Execute a single task container (called in worker thread)."""
    _reset_task_trace_dir(config.output.trace_root, trace_dir, docker_cleanup_image=task.image)

    if config.runtime.mode == "host-openclaw-sandbox":
        return run_host_sandbox_task(
            task=task,
            trace_dir=trace_dir,
            config=config,
            bundle_dir=bundle_dir,
        )

    retries = config.batch.retry_failed + 1
    last_result: ContainerResult | None = None

    for attempt in range(1, retries + 1):
        if attempt > 1:
            _log(f"[{task.instance_id}] retry {attempt}/{retries}")

        # Pull image if needed
        if not pull_image(client, task.image, config.docker.pull_policy):
            return ContainerResult(
                task_id=task.instance_id, image=task.image,
                exit_code=-1, error=f"Failed to pull image: {task.image}",
                trace_dir=trace_dir,
            )

        result = run_container(
            client=client,
            image=task.image,
            task_id=task.instance_id,
            bundle_dir=bundle_dir,
            trace_dir=trace_dir,
            problem_statement=task.problem_statement,
            config=config.docker,
            llm_api_key=config.llm.api_key,
            llm_upstream_url=config.llm.upstream_base_url,
            llm_model=config.llm.model,
            openclaw_model_ref=config.llm.openclaw_model_ref,
            timeout_seconds=config.batch.task_timeout_seconds,
            env_extra={
                "TASK_BASE_COMMIT": task.base_commit,
                "TASK_HINT_TEXT": task.hint_text,
                **task.extra_env,
            },
        )
        last_result = result

        # Success ── don't retry
        if result.exit_code == 0 and not result.error:
            return result

    return last_result or ContainerResult(
        task_id=task.instance_id, image=task.image,
        exit_code=-1, error="All retries exhausted",
        trace_dir=trace_dir,
    )


def _pre_pull_images(client: Any, tasks: list[TaskDef], policy: str) -> None:
    """Pre-pull all unique images in parallel."""
    unique = list({t.image for t in tasks if t.image})
    if not unique:
        return
    _log(f"Pre-pulling {len(unique)} unique images...")
    with ThreadPoolExecutor(max_workers=min(4, len(unique))) as executor:
        futures = {executor.submit(pull_image, client, img, policy): img for img in unique}
        for future in as_completed(futures):
            img = futures[future]
            try:
                ok = future.result()
                _log(f"  pull {img}: {'OK' if ok else 'FAIL'}")
            except Exception as exc:
                _log(f"  pull {img}: {exc}")


def _task_trace_dir(config: RunnerConfig, task: TaskDef) -> Path:
    """Compute the per-task trace output directory."""
    safe_id = task.instance_id.replace("/", "_").replace(":", "_")
    return config.output.trace_root / safe_id


# ── Trace export ──────────────────────────────────────────────────

def _reset_task_trace_dir(
    trace_root: Path,
    trace_dir: Path,
    *,
    docker_cleanup_image: str | None = None,
) -> None:
    """Remove stale per-task artifacts before a fresh run."""
    root = trace_root.resolve()
    target = trace_dir.resolve()

    if target == root or root not in target.parents:
        raise ValueError(f"refusing to clear trace directory outside trace root: {target}")

    if target.exists():
        try:
            shutil.rmtree(target, onerror=_chmod_and_retry)
        except OSError as exc:
            if not _can_retry_trace_cleanup_with_docker(exc, docker_cleanup_image):
                raise
            _reset_directory_with_docker(target, docker_cleanup_image)
            return
    target.mkdir(parents=True, exist_ok=True)


def _can_retry_trace_cleanup_with_docker(exc: OSError, docker_cleanup_image: str | None) -> bool:
    if docker_cleanup_image is None:
        return False
    return isinstance(exc, PermissionError) or exc.errno in {errno.ENOTEMPTY, 39}


def _require_llm_api_key(config: RunnerConfig) -> None:
    if config.llm.api_key:
        return

    searched = []
    if config.llm.api_key_file is not None:
        searched.append(str(config.llm.api_key_file))
    searched.append(str(config.repo_root / ".env"))
    raise SystemExit(
        "ERROR: LLM API key is not configured. "
        "Set LLM_API_KEY, write the key to swe_rebench/llm_api_key.txt, "
        f"or set llm.api_key_file. Searched: {', '.join(searched)}"
    )


def _export_traces(config: RunnerConfig, report: BatchReport) -> None:
    """Copy trace files into a flat export directory keyed by task ID."""
    export_dir = config.output.flat_export_dir
    if export_dir is None:
        return
    export_dir.mkdir(parents=True, exist_ok=True)
    exported = 0
    for entry in report.results:
        task_id = entry["task_id"]
        trace_files = entry.get("trace_files", [])
        for tf_path_str in trace_files:
            src = Path(tf_path_str)
            if not src.exists():
                continue
            # Name: {task_id}_{original_name}
            dst_name = f"{task_id}_{src.name}"
            dst = export_dir / dst_name
            shutil.copy2(src, dst)
            exported += 1
    _log(f"Exported {exported} trace files to {export_dir}")


def collect_traces(config: RunnerConfig) -> BatchReport:
    """Scan trace_root for existing trace files and export them.

    Does not run containers ── only collects traces from previous runs.
    """
    trace_root = config.output.trace_root
    if not trace_root.exists():
        _log(f"Trace root not found: {trace_root}")
        return BatchReport(config_path="", total_tasks=0)

    results: list[dict[str, Any]] = []
    task_dirs = sorted(trace_root.iterdir())
    for task_dir in task_dirs:
        if not task_dir.is_dir():
            continue
        traces = sorted(task_dir.glob("*.jsonl"))
        if not traces:
            continue
        results.append({
            "task_id": task_dir.name,
            "image": "",
            "exit_code": 0,
            "error": None,
            "trace_dir": str(task_dir),
            "trace_files": [str(t) for t in traces],
            "trace_lines": sum(_count_lines(t) for t in traces),
            "duration_seconds": 0.0,
        })

    report = BatchReport(
        config_path="",
        total_tasks=len(results),
        completed=len(results),
        results=results,
    )
    _log(f"Found {len(results)} task directories with traces")

    if config.output.flat_export_dir:
        _export_traces(config, report)

    report_path = config.output.report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _log(f"Report written to {report_path}")

    return report


# ── CLI ───────────────────────────────────────────────────────────

def _detect_repo_root() -> Path:
    p = Path(__file__).resolve()
    for _ in range(6):
        if (p / "AGENTS.md").exists():
            return p
        p = p.parent
    return Path.cwd()


def _resolve_path(value: str, repo_root: Path) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return repo_root / p


def _resolve_config_path(config_arg: str | None, repo_root: Path, default_config: Path) -> Path:
    if config_arg:
        candidate = Path(config_arg)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.exists():
            return candidate
        example = repo_root / "swe_rebench" / "config.example.yaml"
        if candidate == example:
            raise FileNotFoundError(f"Config file not found: {candidate}")
        _log(f"Warning: config file not found at {candidate}; falling back to example config {example}")
        return example
    return default_config


def main() -> None:
    repo_root = _detect_repo_root()
    default_config = repo_root / "swe_rebench" / "config.example.yaml"

    parser = argparse.ArgumentParser(
        description="SWE-Rebench batch runner with OpenClaw + sidecar trace collection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default=default_config,
        help=f"Path to config YAML (default: {default_config})",
    )
    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # Share --config across all subcommands so it can be placed before
    # OR after the subcommand (argparse limitation workaround).
    def add_config_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config", default=None,
            help=f"Path to config YAML (default: {default_config})",
        )

    # ── prepare ──
    prep = sub.add_parser("prepare", help="Build the runtime bundle")
    add_config_arg(prep)
    prep.add_argument("--bundle-dir", default=None, help="Override bundle output directory")

    # ── run ──
    run_p = sub.add_parser("run", help="Run swe-rebench tasks")
    add_config_arg(run_p)
    run_p.add_argument("--prepare", action="store_true", dest="do_prepare",
                       help="Run prepare step before executing tasks")
    run_p.add_argument("--dataset", default=None,
                       help="Path to swe-bench dataset JSON/JSONL file")
    run_p.add_argument("--tasks", default=None,
                       help="Path to simple JSON task list")
    run_p.add_argument("--image", default=None,
                       help="Single Docker image to run (requires --task-id and --problem)")
    run_p.add_argument("--task-id", default=None, help="Task ID for single-image mode")
    run_p.add_argument("--problem", default=None, help="Problem statement for single-image mode")
    run_p.add_argument("--sample", type=int, default=None,
                       help="Run only the first N selected tasks")
    run_p.add_argument("--skip", type=int, default=0,
                       help="Skip the first N selected tasks before --sample")
    run_p.add_argument("--instance-ids", default=None,
                       help="Comma-separated instance IDs to run, preserving the given order")
    run_p.add_argument("--repo", default=None,
                       help="Run only tasks whose repo field matches this value")
    run_p.add_argument("--parallelism", type=int, default=None,
                       help="Override parallelism from config")
    run_p.add_argument("--runtime-mode", default=None,
                       choices=("container-openclaw", "host-openclaw-sandbox"),
                       help="Override runtime mode from config")
    run_p.add_argument("--export", action="store_true",
                       help="Export traces to flat directory after run")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Print tasks without running containers")

    # ── collect ──
    col = sub.add_parser("collect", help="Collect and export traces from previous runs")
    add_config_arg(col)
    col.add_argument("--export-dir", default=None, help="Override flat export directory")

    # ── cleanup ──
    cln = sub.add_parser("cleanup", help="(No-op: containers are auto-removed)")
    add_config_arg(cln)

    args = parser.parse_args()
    # Resolve --config: subcommand-level arg takes precedence over top-level.
    config_path = _resolve_config_path(args.config, repo_root, default_config)

    if not args.command:
        parser.print_help()
        return

    config = RunnerConfig.from_yaml(config_path, repo_root=repo_root)

    if args.command == "prepare":
        bundle_dir = Path(args.bundle_dir) if args.bundle_dir else None
        if bundle_dir is not None:
            config.bundle.output_dir = str(bundle_dir)
        build_bundle(config)
        return

    if args.command == "run":
        if args.parallelism is not None:
            config.batch.parallelism = args.parallelism
        if args.runtime_mode is not None:
            config.runtime.mode = args.runtime_mode

        # Build bundle if requested or stale.  The plugin runtime lives in
        # ignored dist/ files, so relying on git reset alone is not enough.
        bundle_dir = repo_root / config.bundle.output_dir
        should_prepare = args.do_prepare or (
            not args.dry_run and bundle_needs_rebuild(config, bundle_dir)
        )
        if should_prepare:
            _log("Preparing runtime bundle...")
            build_bundle(config)

        # Load and select tasks
        tasks = _load_tasks(args, repo_root)
        tasks = filter_tasks(
            tasks,
            sample=args.sample,
            skip=max(0, args.skip),
            instance_ids=parse_instance_ids(args.instance_ids),
            repo=args.repo,
        )

        if not tasks:
            _log("ERROR: no tasks loaded.  Provide --dataset, --tasks, or --image.")
            sys.exit(1)

        _log(f"Loaded {len(tasks)} tasks")

        if args.dry_run:
            for i, t in enumerate(tasks):
                _log(f"  [{i+1}] {t.instance_id}  image={t.image}")
                if t.problem_statement:
                    _log(f"       problem: {t.problem_statement[:120]}...")
            return

        _require_llm_api_key(config)

        report = run_batch(config, tasks, bundle_dir, export_after=args.export)

        # Print summary
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        if report.failed > 0:
            sys.exit(1)

    elif args.command == "collect":
        if args.export_dir:
            config.output.flat_export_dir = _resolve_path(args.export_dir, repo_root)
        report = collect_traces(config)
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))

    elif args.command == "cleanup":
        _log("Containers are auto-removed (--rm). Nothing to clean up.")


def _load_tasks(args: argparse.Namespace, repo_root: Path) -> list[TaskDef]:
    """Load tasks from whichever source was specified."""
    # Single image mode
    if args.image:
        task_id = args.task_id or "task-1"
        problem = args.problem or ""
        return [create_single_task(task_id, args.image, problem)]

    # Simple JSON task list
    if args.tasks:
        path = _resolve_path(args.tasks, repo_root)
        if not path.exists():
            raise FileNotFoundError(
                f"Tasks file not found: {path}\n"
                f"Generate one with:\n"
                f"  python -m swe_rebench.discover --out {path}"
            )
        from swe_rebench.task_source import load_tasks_from_simple_list
        return load_tasks_from_simple_list(path)

    # Swe-bench dataset
    if args.dataset:
        path = _resolve_path(args.dataset, repo_root)
        if not path.exists():
            raise FileNotFoundError(
                f"Dataset file not found: {path}\n"
                f"Generate one with:\n"
                f"  python -m swe_rebench.discover --out {path}\n"
                f"  python -m swe_rebench.discover --sample 10 --out {path}\n"
                f"Or use --image for a single task:\n"
                f"  python -m swe_rebench.runner run --image <docker-image> --task-id <id> --problem \"...\""
            )
        return load_tasks_from_swebench_dataset(path)

    default_dataset = _default_agent_test_bench_tasks(repo_root)
    if default_dataset is not None:
        _log(f"Using default SWE-Rebench task source: {default_dataset}")
        return load_tasks_from_swebench_dataset(default_dataset)

    return []


def _default_agent_test_bench_tasks(repo_root: Path) -> Path | None:
    """Find the local agent-test-bench SWE-Rebench tasks file if present."""
    candidates: list[Path] = []
    env_root = os.getenv("AGENT_TEST_BENCH_ROOT")
    if env_root:
        candidates.append(Path(env_root) / "data" / "swe-rebench" / "tasks.json")
    candidates.extend([
        repo_root / "swe_rebench" / "tasks.json",
        repo_root.parent / "agent-test-bench" / "data" / "swe-rebench" / "tasks.json",
    ])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


if __name__ == "__main__":
    main()
