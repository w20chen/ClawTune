"""Host OpenClaw + OpenClaw Docker sandbox runner for SWE-Rebench.

This mode keeps OpenClaw, the plugin, and the scheduler sidecar on the host.
The SWE-Rebench task repository is copied out of the task image into a host
workspace, then OpenClaw's own Docker sandbox executes tools against that
workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any

from swe_rebench.config import RunnerConfig
from swe_rebench.docker import ContainerResult
from swe_rebench.sandbox import sandbox_container_prefix
from swe_rebench.task_source import TaskDef


def run_host_sandbox_task(
    *,
    task: TaskDef,
    trace_dir: Path,
    config: RunnerConfig,
    bundle_dir: Path,
) -> ContainerResult:
    """Run one task with host OpenClaw and OpenClaw Docker sandbox."""
    started = time.monotonic()
    trace_dir.mkdir(parents=True, exist_ok=True)
    workspace = _task_workspace(config, task)
    openclaw_home = trace_dir / "openclaw-home"
    sidecar_port = _free_port()
    sidecar = None
    exit_code = -1
    error: str | None = None

    try:
        _write_host_tool_resource_preflight(trace_dir, config)
        _reset_directory(workspace, docker_cleanup_image=task.image)
        _reset_directory(openclaw_home)
        _export_testbed_from_image(task.image, workspace, config.docker.pull_policy)
        _install_sandbox_launcher(workspace, bundle_dir)
        _write_task_inputs(trace_dir, task, config, workspace)
        _ensure_openclaw_sandbox_image(task.image, trace_dir)

        sidecar = _start_sidecar(
            trace_dir=trace_dir,
            port=sidecar_port,
            config=config,
            workspace=workspace,
        )
        _configure_openclaw(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            sidecar_port=sidecar_port,
            workspace=workspace,
            config=config,
        )
        _cleanup_openclaw_sandbox_containers(trace_dir, workspace)
        exit_code = _run_openclaw_agent(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            workspace=workspace,
            sidecar_port=sidecar_port,
            task=task,
            config=config,
        )
        _cleanup_runtime_artifacts(workspace)
        _collect_patch(trace_dir, workspace, task)
    except Exception as exc:
        error = str(exc)
        _write_text(trace_dir / "host_sandbox_error.txt", traceback.format_exc())
    finally:
        if sidecar is not None:
            _stop_process(sidecar)
        _write_result_summary(trace_dir, task, workspace, exit_code, error)

    return ContainerResult(
        task_id=task.instance_id,
        image=task.image,
        exit_code=exit_code,
        error=error,
        trace_dir=trace_dir,
        trace_files=sorted(trace_dir.glob("*.jsonl")),
        duration_seconds=time.monotonic() - started,
    )


def _task_workspace(config: RunnerConfig, task: TaskDef) -> Path:
    safe_id = task.instance_id.replace("/", "_").replace(":", "_")
    return config.output.trace_root.parent / "workspaces" / safe_id


def _export_testbed_from_image(image: str, workspace: Path, pull_policy: str) -> None:
    docker = _require_executable("docker")
    if pull_policy != "never":
        pull = [docker, "pull", image]
        if pull_policy == "missing":
            inspect = subprocess.run(
                [docker, "image", "inspect", image],
                capture_output=True,
                text=True,
            )
            if inspect.returncode == 0:
                pull = []
        if pull:
            _run_checked(pull, "docker_pull")

    create = subprocess.run(
        [docker, "create", image],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = create.stdout.strip()
    try:
        _run_checked([docker, "cp", f"{container_id}:/testbed/.", str(workspace)], "docker_cp_testbed")
    finally:
        subprocess.run([docker, "rm", "-f", container_id], capture_output=True, text=True)


def _ensure_openclaw_sandbox_image(task_image: str, trace_dir: Path) -> None:
    """Tag the swe-rebench task image as the OpenClaw sandbox image.

    OpenClaw uses ``openclaw-sandbox:bookworm-slim`` as its default Docker
    sandbox image.  Instead of building a minimal image from scratch, we
    re-tag the swe-rebench task image so the sandbox inherits all of the
    compilers, libraries, and tools that the upstream SWE-Rebench task
    expects.
    """
    sandbox_image = "openclaw-sandbox:bookworm-slim"
    docker = _require_executable("docker")

    # Only re-tag when the task image differs from the current sandbox tag.
    # ``docker image inspect`` reports the digest, so we compare the actual
    # image identity rather than just the tag name.
    tag_needed = True
    inspect_sandbox = subprocess.run(
        [docker, "image", "inspect", sandbox_image],
        capture_output=True,
        text=True,
    )
    if inspect_sandbox.returncode == 0:
        try:
            sandbox_info = json.loads(inspect_sandbox.stdout)[0]
            sandbox_digest = sandbox_info.get("RepoDigests", [None])[0]
        except (json.JSONDecodeError, IndexError, KeyError):
            sandbox_digest = None

        inspect_task = subprocess.run(
            [docker, "image", "inspect", task_image],
            capture_output=True,
            text=True,
        )
        if inspect_task.returncode == 0:
            try:
                task_info = json.loads(inspect_task.stdout)[0]
                task_digest = task_info.get("RepoDigests", [None])[0]
            except (json.JSONDecodeError, IndexError, KeyError):
                task_digest = None

            if sandbox_digest and task_digest and sandbox_digest == task_digest:
                tag_needed = False

    if not tag_needed:
        return

    log_path = trace_dir / "sandbox-image-build.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [docker, "tag", task_image, sandbox_image],
            stdout=log,
            stderr=log,
            text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw_sandbox_image_tag_failed exit={result.returncode}: "
            f"{_tail_text(log_path, 2000)}"
        )


def _install_sandbox_launcher(workspace: Path, bundle_dir: Path) -> None:
    scheduler_src = bundle_dir / "scheduler" / "src"
    target_src = workspace / ".claw" / "scheduler" / "src"
    target_bin = workspace / ".claw" / "bin"
    if not scheduler_src.exists():
        raise FileNotFoundError(f"scheduler source not found in bundle: {scheduler_src}")
    if target_src.parent.exists():
        shutil.rmtree(target_src.parent)
    shutil.copytree(scheduler_src, target_src)
    target_bin.mkdir(parents=True, exist_ok=True)
    launcher = target_bin / "claw-launch"
    launcher.write_text(
        "#!/bin/sh\n"
        "export PYTHONPATH=\"/workspace/.claw/scheduler/src${PYTHONPATH:+:$PYTHONPATH}\"\n"
        "exec python3 -m agent_scheduler.launcher \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | 0o111)


def _start_sidecar(
    *,
    trace_dir: Path,
    port: int,
    config: RunnerConfig,
    workspace: Path,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    scheduler_src = str(config.repo_root / "services" / "scheduler" / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env.update(
        {
            "PYTHONPATH": (
                scheduler_src
                if not existing_pythonpath
                else scheduler_src + os.pathsep + existing_pythonpath
            ),
            "AGENT_SCHEDULER_DB_PATH": str(trace_dir / "scheduler.sqlite3"),
            "AGENT_SCHEDULER_TRACE_DIR": str(trace_dir),
            "AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL": config.llm.upstream_base_url,
            "AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY": config.llm.api_key,
            "AGENT_SCHEDULER_LLM_PROXY_ENABLED": "true",
            "AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL": config.llm.model,
            "AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL": config.llm.model,
            "AGENT_SCHEDULER_POLICY": "observe-only",
            "AGENT_SCHEDULER_DOCKER_EXEC_OBSERVER": "true",
            "AGENT_SCHEDULER_DOCKER_EXEC_CONTAINER_PREFIX": _sandbox_container_prefix(workspace),
            "AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED": (
                "true" if config.runtime.stage2_required else "false"
            ),
            "AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR": str(trace_dir / "tool-resource"),
        }
    )
    stdout = (trace_dir / "sidecar-stdout.txt").open("w", encoding="utf-8")
    stderr = (trace_dir / "sidecar-stderr.txt").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_scheduler.main",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
        cwd=str(config.repo_root / "services" / "scheduler"),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    _wait_ready(port)
    return process


def _write_host_tool_resource_preflight(trace_dir: Path, config: RunnerConfig) -> None:
    scheduler_src = str(config.repo_root / "services" / "scheduler" / "src")
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        scheduler_src
        if not existing_pythonpath
        else scheduler_src + os.pathsep + existing_pythonpath
    )
    code = r"""
