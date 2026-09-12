"""Shared native OpenClaw runtime, independent of benchmark selection/splitting."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from .bootstrap import ROOT
from .adapters import Task
from clawtune_kb.store import write_json
from swe_rebench.cancellation import run_command


def execute(task: Task, config, assets: Path, run_dir: Path, port: int):
    from swe_rebench.host_openclaw import run_host_openclaw_task
    from swe_rebench.task_source import TaskDef
    # RunnerConfig carries task-local paths and namespace fields used by the
    # retained host executors. Never mutate the shared instance: concurrent
    # tasks would otherwise overwrite one another's workspace and repo key.
    config = copy.deepcopy(config)
    config.kb_repo = task.benchmark + ":" + task.group
    config.task_directory = task.directory_name
    # Runtime cleanup must wait for this task's executions/finalizers, but a
    # per-task persistence barrier would serialize worker turnover. The common
    # runner performs one global durability barrier after all tasks finish.
    config.flush_kb_on_task_drain = False
    trace = run_dir / "traces" / task.directory_name
    trace.mkdir(parents=True, exist_ok=False)
    if task.kind == "repository":
        return run_host_openclaw_task(task=TaskDef(task.task_id, task.image, task.prompt, task.repo, task.base_commit),
            trace_dir=trace, config=config, runtime_assets_dir=assets, shared_kb_dir=run_dir / "kb",
            sidecar_port=port, shared_sidecar_trace_dir=run_dir / "sidecar", manage_sidecar=False)
    if task.kind == "research":
        from deep_research_bench.config import DRBConfig
        from deep_research_bench.host_runner import run_drb_task
        from deep_research_bench.task_source import DRBTask
        drb = DRBConfig.from_yaml(config.config_path, repo_root=ROOT)
        drb.runtime, drb.output, drb.llm, drb.batch = config.runtime, config.output, config.llm, config.batch
        drb.task_directory = task.directory_name
        return run_drb_task(task=DRBTask(task.task_id, task.prompt, task.payload["reference_answer"]),
            trace_dir=trace, config=drb, swe_cfg=config, runtime_assets_dir=assets, sidecar_port=port,
            shared_kb_dir=run_dir / "kb", shared_sidecar_trace_dir=run_dir / "sidecar")
    return _execute_bridged(task, config, run_dir, port, trace)


def flush_all_kb_updates(port: int, runtime_ids: list[str], *, timeout_seconds: float = 60.0) -> None:
    """Drain every producer before placing one KB durability barrier.

    A worker returning (possibly with a drain error) does not prove that its
    sidecar finalizers have finished. Recheck the actual runtimes, not a
    synthetic runtime with no activity. No workers may be running here.
    """
    from swe_rebench import host_openclaw as host

    if not runtime_ids:
        raise ValueError("final KB barrier requires the task runtime IDs")
    deadline = time.monotonic() + timeout_seconds
    for runtime_id in dict.fromkeys(runtime_ids):
        host._drain_runtime(
            port, runtime_id, gateway_id="swe-rebench",
            timeout_seconds=max(0.0, deadline - time.monotonic()),
            flush_kb=False,
        )
    host._drain_runtime(
        port,
        runtime_ids[-1],
        gateway_id="swe-rebench",
        timeout_seconds=max(0.0, deadline - time.monotonic()),
        flush_kb=True,
    )


def _required_terminal_preflight(config, task, trace: Path, deadline: float | None) -> None:
    """Fail closed before a Terminal agent runs when eBPF telemetry is required.

    The bridged executor has no OpenClaw sandbox entrypoint, so this host
    preflight is the only place that proves the eBPF clause collector works
    before the task's tool calls depend on it.  It writes
    ``tool_resource_preflight_host.json`` into the task trace directory and
    raises when the required collector is unavailable.
    """

    if not (config.runtime.ebpf_required and task.kind == "terminal"):
        return
    from swe_rebench.host_openclaw import _write_host_tool_resource_preflight

    _write_host_tool_resource_preflight(trace, config, deadline=deadline)


def _execute_bridged(task, config, run_dir, port, trace):
    from .backends import BFCLBackend, TerminalBackend
    from .tool_bridge import ToolBridge
    from swe_rebench import host_openclaw as host
    from swe_rebench.task_source import TaskDef
    from swe_rebench.docker import ContainerResult
    started = time.monotonic()
    workspace = run_dir / "workspaces" / task.directory_name
    workspace.mkdir(parents=True)
    home = trace / "openclaw-home"
    runtime_id = host._runtime_id(workspace)
    host._write_runtime_case_map(run_dir / "sidecar", runtime_id, task.task_id)
    deadline = host._task_deadline(config, started)
    _required_terminal_preflight(config, task, trace, deadline)
    backend = BFCLBackend(task, run_dir) if task.kind == "functions" else TerminalBackend(
        task, run_dir, deadline=deadline, platform=config.docker.platform,
        sidecar_port=port, runtime_id=runtime_id, repo=config.kb_repo,
        telemetry_required=config.runtime.ebpf_required)
    manifest = trace / "tool-bridge.json"
    exit_code, error = -1, None
    try:
        with ToolBridge(backend) as bridge:
            bridge.manifest(manifest)
            # The manifest belongs to this OpenClaw process. Passing it through
            # the task-local config avoids a process-global environment race
            # when BFCL or Terminal tasks run concurrently.
            config.benchmark_tools_manifest = str(manifest)
            host._configure_openclaw(trace_dir=trace, openclaw_home=home, sidecar_port=port,
                                     workspace=workspace, config=config, deadline=deadline)
            env = host._openclaw_env(home, port, config, workspace)
            tool_names = [tool["name"] for tool in backend.tools]
            patch = {"agents": {"defaults": {"sandbox": {"mode": "off"}}},
                     "tools": {"allow": tool_names},
                     "plugins": {"entries": {"clawtune": {"config": {"executionBackend": "hook-only", "instrumentTools": []}}}}}
            run_command([host._require_executable("openclaw"), "config", "patch", "--stdin"],
                input=json.dumps(patch), env=env, text=True, check=True, capture_output=True,
                timeout=host._remaining_task_seconds(deadline, phase="task tools setup"))
            if backend.system:
                (workspace / "AGENTS.md").write_text("\n\n".join(backend.system), encoding="utf-8")
            turn_cfg = copy.deepcopy(config)
            # Each process is a turn in the same persistent OpenClaw session.
            turn_cfg.agent.extra_args = [*config.agent.extra_args, "--session-id", task.directory_name]
            if task.kind == "terminal":
                deadline = backend.start_agent()
            for index, prompt in enumerate(backend.turns):
                prompt_path = trace / f"turn-{index}.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                exit_code = host._run_openclaw_agent(trace_dir=trace, openclaw_home=home, workspace=workspace,
                    sidecar_port=port, task=TaskDef(task.task_id, "", prompt, f"{task.benchmark}:{task.group}"),
                    config=turn_cfg, task_deadline=deadline, post_sandbox_scope=False, prompt_path=prompt_path)
                # The runtime logger uses fixed names; preserve each turn's logs.
                for name in ("agent-stdout.txt", "agent-stderr.txt"):
                    if (trace / name).exists():
                        (trace / name).rename(trace / f"turn-{index}-{name}")
                if exit_code != 0:
                    break
    finally:
        manifest.unlink(missing_ok=True)
        try:
            quiesce = getattr(backend, "quiesce", None)
            if quiesce is not None:
                quiesce()
            host._drain_runtime(
                port,
                runtime_id,
                gateway_id="swe-rebench",
                flush_kb=False,
            )
        finally:
            try:
                host._collect_runtime_traces(run_dir / "sidecar", trace, runtime_id, task_label=task.directory_name)
            finally:
                backend.close()
    return ContainerResult(task_id=task.task_id, image=task.image, exit_code=exit_code, error=error,
        trace_dir=trace, trace_files=sorted(trace.glob("*.jsonl")), duration_seconds=time.monotonic() - started)
