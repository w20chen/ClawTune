"""
Host OpenClaw + very basic Docker sandbox runner for Deep Research Bench.

Topology mirrors swe_rebench host-openclaw-sandbox: OpenClaw, the plugin, and
the scheduler sidecar run on the host; the agent's tools execute inside a
single very basic Docker sandbox image (no SWE-Bench task image, no /testbed
export).  DeepResearchBench tasks are research QA, so telemetry is the
read/edit/web-tool style (sandbox-container / per-PID docker-exec) rather than
Stage-2 exec clause telemetry.

The OpenClaw onboarding, plugin staging, sidecar lifecycle, and agent argv
helpers are reused from ``swe_rebench.host_sandbox`` (they take a
swe-rebench ``RunnerConfig``, which :meth:`DRBConfig.to_swe_runner_config`
provides).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from swe_rebench.config import RunnerConfig
from swe_rebench.docker import (
    ContainerResult,
    get_docker_client,
    local_image_available,
    pull_image,
)
from swe_rebench.host_sandbox import (
    TaskDeadlineExceeded,
    _configure_openclaw,
    _free_port,
    _install_sandbox_launcher,
    _make_sandbox_workspace_writable,
    _openclaw_env,
    _read_json_object,
    _remaining_task_seconds,
    _require_executable,
    _reset_directory,
    _run_openclaw_agent,
    _start_sidecar,
    _stop_process,
    _tail_text,
    _task_deadline,
    _write_result_summary,
    _write_text,
    _write_timeout_record,
)

from deep_research_bench.config import DRBConfig
from deep_research_bench.prompt import render_drb_prompt
from deep_research_bench.task_source import DRBTask, task_to_swe_taskdef

_BENCHMARK_GATEWAY_ID = "deep-research-bench"


def run_drb_task(
    *,
    task: DRBTask,
    trace_dir: Path,
    config: DRBConfig,
    swe_cfg: RunnerConfig,
    bundle_dir: Path,
    sidecar_port: int | None = None,
) -> ContainerResult:
    """Run one DeepResearchBench task via host OpenClaw + basic sandbox."""
    started = time.monotonic()
    deadline = _task_deadline(swe_cfg, started)
    trace_dir.mkdir(parents=True, exist_ok=True)
    workspace = _drb_workspace(config, task)
    openclaw_home = trace_dir / "openclaw-home"
    sidecar = None
    exit_code = -1
    error: str | None = None
    try:
        _remaining_task_seconds(deadline, phase="workspace reset")
        _reset_directory(workspace, deadline=deadline)
        _reset_directory(openclaw_home, deadline=deadline)
        _make_sandbox_workspace_writable(workspace)
        # Exec (if the model calls it) runs through claw-launch in the basic
        # container; install the launcher runtime into the host workspace.
        _install_sandbox_launcher(workspace, bundle_dir)
        _write_drb_task_inputs(trace_dir, task, config, workspace)
        _apply_web_search_key(config)
        _ensure_basic_image(config, swe_cfg)
        _remaining_task_seconds(deadline, phase="agent setup")
        sidecar_port = sidecar_port or _free_port()
        sidecar = _start_sidecar(
            trace_dir=trace_dir,
            port=sidecar_port,
            config=swe_cfg,
            workspace=workspace,
            repo="deep-research-bench",
            deadline=deadline,
        )
        _configure_openclaw(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            sidecar_port=sidecar_port,
            workspace=workspace,
            sandbox_image=config.sandbox.image,
            config=swe_cfg,
            deadline=deadline,
        )
        _pin_web_search_provider(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            sidecar_port=sidecar_port,
            config=config,
            swe_cfg=swe_cfg,
            workspace=workspace,
        )
        _remaining_task_seconds(deadline, phase="agent execution")
        swe_task = task_to_swe_taskdef(task, config.sandbox.image)
        exit_code = _run_openclaw_agent(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            workspace=workspace,
            sidecar_port=sidecar_port,
            task=swe_task,
            config=swe_cfg,
            task_deadline=deadline,
            post_sandbox_scope=True,
        )
        timeout_record = _read_json_object(trace_dir / "task-timeout.json")
        if exit_code == 124 and isinstance(timeout_record, dict):
            error = str(timeout_record.get("message") or "task timed out")
    except TaskDeadlineExceeded as exc:
        exit_code = 124
        error = str(exc)
        _write_timeout_record(
            trace_dir,
            scope="task",
            message=error,
            configured_seconds=config.batch.task_timeout_seconds,
        )
    except Exception as exc:
        error = str(exc)
        _write_text(trace_dir / "drb_host_error.txt", traceback.format_exc())
    finally:
        if sidecar is not None:
            try:
                _stop_process(sidecar)
            except Exception as exc:
                if error is None:
                    error = f"sidecar cleanup failed: {exc}"
        _write_result_summary(
            trace_dir,
            task_to_swe_taskdef(task, config.sandbox.image),
            workspace,
            exit_code,
            error,
        )
    return ContainerResult(
        task_id=task.instance_id,
        image=config.sandbox.image,
        exit_code=exit_code,
        error=error,
        trace_dir=trace_dir,
        trace_files=sorted(trace_dir.glob("*.jsonl")),
        duration_seconds=time.monotonic() - started,
    )


def _drb_workspace(config: DRBConfig, task: DRBTask) -> Path:
    safe_id = task.instance_id.replace("/", "_").replace(":", "_")
    return config.output.trace_root.parent / "workspaces" / safe_id


def _apply_web_search_key(config: DRBConfig) -> None:
    """Expose the configured web-search key to the ``openclaw agent`` process.

    OpenClaw's built-in ``web_search`` runs in the agent runtime on the host,
    not inside the sandbox.  ``_openclaw_env`` copies ``os.environ`` when the
    agent is spawned, so setting ``TAVILY_API_KEY`` here (when one is
    configured and not already present) is sufficient.
    """
    if not config.web_search.enabled or not config.web_search.api_key:
        return
    if not os.environ.get("TAVILY_API_KEY"):
        os.environ["TAVILY_API_KEY"] = config.web_search.api_key


def _web_search_config_patch(config: DRBConfig) -> dict[str, Any] | None:
    """Return the ``tools.web.search`` config patch for this run, or ``None``.

    ``None`` (web search disabled) leaves OpenClaw's config untouched.  A
    ``provider`` of ``""`` or ``"auto"`` enables search but keeps
    auto-detection.
    """
    if not config.web_search.enabled:
        return None
    search: dict[str, Any] = {"enabled": True}
    if config.web_search.provider and config.web_search.provider != "auto":
        search["provider"] = config.web_search.provider
    return {"tools": {"web": {"search": search}}}


def _pin_web_search_provider(
    *,
    trace_dir: Path,
    openclaw_home: Path,
    sidecar_port: int,
    config: DRBConfig,
    swe_cfg: RunnerConfig,
    workspace: Path,
) -> None:
    """Pin the run-scoped OpenClaw config to the configured web provider.

    OpenClaw auto-detects the first *API-backed* provider with a credential
    (Brave has priority over Tavily), so to make Tavily the default for DRB we
    patch ``tools.web.search.provider`` into this task's isolated OpenClaw
    config.
    """
    patch = _web_search_config_patch(config)
    if patch is None:
        return
    env = _openclaw_env(openclaw_home, sidecar_port, swe_cfg, workspace)
    openclaw = _require_executable("openclaw")
    log_path = trace_dir / "web-search-config.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [openclaw, "config", "patch", "--stdin"],
            input=json.dumps(patch),
            stdout=log,
            stderr=log,
            text=True,
            env=env,
            timeout=60,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw_web_search_config_patch_failed exit={result.returncode}: "
            f"{_tail_text(log_path, 2000)}"
        )


def _ensure_basic_image(config: DRBConfig, swe_cfg: RunnerConfig) -> str:
    """Ensure the very basic sandbox image is available locally."""
    image = config.sandbox.image
    client = get_docker_client(swe_cfg.docker)
    if local_image_available(client, image, swe_cfg.docker.platform):
        return image
    if swe_cfg.docker.pull_policy == "never":
        raise RuntimeError(
            f"basic sandbox image {image} is not available locally and "
            "pull_policy=never"
        )
    if not pull_image(
        client,
        image,
        swe_cfg.docker.pull_policy,
        swe_cfg.docker.platform,
    ):
        raise RuntimeError(f"failed to pull basic sandbox image {image}")
    return image


def _write_drb_task_inputs(
    trace_dir: Path,
    task: DRBTask,
    config: DRBConfig,
    workspace: Path,
) -> None:
    """Write the agent prompt, workspace cwd, task manifest, and reference."""
    prompt = render_drb_prompt(task, prompt_template=config.dataset.prompt_template)
    _write_text(trace_dir / "agent_prompt.txt", prompt)
    _write_text(trace_dir / "agent-cwd.txt", str(workspace) + "\n")
    _write_text(
        trace_dir / "task_manifest.json",
        json.dumps(
            {
                "task_id": task.instance_id,
                "benchmark": "deep-research-bench",
                "reference_kind": task.reference_kind,
                "model": config.llm.model,
                "openclaw_model_ref": config.llm.openclaw_model_ref,
                "runtime_mode": config.runtime.mode,
                "sandbox_image": config.sandbox.image,
                "workspace": str(workspace),
                "problem_statement_bytes": len(task.problem_statement),
                "reference_answer_bytes": len(task.reference_answer),
                "topic": task.topic,
                "difficulty": task.difficulty,
                "domain": task.domain,
            },
            indent=2,
        )
        + "\n",
    )
    # Record-only MVP: keep the reference article next to the trace for later
    # offline grading.  No automatic grading is performed.
    if task.reference_answer:
        _write_text(trace_dir / "reference_answer.txt", task.reference_answer)