import json
import os
import platform
import shutil
import sys
from pathlib import Path

payload = {
    "mode": "host-openclaw-sandbox",
    "platform": platform.system().lower(),
    "euid": os.geteuid() if hasattr(os, "geteuid") else None,
    "python": sys.executable,
    "pythonpath": os.environ.get("PYTHONPATH", ""),
    "docker": shutil.which("docker"),
    "clang": shutil.which("clang"),
    "llc": shutil.which("llc"),
    "bpftool": shutil.which("bpftool"),
    "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
    "lib_modules_exists": Path("/lib/modules").exists(),
}
try:
    from tool_resource.telemetry import (
        _bpf_runtime_diagnostics,
        _ensure_bcc_importable,
        validate_clause_telemetry_smoke,
        validate_clause_telemetry_runtime,
    )
    _ensure_bcc_importable()
    import bcc
    payload["bcc_import"] = {"ok": True, "path": getattr(bcc, "__file__", None)}
    payload["bpf_runtime"] = _bpf_runtime_diagnostics()
    try:
        validate_clause_telemetry_runtime(
            container_executable="docker",
            concurrency=1,
            workers=1,
        )
        payload["semantic_smoke"] = validate_clause_telemetry_smoke()
    except Exception as exc:
        payload["stage2_ready"] = False
        payload["stage2_disabled_reason"] = f"{type(exc).__name__}: {exc}"
    else:
        payload["stage2_ready"] = True
        payload["stage2_disabled_reason"] = None
        payload["bpf_module_load"] = {"ok": True}
