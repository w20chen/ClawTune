from __future__ import annotations

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


def run(tasks: list[Task], *, config_path: Path, seed: Path, output: Path | None = None,
        resume: Path | None = None, task_timeout: int | None = None, agent_timeout: int | None = None) -> dict:
    from swe_rebench.config import RunnerConfig
    from swe_rebench.prepare import build_runtime_assets
    from swe_rebench import host_openclaw as host
    from swe_rebench.runner import _result_dict, _required_telemetry_error
    from .runtime import execute
    from clawtune_kb.contracts import validate
    if not tasks or len({task.benchmark for task in tasks}) != 1:
        raise ValueError("a simulation run requires tasks from exactly one benchmark")
    if platform.system() != "Linux":
        raise RuntimeError("Live benchmarks require Linux, OpenClaw, Docker and eBPF; --dry-run works here")
    validate_seed(seed)
    config = RunnerConfig.from_yaml(config_path, repo_root=ROOT)
    if not config.llm.api_key:
        raise ValueError("LLM_API_KEY or config llm.api_key_file is required")
    config.runtime.mode = "host-openclaw"
    config.runtime.kb_frozen = False
    config.runtime.ebpf_required = tasks[0].kind == "repository"
    config.batch.parallelism = 1
    config.batch.retry_failed = 0
    for value in (task_timeout, agent_timeout):
        if value is not None and value < 0:
            raise ValueError("timeouts must be nonnegative")
    if task_timeout is not None:
        config.batch.task_timeout_seconds = task_timeout
    if agent_timeout is not None:
        config.batch.agent_timeout_seconds = agent_timeout
    folder = (resume or output or ROOT / ".runtime" / "benchmarks" / tasks[0].benchmark /
              (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    reference = Path(os.getenv("AGENT_TEST_BENCH_ROOT", str(ROOT.parent / "agent-test-bench"))).resolve()
    protected = [reference, seed.resolve(), *[Path(t.payload["task_path"]).resolve() for t in tasks if t.kind == "terminal"]]
    if any(folder.is_relative_to(path) for path in protected):
        raise ValueError("run output must be outside the read-only reference, tasks and seed")
    if folder.exists() and resume is None:
        raise FileExistsError(f"run output already exists: {folder}")
    folder.mkdir(parents=True, exist_ok=resume is not None)
    config.output.trace_root = folder / "traces"
    config.output.report_path = folder / "report.json"
    config.output.flat_export_dir = None
    if resume is None:
        initialize_state(folder / "kb", seed, owner=f"benchmark:{tasks[0].benchmark}:{folder.name}")
    config.kb_owner = committed_state(folder / "kb")["owner"]
    manifest = {"schema": "clawtune.benchmark-run.v1", "benchmark": tasks[0].benchmark,
        "mode": "online", "status": "preparing", "seed_sha256": digest(seed / "manifest.json"),
        "task_order": [task.task_id for task in tasks], "results": [],
        "environment": {"platform": platform.platform(), "model": config.llm.model},
        "kb_path": str(folder / "kb"), "official_score": None,
        "tasks": [task.to_dict() for task in tasks], "config_sha256": digest(config_path)}
    if resume is not None:
        manifest = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        if manifest.get("tasks") != [task.to_dict() for task in tasks]:
            raise ValueError("resume requires exactly the saved tasks")
        if manifest.get("active_task"):
            raise ValueError("run has an interrupted task with partial learning; start a new run to avoid duplicate learning")
        if manifest.get("config_sha256") != digest(config_path) or manifest.get("seed_sha256") != digest(seed / "manifest.json"):
            raise ValueError("resume requires the original config and seed")
        completed = [row["task_id"] for row in manifest["results"]]
        if completed != [task.task_id for task in tasks[:len(completed)]]:
            raise ValueError("saved results are not a prefix of the task list")
        tasks = tasks[len(completed):]
        if not tasks:
            return manifest
    validate(manifest, "benchmark-run.schema.json")
    write_json(folder / "run.json", manifest)
    print(f"{tasks[0].benchmark}: {len(tasks)} tasks; serial online learning; KB={folder / 'kb'}", flush=True)
    sidecar = None
    try:
        assets = build_runtime_assets(config)
        if not isinstance(assets, Path):
            assets = ROOT / config.runtime_assets.output_dir
        (folder / "sidecar").mkdir(exist_ok=resume is not None)
        port = host._free_port()
        sidecar = host._start_sidecar(trace_dir=folder / "sidecar", port=port, config=config,
            workspace=folder / "workspaces", repo=tasks[0].benchmark, artifact_dir=folder / "kb",
            sandbox_container_prefix="")
        manifest["status"] = "running"
        for task in tasks:
            manifest["active_task"] = task.task_id
            write_json(folder / "run.json", manifest)
            before = committed_state(folder / "kb")
            result = execute(task, config, assets, folder, port)
            write_json(folder / "traces" / task.directory_name / "dataset-task.json",
                       {"benchmark": task.benchmark, "instance_id": task.task_id,
                        "repo": task.repo or None, "category": task.group})
            row = _result_dict(result)
            if task.kind == "repository":
                error = _required_telemetry_error(config, row)
                if error:
                    row["error"] = row.get("error") or error
            else:
                row.pop("smoke", None)
                row.pop("agent_diagnostics", None)
                if not row["resource_summary"].get("tool_span_ends"):
                    row["error"] = row.get("error") or "no tool spans: simulation produced no learning observations"
            row.update(benchmark=task.benchmark, group=task.group, official_score=None,
                       kb_generation_before=before["generation"],
                       kb_generation_after=committed_state(folder / "kb")["generation"])
            row["learning_status"] = "updated" if row["kb_generation_after"] > row["kb_generation_before"] else "no_committed_update"
            manifest["results"].append(row)
            manifest.pop("active_task", None)
            write_json(folder / "run.json", manifest)
            print(f"{task.task_id}: exit={result.exit_code}, KB {row['kb_generation_before']} -> {row['kb_generation_after']}", flush=True)
        manifest["status"] = "completed" if all(not r.get("error") and r["exit_code"] == 0 for r in manifest["results"]) else "failed"
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
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
