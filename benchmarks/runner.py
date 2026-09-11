from __future__ import annotations

import concurrent.futures
import json
import os
import platform
import time
import uuid
from pathlib import Path

from .bootstrap import ROOT
from .adapters import Task
from clawtune_kb import initialize_state, validate_seed
from clawtune_kb.store import committed_state, digest, write_json


def _failure_result(task: Task, output: Path, exc: Exception):
    from swe_rebench.docker import ContainerResult

    return ContainerResult(
        task_id=task.task_id,
        image=task.image,
        exit_code=-1,
        error=str(exc),
        trace_dir=output / "traces" / task.directory_name,
        trace_files=sorted(
            (output / "traces" / task.directory_name).glob("*.jsonl")
        ),
    )


def _observed_generation(path: Path) -> int | None:
    """Read the atomic compatibility state without racing generation cleanup."""
    try:
        value = json.loads((path / "state.json").read_text(encoding="utf-8"))
        generation = value.get("generation")
        return generation if isinstance(generation, int) else None
    except (OSError, ValueError):
        return None


def run(
    tasks: list[Task],
    *,
    config_path: Path,
    seed: Path,
    output: Path | None = None,
    resume: Path | None = None,
    task_timeout: int | None = None,
    agent_timeout: int | None = None,
    parallelism: int | None = None,
) -> dict:
    from swe_rebench.config import RunnerConfig
    from swe_rebench.prepare import build_runtime_assets
    from swe_rebench import host_openclaw as host
    from swe_rebench.runner import _result_dict, _required_telemetry_error
    from .runtime import execute, flush_all_kb_updates
    from clawtune_kb.contracts import validate

    if not tasks or len({task.benchmark for task in tasks}) != 1:
        raise ValueError(
            "a simulation run requires tasks from exactly one benchmark"
        )
    if platform.system() != "Linux":
        raise RuntimeError(
            "Live benchmarks require Linux, OpenClaw, Docker and eBPF; "
            "--dry-run works here"
        )

    validate_seed(seed)
    config = RunnerConfig.from_yaml(config_path, repo_root=ROOT)
    if not config.llm.api_key:
        raise ValueError("LLM_API_KEY or config llm.api_key_file is required")

    config.runtime.mode = "host-openclaw"
    config.runtime.kb_frozen = False
    config.runtime.ebpf_required = tasks[0].kind == "repository"
    config.batch.retry_failed = 0
    selected_parallelism = (
        config.batch.parallelism if parallelism is None else parallelism
    )
    if (
        isinstance(selected_parallelism, bool)
        or not isinstance(selected_parallelism, int)
        or selected_parallelism < 1
    ):
        raise ValueError("parallelism must be a positive integer")
    config.batch.parallelism = selected_parallelism

    for value in (task_timeout, agent_timeout):
        if value is not None and value < 0:
            raise ValueError("timeouts must be nonnegative")
    if task_timeout is not None:
        config.batch.task_timeout_seconds = task_timeout
    if agent_timeout is not None:
        config.batch.agent_timeout_seconds = agent_timeout

    folder = (
        resume
        or output
        or ROOT
        / ".runtime"
        / "benchmarks"
        / tasks[0].benchmark
        / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    ).resolve()
    reference = Path(
        os.getenv("AGENT_TEST_BENCH_ROOT", str(ROOT.parent / "agent-test-bench"))
    ).resolve()
    protected = [
        reference,
        seed.resolve(),
        *[
            Path(task.payload["task_path"]).resolve()
            for task in tasks
            if task.kind == "terminal"
        ],
    ]
    if any(folder.is_relative_to(path) for path in protected):
        raise ValueError(
            "run output must be outside the read-only reference, tasks and seed"
        )
    if folder.exists() and resume is None:
        raise FileExistsError(f"run output already exists: {folder}")
    folder.mkdir(parents=True, exist_ok=resume is not None)

    config.output.trace_root = folder / "traces"
    config.output.report_path = folder / "report.json"
    config.output.flat_export_dir = None
    if resume is None:
        initialize_state(
            folder / "kb",
            seed,
            owner=f"benchmark:{tasks[0].benchmark}:{folder.name}",
        )
    config.kb_owner = committed_state(folder / "kb")["owner"]

    manifest = {
        "schema": "clawtune.benchmark-run.v1",
        "benchmark": tasks[0].benchmark,
        "mode": "online",
        "status": "preparing",
        "seed_sha256": digest(seed / "manifest.json"),
        "task_order": [task.task_id for task in tasks],
        "results": [],
        "environment": {
            "platform": platform.platform(),
            "model": config.llm.model,
        },
        "kb_path": str(folder / "kb"),
        "kb_flush_complete": False,
        "official_score": None,
        "parallelism": selected_parallelism,
        "tasks": [task.to_dict() for task in tasks],
        "config_sha256": digest(config_path),
    }

    if resume is not None:
        manifest = json.loads(
            (folder / "run.json").read_text(encoding="utf-8")
        )
        if manifest.get("tasks") != [task.to_dict() for task in tasks]:
            raise ValueError("resume requires exactly the saved tasks")
        active = manifest.get("active_tasks") or (
            [manifest["active_task"]] if manifest.get("active_task") else []
        )
        if active:
            raise ValueError(
                "run has interrupted tasks with possible partial learning; "
                "start a new run to avoid duplicate learning"
            )
        if (
            manifest.get("config_sha256") != digest(config_path)
            or manifest.get("seed_sha256")
            != digest(seed / "manifest.json")
        ):
            raise ValueError("resume requires the original config and seed")
        stored_parallelism = int(manifest.get("parallelism", 1))
        if parallelism is not None and parallelism != stored_parallelism:
            raise ValueError("resume requires the original parallelism")
        selected_parallelism = stored_parallelism
        config.batch.parallelism = stored_parallelism
        completed = [row["task_id"] for row in manifest["results"]]
        if len(completed) != len(set(completed)):
            raise ValueError("saved results contain duplicate task IDs")
        expected = {task.task_id for task in tasks}
        if any(task_id not in expected for task_id in completed):
            raise ValueError("saved results do not match the task list")
        tasks = [task for task in tasks if task.task_id not in set(completed)]
        if not tasks:
            if (
                "kb_flush_complete" in manifest
                and manifest["kb_flush_complete"] is not True
            ):
                raise ValueError(
                    "run ended before its final KB durability barrier; "
                    "start a new run to avoid losing or duplicating learning"
                )
            return manifest

    validate(manifest, "benchmark-run.schema.json")
    write_json(folder / "run.json", manifest)
    print(
        f"{manifest['benchmark']}: {len(tasks)} tasks; "
        f"parallelism={selected_parallelism}; asynchronous online learning; "
        f"KB={folder / 'kb'}",
        flush=True,
    )

    sidecar = None

    def execute_task(task: Task):
        before = _observed_generation(folder / "kb")
        try:
            result = execute(task, config, assets, folder, port)
        except Exception as exc:
            result = _failure_result(task, folder, exc)
        after = _observed_generation(folder / "kb")
        return result, before, after

    def record_result(task: Task, future) -> None:
        result, before, after = future.result()
        write_json(
            folder / "traces" / task.directory_name / "dataset-task.json",
            {
                "benchmark": task.benchmark,
                "instance_id": task.task_id,
                "repo": task.repo or None,
                "category": task.group,
            },
        )
        row = _result_dict(result)
        if task.kind == "repository":
            error = _required_telemetry_error(config, row)
            if error:
                row["error"] = row.get("error") or error
        else:
            row.pop("smoke", None)
            row.pop("agent_diagnostics", None)
            if not row["resource_summary"].get("tool_span_ends"):
                row["error"] = (
                    row.get("error")
                    or "no tool spans: simulation produced no learning observations"
                )
        row.update(
            benchmark=task.benchmark,
            group=task.group,
            official_score=None,
            kb_generation_before=before,
            kb_generation_after=after,
        )
        row["learning_status"] = (
            "shared_kb_progress_observed"
            if before is not None and after is not None and after > before
            else "no_shared_kb_commit_observed_during_task"
        )
        manifest["results"].append(row)
        print(
            f"{task.task_id}: exit={result.exit_code}, "
            f"observed KB {before} -> {after}",
            flush=True,
        )

    try:
        assets = build_runtime_assets(config)
        if not isinstance(assets, Path):
            assets = ROOT / config.runtime_assets.output_dir
        (folder / "sidecar").mkdir(exist_ok=resume is not None)
        port = host._free_port()
        sidecar = host._start_sidecar(
            trace_dir=folder / "sidecar",
            port=port,
            config=config,
            workspace=folder / "workspaces",
            repo=manifest["benchmark"],
            artifact_dir=folder / "kb",
            sandbox_container_prefix="",
        )
        manifest["status"] = "running"

        task_iter = iter(tasks)
        futures: dict[concurrent.futures.Future, Task] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=selected_parallelism,
            thread_name_prefix="clawtune-benchmark",
        ) as executor:
            initial_tasks: list[Task] = []
            while len(initial_tasks) < selected_parallelism:
                task = next(task_iter, None)
                if task is None:
                    break
                initial_tasks.append(task)
            # Persist ownership before a worker can produce an observation.
            # A crash between this write and submit is conservatively
            # non-resumable, which is safer than duplicating partial learning.
            manifest["active_tasks"] = [task.task_id for task in initial_tasks]
            write_json(folder / "run.json", manifest)
            for task in initial_tasks:
                futures[executor.submit(execute_task, task)] = task

            while futures:
                done, _ = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                replacements: list[Task] = []
                for future in done:
                    task = futures.pop(future)
                    record_result(task, future)
                    replacement = next(task_iter, None)
                    if replacement is not None:
                        replacements.append(replacement)
                manifest["active_tasks"] = [
                    task.task_id for task in futures.values()
                ] + [task.task_id for task in replacements]
                write_json(folder / "run.json", manifest)
                for task in replacements:
                    futures[executor.submit(execute_task, task)] = task

        # Per-task drains wait only for runtime-local finalizers. Persistence is
        # deliberately coalesced by the sidecar's single writer and forced once
        # here, after all producers have finished.
        flush_all_kb_updates(port)
        manifest["kb_flush_complete"] = True
        manifest["kb_final_generation"] = committed_state(
            folder / "kb"
        )["generation"]
        manifest.pop("active_tasks", None)
        manifest["status"] = (
            "completed"
            if all(
                not row.get("error") and row["exit_code"] == 0
                for row in manifest["results"]
            )
            else "failed"
        )
    except BaseException as exc:
        manifest["status"] = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        )
        manifest["error"] = str(exc)
        raise
    finally:
        try:
            if sidecar is not None:
                host._stop_process(sidecar)
        finally:
            write_json(folder / "run.json", manifest)
            write_json(folder / "report.json", manifest)

    return manifest