except Exception as exc:
    payload["bcc_import"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload["stage2_ready"] = False
    payload["stage2_disabled_reason"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(payload, indent=2))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(config.repo_root / "services" / "scheduler"),
    )
    output = result.stdout if result.stdout.strip() else result.stderr
    if not output.strip():
        output = json.dumps(
            {
                "mode": "host-openclaw-sandbox",
                "error": "host tool-resource preflight produced no output",
                "returncode": result.returncode,
            },
            indent=2,
        )
    _write_text(trace_dir / "tool_resource_preflight_host.json", output.rstrip() + "\n")
    if not config.runtime.stage2_required:
        return
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Stage-2 eBPF preflight returned invalid JSON; see "
            f"{trace_dir / 'tool_resource_preflight_host.json'}"
        ) from exc
    if result.returncode != 0 or payload.get("stage2_ready") is not True:
        reason = payload.get("stage2_disabled_reason") or payload.get("error") or (
            f"preflight exited with status {result.returncode}"
        )
        raise RuntimeError(
            "Stage-2 eBPF telemetry is required but the host preflight failed: "
            f"{reason}. Run the host-sandbox command as root (for example, "
            "`sudo -E env \"PATH=$PATH\" \"$(command -v python3)\" "
            "-m swe_rebench.runner run ...`) and inspect "
            f"{trace_dir / 'tool_resource_preflight_host.json'}."
        )


