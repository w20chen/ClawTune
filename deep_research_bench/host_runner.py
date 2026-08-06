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
    _TASK_CLEANUP_TIMEOUT_SECONDS,
    _cleanup_openclaw_sandbox_containers,
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
    _verify_sandbox_launcher,
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
        # Managed-wrapper exec runs through claw-launch in the basic sandbox
        # container.  Preflight it so a launcher that is unreadable or not
        # executable in the sandbox fails fast with one clear error instead of
        # surfacing as repeated docker-exec failures during agent execution.
        _verify_sandbox_launcher(
            trace_dir,
            workspace,
            sandbox_image=config.sandbox.image,
            deadline=deadline,
        )
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
        # OpenClaw scopes Docker sandbox containers by workspace prefix and
        # reuses a running container.  A stale container can carry a host
        # workspace cwd that is outside the container mount namespace, which
        # makes every docker exec fail with "current working directory is
        # outside of container mount namespace root -- possible container
        # breakout detected".  Remove stale containers so each task provisions
        # a fresh sandbox container (the same fix SWE-Rebench applies before
        # its agent runs).
        cleanup_budget = _remaining_task_seconds(
            deadline,
            phase="pre-agent sandbox cleanup",
        )
        _cleanup_openclaw_sandbox_containers(
            trace_dir,
            workspace,
            timeout_seconds=min(
                _TASK_CLEANUP_TIMEOUT_SECONDS,
                cleanup_budget or _TASK_CLEANUP_TIMEOUT_SECONDS,
            ),
            strict=True,
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


# Official external web-search provider plugins that OpenClaw ships as
# separate npm packages (``openclaw plugins install <package>``).  DRB runs the
# agent in an isolated ``OPENCLAW_HOME`` that only contains the ClawTune
# ``agent-scheduler`` plugin, so a globally installed provider plugin (e.g.
# Tavily) is invisible there unless the runner links it in.
_WEB_SEARCH_PROVIDER_PACKAGES: dict[str, str] = {
    "tavily": "@openclaw/tavily-plugin",
    "exa": "@openclaw/exa-plugin",
    "firecrawl": "@openclaw/firecrawl-plugin",
    "perplexity": "@openclaw/perplexity-plugin",
    "searxng": "@openclaw/searxng-plugin",
}


def _candidate_openclaw_homes() -> list[Path]:
    """Candidate user OpenClaw homes for discovering globally installed plugins.

    The benchmark runner usually runs as root via ``sudo`` with an isolated
    ``OPENCLAW_HOME``, so globally installed plugins live in the invoking
    user's home (``SUDO_USER``) or, failing that, ``$HOME``.
    """
    homes: list[Path] = []
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            homes.append(Path(os.path.expanduser(f"~{sudo_user}")))
        except (KeyError, RuntimeError):
            pass
    home = os.environ.get("HOME")
    if home:
        homes.append(Path(home))
    return homes


def _discover_web_search_provider_plugin(provider: str) -> Path | None:
    """Locate a globally installed plugin directory for ``provider``.

    OpenClaw installs npm plugins under
    ``<home>/.openclaw/npm/projects/<encoded-package>-<hash>`` (e.g.
    ``openclaw-tavily-plugin-8ad843922d``).  Returns the first matching
    directory that contains a plugin manifest, or ``None``.
    """
    package = _WEB_SEARCH_PROVIDER_PACKAGES.get(provider)
    if not package:
        return None
    base = package.removeprefix("@").replace("/", "-")
    for home in _candidate_openclaw_homes():
        projects = home / ".openclaw" / "npm" / "projects"
        if not projects.is_dir():
            continue
        for candidate in sorted(projects.glob(f"{base}*")):
            if candidate.is_dir() and (
                (candidate / "openclaw.plugin.json").exists()
                or (candidate / "package.json").exists()
            ):
                return candidate
    return None


def _link_web_search_provider_plugin(
    *,
    openclaw: str,
    env: dict[str, str],
    log_path: Path,
    plugin_dir: Path,
    plugin_id: str,
) -> bool:
    """Link a locally installed web provider plugin into the isolated home.

    Mirrors how ``_configure_openclaw`` links the ClawTune plugin (``plugins
    install --link`` then ``plugins enable``).  Returns ``True`` on success.
    This is best-effort: any failure is logged and reported as ``False`` so
    the caller can fall back to auto-detection instead of failing the task.
    """
    try:
        with log_path.open("a", encoding="utf-8") as log:
            result = subprocess.run(
                [openclaw, "plugins", "install", "--link", str(plugin_dir)],
                stdout=log,
                stderr=log,
                text=True,
                env=env,
                timeout=60,
            )
            if result.returncode != 0:
                log.write(
                    f"[warn] link provider plugin failed (exit={result.returncode})\n"
                )
                return False
            result = subprocess.run(
                [openclaw, "plugins", "enable", plugin_id],
                stdout=log,
                stderr=log,
                text=True,
                env=env,
                timeout=60,
            )
            if result.returncode != 0:
                log.write(
                    f"[warn] enable provider plugin failed (exit={result.returncode})\n"
                )
                return False
            return True
    except (OSError, subprocess.SubprocessError) as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[warn] link provider plugin raised: {exc}\n")
        return False


def _run_web_search_config_patch(
    openclaw: str,
    patch: dict[str, Any],
    env: dict[str, str],
    log_path: Path,
) -> subprocess.CompletedProcess:
    """Run ``openclaw config patch --stdin`` once, appending to ``log_path``."""
    with log_path.open("a", encoding="utf-8") as log:
        return subprocess.run(
            [openclaw, "config", "patch", "--stdin"],
            input=json.dumps(patch),
            stdout=log,
            stderr=log,
            text=True,
            env=env,
            timeout=60,
        )


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

    Web search is best-effort, so a missing provider plugin must not fail the
    whole task.  If the pinned provider is not available in this task's
    isolated OpenClaw home (OpenClaw rejects ``tools.web.search.provider``
    with "provider is not available"), we degrade to OpenClaw auto-detection
    instead.  The warning and the host fix (``openclaw plugin install
    <provider>`` or ``openclaw doctor --fix``) are recorded in
    ``web-search-config.log``.
    """
    patch = _web_search_config_patch(config)
    if patch is None:
        return
    env = _openclaw_env(openclaw_home, sidecar_port, swe_cfg, workspace)
    openclaw = _require_executable("openclaw")
    log_path = trace_dir / "web-search-config.log"
    log_path.write_text("", encoding="utf-8")
    pinned_provider = config.web_search.provider
    pinned = bool(pinned_provider and pinned_provider != "auto")
    result = _run_web_search_config_patch(openclaw, patch, env, log_path)
    if result.returncode == 0:
        return
    if pinned:
        # The pinned provider (e.g. tavily) is not available in the isolated
        # OpenClaw home.  First try to link a globally installed plugin for it
        # so web search can actually use the provider; if that is not possible,
        # keep web search best-effort by degrading to auto-detection.
        plugin_dir = _discover_web_search_provider_plugin(pinned_provider)
        if plugin_dir is not None and _link_web_search_provider_plugin(
            openclaw=openclaw,
            env=env,
            log_path=log_path,
            plugin_dir=plugin_dir,
            plugin_id=pinned_provider,
        ):
            result = _run_web_search_config_patch(openclaw, patch, env, log_path)
            if result.returncode == 0:
                return
        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                f"[warn] web search provider '{pinned_provider}' is not "
                "available in this task's isolated OpenClaw home; degrading to "
                "auto-detection. To pin it, install or enable its plugin (e.g. "
                "`openclaw plugin install tavily`) or run `openclaw doctor "
                "--fix`.\n"
            )
        fallback = {"tools": {"web": {"search": {"enabled": True}}}}
        result = _run_web_search_config_patch(openclaw, fallback, env, log_path)
        if result.returncode == 0:
            return
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