def _configure_openclaw(
    *,
    trace_dir: Path,
    openclaw_home: Path,
    sidecar_port: int,
    workspace: Path,
    config: RunnerConfig,
) -> None:
    openclaw = _require_executable("openclaw")
    env = _openclaw_env(openclaw_home, sidecar_port, config, workspace)
    plugin_dir = config.repo_root / config.bundle.plugin_source
    _ensure_plugin_built(trace_dir, plugin_dir)
    plugin_install_dir = _stage_plugin_for_openclaw_if_needed(
        trace_dir=trace_dir,
        plugin_dir=plugin_dir,
    )
    endpoint_host = f"http://127.0.0.1:{sidecar_port}"
    endpoint_sandbox = f"http://host.docker.internal:{sidecar_port}"
    sandbox_config = _openclaw_config(
        endpoint_host=endpoint_host,
        endpoint_sandbox=endpoint_sandbox,
        workspace=workspace,
        config=config,
    )

    phase_log = trace_dir / "phase3.log"
    with phase_log.open("w", encoding="utf-8") as log:
        _run_logged(
            [
                openclaw,
                "onboard",
                "--non-interactive",
                "--accept-risk",
                "--skip-health",
                "--mode",
                "local",
                "--auth-choice",
                "vllm",
                "--custom-base-url",
                f"{endpoint_host}/v1",
                "--custom-api-key",
                config.llm.api_key,
                "--custom-model-id",
                config.llm.model,
            ],
            env,
            log,
            "openclaw_onboard",
        )
        _run_logged([openclaw, "plugins", "install", "--link", str(plugin_install_dir)], env, log, "plugin_install")
        _run_logged([openclaw, "plugins", "enable", "agent-scheduler"], env, log, "plugin_enable")
        patch = subprocess.run(
            [openclaw, "config", "patch", "--stdin"],
            input=sandbox_config,
            stdout=log,
            stderr=log,
            text=True,
            env=env,
        )
        if patch.returncode != 0:
            raise RuntimeError(
                f"openclaw_config_patch_failed exit={patch.returncode}: "
                f"{_tail_text(phase_log, 2000)}"
            )


def _run_openclaw_agent(
    *,
    trace_dir: Path,
    openclaw_home: Path,
    workspace: Path,
    sidecar_port: int,
    task: TaskDef,
    config: RunnerConfig,
) -> int:
    openclaw = _require_executable("openclaw")
    env = _openclaw_env(openclaw_home, sidecar_port, config, workspace)
    env.update(
        {
            "TASK_INSTANCE_ID": task.instance_id,
            "CLAW_SCHEDULER_ENDPOINT": f"http://host.docker.internal:{sidecar_port}",
            "CLAW_EXEC_WORKDIR": "/workspace",
            "CLAW_SANDBOX_HOST_WORKSPACE": str(workspace),
            "CLAW_SANDBOX_CONTAINER_WORKSPACE": "/workspace",
            "CLAW_ENABLE_CGROUP": "1",
            "CLAW_LAUNCH_DEBUG": "1",
        }
    )
    prompt_path = trace_dir / "agent_prompt.txt"
    stdout = (trace_dir / "agent-stdout.txt").open("w", encoding="utf-8")
    stderr = (trace_dir / "agent-stderr.txt").open("w", encoding="utf-8")
    stop_discovery = threading.Event()
    discovery = threading.Thread(
        target=_discover_sandbox_scope_loop,
        kwargs={
            "trace_dir": trace_dir,
            "openclaw_home": openclaw_home,
            "sidecar_port": sidecar_port,
            "config": config,
            "workspace": workspace,
            "stop_event": stop_discovery,
        },
        daemon=True,
    )
    discovery.start()
    process = subprocess.Popen(
        [
            openclaw,
            "agent",
            "--local",
            "--agent",
            "main",
            "--model",
            config.llm.openclaw_model_ref,
            "--message-file",
            str(prompt_path),
            *config.agent.extra_args,
        ],
        cwd=str(config.repo_root),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    try:
        return process.wait(
            timeout=config.batch.task_timeout_seconds if config.batch.task_timeout_seconds > 0 else None
        )
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return 124
    finally:
        stop_discovery.set()
        discovery.join(timeout=2)


def _openclaw_config(
    *,
    endpoint_host: str,
    endpoint_sandbox: str,
    workspace: Path,
    config: RunnerConfig,
) -> str:
    return json.dumps(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "repoRoot": str(workspace),
                    "sandbox": {
                        "mode": "all",
                        "backend": "docker",
                        "scope": "session",
                        "workspaceAccess": "rw",
                        "docker": {
                            "containerPrefix": _sandbox_container_prefix(workspace),
                            "workdir": "/workspace",
                            "network": "bridge",
                            "extraHosts": ["host.docker.internal:host-gateway"],
                            "dangerouslyAllowExternalBindSources": True,
                        },
                    },
                },
            },
            "plugins": {
                "entries": {
                    "agent-scheduler": {
                        "enabled": True,
                        "config": {
                            "endpoint": endpoint_host,
                            "mode": "observe",
                            "decisionTimeoutMs": 800,
                            "reportTimeoutMs": 800,
                            "failOpen": True,
                            "sendRawParams": False,
                            "recordRawTrace": False,
                            "logLevel": "warn",
                            "executionBackend": "managed-wrapper",
                            "launcherPath": "/workspace/.claw/bin/claw-launch",
                            "instrumentHosts": ["gateway", "*"],
                            "instrumentTools": ["exec"],
                            "enableCgroup": True,
                            "enableAffinity": False,
                            "enableNuma": False,
                            "profilingMode": "off",
                            "securityBoundaryAccepted": True,
                            "trace": {
                                "schema_version": 6,
                                "include_raw_events": False,
                                "include_llm_messages": True,
                                "include_tool_outputs": True,
                                "redact_sensitive_data": True,
                                "flush_span_start": True,
                                "max_string_bytes": 16384,
                                "max_messages_bytes": 131072,
                                "max_tool_output_bytes": 65536,
                                "trace_dir": "",
                            },
                        },
                    },
                },
            },
            "env": {
                "CLAW_SCHEDULER_ENDPOINT": endpoint_sandbox,
                "CLAW_EXEC_WORKDIR": "/workspace",
                "CLAW_SANDBOX_HOST_WORKSPACE": str(workspace),
                "CLAW_SANDBOX_CONTAINER_WORKSPACE": "/workspace",
                "CLAW_ENABLE_CGROUP": "1",
                "CLAW_LAUNCH_DEBUG": "1",
            },
        },
        indent=2,
    )


def _sandbox_container_prefix(workspace: Path) -> str:
    return sandbox_container_prefix(workspace)


def _cleanup_openclaw_sandbox_containers(trace_dir: Path, workspace: Path) -> None:
    """Remove stale OpenClaw sandbox containers for this task workspace.

    OpenClaw scopes sandbox containers by prefix.  Reusing a stale container can
    leave Docker exec stuck with a host workspace cwd that is outside the
    container mount namespace, so start each SWE-Rebench task from a fresh
    sandbox container.
    """
    docker = _require_executable("docker")
    prefix = _sandbox_container_prefix(workspace)
    log_path = trace_dir / "sandbox-container-cleanup.log"
    listed = subprocess.run(
        [docker, "ps", "-aq", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        _write_text(
            log_path,
            f"docker_ps_failed exit={listed.returncode}\n{listed.stdout}{listed.stderr}",
        )
        return

    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not container_ids:
        _write_text(log_path, f"no stale containers for prefix {prefix}\n")
        return

    removed = subprocess.run(
        [docker, "rm", "-f", *container_ids],
        capture_output=True,
        text=True,
    )
    _write_text(
        log_path,
        f"prefix={prefix}\ncontainers={json.dumps(container_ids)}\n"
        f"exit={removed.returncode}\n{removed.stdout}{removed.stderr}",
    )


def _ensure_plugin_built(trace_dir: Path, plugin_dir: Path) -> None:
    package_json = plugin_dir / "package.json"
    if not package_json.exists():
        raise FileNotFoundError(f"plugin package.json not found: {package_json}")

    npm = _require_executable("npm")
    log_path = trace_dir / "plugin-build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [npm, "run", "build"],
            cwd=str(plugin_dir),
            stdout=log,
            stderr=log,
            text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"plugin_build_failed exit={result.returncode}: "
            f"{_tail_text(log_path, 2000)}"
        )


def _stage_plugin_for_openclaw_if_needed(*, trace_dir: Path, plugin_dir: Path) -> Path:
    """Use a root-owned linked plugin path when the runner itself is root.

    OpenClaw rejects linked plugins whose ownership does not match the current
    user/root trust boundary. In host-openclaw-sandbox, users may run the runner
    with sudo only to satisfy eBPF permissions while keeping the repo owned by
    their normal account. Stage a per-run copy instead of asking them to chown
    the source tree.
    """

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return plugin_dir
    try:
        if plugin_dir.stat().st_uid == 0:
            return plugin_dir
    except OSError:
        return plugin_dir
    staged = trace_dir / "openclaw-plugin-root-owned"
    if staged.exists():
        shutil.rmtree(staged, onerror=_chmod_and_retry)
    shutil.copytree(plugin_dir, staged, ignore=shutil.ignore_patterns("node_modules"))
    return staged


def _openclaw_env(
    openclaw_home: Path,
    sidecar_port: int,
    config: RunnerConfig,
    workspace: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    # The sidecar owns benchmark trace writing.  Do not let a host-level
    # plugin trace override leak into OpenClaw and re-enable the plugin's
    # fallback trace writer for SWE-Rebench runs.
    env.pop("OPENCLAW_AGENT_SCHEDULER_TRACE_DIR", None)
    env.update(
        {
            "OPENCLAW_HOME": str(openclaw_home),
            "OPENCLAW_STATE_DIR": str(openclaw_home / ".openclaw"),
            "OPENCLAW_CONFIG_PATH": str(openclaw_home / ".openclaw" / "openclaw.json"),
            "OPENCLAW_WORKSPACE_DIR": str(workspace if workspace is not None else openclaw_home / ".openclaw" / "workspace"),
            "VLLM_API_KEY": config.llm.api_key or "sk-test",
            "LLM_API_KEY": config.llm.api_key,
            "CLAW_SCHEDULER_ENDPOINT": f"http://host.docker.internal:{sidecar_port}",
            "CLAW_EXEC_WORKDIR": "/workspace",
            "CLAW_SANDBOX_HOST_WORKSPACE": str(workspace) if workspace is not None else "",
            "CLAW_SANDBOX_CONTAINER_WORKSPACE": "/workspace",
            "CLAW_ENABLE_CGROUP": "1",
            "CLAW_LAUNCH_DEBUG": "1",
        }
    )
    (openclaw_home / ".openclaw").mkdir(parents=True, exist_ok=True)
    return env


def _discover_sandbox_scope_loop(
    *,
    trace_dir: Path,
    openclaw_home: Path,
    sidecar_port: int,
    config: RunnerConfig,
    workspace: Path,
    stop_event: threading.Event,
) -> None:
    openclaw = shutil.which("openclaw") or shutil.which("openclaw.cmd")
    docker = shutil.which("docker") or shutil.which("docker.cmd")
    if openclaw is None or docker is None:
        return
    env = _openclaw_env(openclaw_home, sidecar_port, config, workspace)
    prefix = _sandbox_container_prefix(workspace)
    seen: set[str] = set()
    while not stop_event.is_set():
        try:
            container_ids = _openclaw_sandbox_container_ids(openclaw, env)
            container_ids.extend(_docker_sandbox_container_ids(docker, prefix))
            for container_id in container_ids:
                if container_id in seen:
                    continue
                scope = _docker_container_scope(docker, container_id)
                if scope is None:
                    continue
                _post_sandbox_scope(sidecar_port, scope)
                seen.add(container_id)
                _write_text(
                    trace_dir / "sandbox_scope.json",
                    json.dumps(scope, indent=2) + "\n",
                )
        except Exception as exc:
            _write_text(trace_dir / "sandbox_scope_discovery_last_error.txt", str(exc) + "\n")
        stop_event.wait(0.1)


def _openclaw_sandbox_container_ids(openclaw: str, env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        [openclaw, "sandbox", "list", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    ids: list[str] = []
    for item in _walk_dicts(parsed):
        for key in ("container_id", "containerId", "container", "id"):
            value = item.get(key)
            if isinstance(value, str) and _looks_like_container_id(value):
                ids.append(value)
                break
    return list(dict.fromkeys(ids))


def _docker_sandbox_container_ids(docker: str, prefix: str) -> list[str]:
    result = subprocess.run(
        [docker, "ps", "-q", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if _looks_like_container_id(line.strip())
    ]


def _docker_container_scope(docker: str, container_id: str) -> dict[str, Any] | None:
    result = subprocess.run(
        [docker, "inspect", "-f", "{{.State.Pid}}", container_id],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        root_pid = int(result.stdout.strip())
    except ValueError:
        return None
    cgroup_path = _read_host_cgroup_path(root_pid)
    if cgroup_path is None:
        return None
    return {
        "kind": "cgroup-v2",
        "execution_id": None,
        "pid": root_pid,
        "root_pid": root_pid,
        "process_start_time": None,
        "root_starttime_ticks": None,
        "cgroup_path": cgroup_path,
        "pid_namespace_inode": None,
        "container_id": container_id,
        "include_children": True,
        "source": "openclaw-sandbox",
        "attribution_source": "shared-sandbox-container",
    }


def _read_host_cgroup_path(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("0::"):
            continue
        path = line[3:]
        if not path or path == "/":
            return "/sys/fs/cgroup"
        return f"/sys/fs/cgroup{path}"
    return None


def _post_sandbox_scope(sidecar_port: int, scope: dict[str, Any]) -> None:
    data = json.dumps(scope).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{sidecar_port}/v1/runtime/sandbox-scope",
        data=data,
        method="POST",
        headers={"content-type": "application/json"},
    )
    bearer = os.environ.get("AGENT_SCHEDULER_TOKEN") or os.environ.get("OPENCLAW_SCHEDULER_TOKEN")
    if bearer:
        request.add_header("authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(request, timeout=2):
        pass


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for item in value.values():
            out.extend(_walk_dicts(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_walk_dicts(item))
    return out


def _looks_like_container_id(value: str) -> bool:
    stripped = value.strip()
    return len(stripped) >= 12 and all(ch.isalnum() or ch in {"_", "-", "."} for ch in stripped)


def _write_task_inputs(trace_dir: Path, task: TaskDef, config: RunnerConfig, workspace: Path) -> None:
    prompt = (
        "You are running a SWE-Rebench task in an OpenClaw Docker sandbox.\n\n"
        "Goal: solve the task by editing the repository in the current workspace.\n\n"
        "Use relative paths for read, edit, write, and apply_patch. For exec, "
        "run commands from the default working directory or use relative paths; "
        "avoid absolute /workspace paths in file-tool calls. Do not request "
        "host/gateway execution or elevated execution; this run is intentionally "
        "sandboxed and those requests will fail.\n\n"
        "Workflow:\n"
        "1. Inspect the repository.\n"
        "2. Edit the source files needed for a minimal fix.\n"
        "3. Run relevant tests or a focused reproduction command.\n"
        "4. Leave the repository modified with your solution.\n\n"
        f"Task instance:\n{task.instance_id}\n\n"
        f"Problem statement:\n{task.problem_statement}\n"
    )
    if task.hint_text:
        prompt += f"\nHint:\n{task.hint_text}\n"
    _write_text(trace_dir / "agent_prompt.txt", prompt)
    _write_text(trace_dir / "agent-cwd.txt", str(workspace) + "\n")
    _write_text(
        trace_dir / "task_manifest.json",
        json.dumps(
            {
                "task_id": task.instance_id,
                "image": task.image,
                "base_commit": task.base_commit,
                "model": config.llm.model,
                "openclaw_model_ref": config.llm.openclaw_model_ref,
                "runtime_mode": "host-openclaw-sandbox",
                "workspace": str(workspace),
                "problem_statement_bytes": len(task.problem_statement),
                "hint_text_bytes": len(task.hint_text),
            },
            indent=2,
        )
        + "\n",
    )


_RUNTIME_ARTIFACTS = (
    ".claw",
    ".local",
    "AGENTS.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
    "openclaw-workspace-state.json",
)


def _cleanup_runtime_artifacts(workspace: Path) -> None:
    for name in _RUNTIME_ARTIFACTS:
        path = workspace / name
        if not path.exists():
            continue
        if _git_tracks_path(workspace, name):
            continue
        if path.is_dir():
            shutil.rmtree(path, onerror=_chmod_and_retry)
        else:
            path.unlink()


def _git_tracks_path(workspace: Path, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--error-unmatch", "--", relative_path],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _collect_patch(trace_dir: Path, workspace: Path, task: TaskDef) -> None:
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--short"],
        capture_output=True,
        text=True,
    )
    diff_stat = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--stat"],
        capture_output=True,
        text=True,
    )
    _write_text(
        trace_dir / "repo_status.txt",
        "=== host workspace ===\n"
        f"{workspace}\n\n"
        "=== git status ===\n"
        f"{status.stdout}{status.stderr}\n"
        "=== git diff --stat ===\n"
        f"{diff_stat.stdout}{diff_stat.stderr}\n",
    )
    diff_cmd = ["git", "-C", str(workspace), "diff"]
    if task.base_commit:
        diff_cmd.append(task.base_commit)
    diff_cmd.append("--")
    patch = subprocess.run(diff_cmd, capture_output=True, text=True)
    _write_text(trace_dir / "model.patch", patch.stdout)


def _write_result_summary(
    trace_dir: Path,
    task: TaskDef,
    workspace: Path,
    exit_code: int,
    error: str | None,
) -> None:
    patch = trace_dir / "model.patch"
    patch_bytes = patch.stat().st_size if patch.exists() else 0
    summary: dict[str, Any] = {
        "task_id": task.instance_id,
        "agent_exit_code": exit_code,
        "testbed_exists": workspace.exists(),
        "patch_bytes": patch_bytes,
        "has_patch": patch_bytes > 0,
        "runtime_mode": "host-openclaw-sandbox",
    }
    if error is not None:
        summary["error"] = error
    _write_text(trace_dir / "result_summary.json", json.dumps(summary, indent=2) + "\n")


def _wait_ready(port: int) -> None:
    deadline = time.monotonic() + 60
    url = f"http://127.0.0.1:{port}/health/ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"sidecar_not_ready port={port}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reset_directory(path: Path, *, docker_cleanup_image: str | None = None) -> None:
    if path.exists():
        try:
            shutil.rmtree(path, onerror=_chmod_and_retry)
        except PermissionError:
            if docker_cleanup_image is None:
                raise
            _reset_directory_with_docker(path, docker_cleanup_image)
    path.mkdir(parents=True, exist_ok=True)


def _chmod_and_retry(function: Any, path: str, _exc_info: Any) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    if function is os.open:
        exc = _exc_info[1] if isinstance(_exc_info, tuple) and len(_exc_info) > 1 else None
        if isinstance(exc, BaseException):
            raise exc
        raise PermissionError(path)
    function(path)


def _reset_directory_with_docker(path: Path, image: str) -> None:
    docker = _require_executable("docker")
    target = path.resolve()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_resolved = parent.resolve()
    if target.parent != parent_resolved:
        target = parent_resolved / target.name
    try:
        target.relative_to(parent_resolved)
    except ValueError as exc:
        raise RuntimeError(f"refusing docker cleanup outside parent: {target}") from exc
    if target.name in {"", ".", ".."} or any(sep in target.name for sep in ("/", "\\")):
        raise RuntimeError(f"refusing unsafe docker cleanup target name: {target.name!r}")

    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    script = (
        'set -eu\n'
        'case "$TARGET" in ""|"."|".."|*/*) exit 64 ;; esac\n'
        'rm -rf "/host_parent/$TARGET"\n'
        'mkdir -p "/host_parent/$TARGET"\n'
        'chown "$HOST_UID:$HOST_GID" "/host_parent/$TARGET" 2>/dev/null || true\n'
    )
    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-e",
            f"TARGET={target.name}",
            "-e",
            f"HOST_UID={uid}",
            "-e",
            f"HOST_GID={gid}",
            "-v",
            f"{parent_resolved}:/host_parent",
            image,
            "sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker_workspace_cleanup_failed exit={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tail_text(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"cannot read log: {exc}"
    return text[-max_chars:]


def _require_executable(name: str) -> str:
    found = shutil.which(name) or shutil.which(f"{name}.cmd")
    if found is None:
        raise FileNotFoundError(f"required executable not found: {name}")
    return found


def _run_checked(cmd: list[str], label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{label}_failed exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def _run_logged(cmd: list[str], env: dict[str, str], log: Any, label: str) -> None:
    log.write(f"=== {label} ===\n")
    result = subprocess.run(cmd, stdout=log, stderr=log, text=True, env=env)
    log.write(f"\nexit={result.returncode}\n\n")
    if result.returncode != 0:
        raise RuntimeError(f"{label}_failed exit={result.returncode}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
