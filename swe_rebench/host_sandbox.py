"""Host OpenClaw + OpenClaw Docker sandbox runner for SWE-Rebench.

This mode keeps OpenClaw, the plugin, and the scheduler sidecar on the host.
The SWE-Rebench task repository is copied out of the task image into a host
workspace, then OpenClaw's own Docker sandbox executes tools against that
workspace.
"""

from __future__ import annotations

import errno
import json
import math
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any

from swe_rebench.config import RunnerConfig
from swe_rebench.docker import ContainerCleanupError, ContainerResult
from swe_rebench.sandbox import sandbox_container_prefix
from swe_rebench.task_source import TaskDef, task_repo_key

# ── thread-safe console logging ──────────────────────────────────
_print_lock = threading.Lock()


# SWE-bench evaluation images keep the task environment under
# /opt/miniconda3/envs/testbed.  Retain the older /opt/conda layout as a
# fallback because some SWE-Rebench images use that prefix.  OpenClaw passes
# this value directly into its Docker sandbox, so omitting the actual testbed
# prefix silently falls back to /usr/bin/python3 and loses the task's installed
# dependencies.
_SANDBOX_TASK_PATH = ":".join(
    (
        "/workspace/.claw/bin",
        "/opt/miniconda3/envs/testbed/bin",
        "/opt/conda/envs/testbed/bin",
        "/opt/miniconda3/condabin",
        "/opt/miniconda3/bin",
        "/opt/conda/bin",
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    )
)

# OpenClaw talks only to the loopback Scheduler proxy in host-sandbox mode.
# Keep the real upstream credential exclusively in the sidecar environment.
_LOCAL_PROXY_API_KEY = "clawtune-local-proxy"

_TOOL_RESOURCE_KB_SCHEMAS = {
    "runtime-tool-resource-kb.json": "runtime_tool_resource_kb_v1",
    "clause-resource-kb.json": "runtime_clause_resource_kb_v4",
    "clause-lattice-time-kb.json": "clause_lattice_time_kb_v1",
}

_TASK_CLEANUP_TIMEOUT_SECONDS = 15.0
_DISCOVERY_COMMAND_TIMEOUT_SECONDS = 5.0
_SUBPROCESS_POPEN_TYPE = subprocess.Popen


class KnowledgeBaseSyncError(RuntimeError):
    """A shared KB generation could not be copied or published safely."""


class TaskDeadlineExceeded(TimeoutError):
    """The whole-task wall-clock budget expired."""


def _task_deadline(config: RunnerConfig, started: float) -> float | None:
    seconds = config.batch.task_timeout_seconds
    return started + seconds if seconds > 0 else None


def _remaining_task_seconds(
    deadline: float | None,
    *,
    phase: str,
) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TaskDeadlineExceeded(
            f"task timed out during {phase}; whole-task wall-clock budget exhausted"
        )
    return remaining


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def run_host_sandbox_task(
    *,
    task: TaskDef,
    trace_dir: Path,
    config: RunnerConfig,
    bundle_dir: Path,
    shared_kb_dir: Path | None = None,
) -> ContainerResult:
    """Run one task with host OpenClaw and OpenClaw Docker sandbox."""
    started = time.monotonic()
    deadline = _task_deadline(config, started)
    trace_dir.mkdir(parents=True, exist_ok=True)
    workspace = _task_workspace(config, task)
    openclaw_home = trace_dir / "openclaw-home"
    sidecar_port = _free_port()
    sidecar = None
    exit_code = -1
    error: str | None = None

    try:
        _write_host_tool_resource_preflight(trace_dir, config, deadline=deadline)
        _remaining_task_seconds(deadline, phase="host telemetry preflight")
        _reset_directory(
            workspace,
            docker_cleanup_image=task.image,
            docker_platform=config.docker.platform,
            deadline=deadline,
        )
        _reset_directory(openclaw_home, deadline=deadline)
        _remaining_task_seconds(deadline, phase="workspace reset")
        _export_testbed_from_image(
            task.image,
            workspace,
            config.docker.pull_policy,
            config.docker.platform,
            deadline=deadline,
        )
        _make_sandbox_workspace_writable(workspace)
        _install_sandbox_launcher(workspace, bundle_dir)
        _write_task_inputs(
            trace_dir,
            task,
            config,
            workspace,
            bundle_dir,
            shared_kb_dir=shared_kb_dir,
        )
        _ensure_openclaw_sandbox_image(
            task.image,
            trace_dir,
            config.docker.platform,
            deadline=deadline,
        )
        _verify_sandbox_launcher(
            trace_dir,
            workspace,
            config.docker.platform,
            deadline=deadline,
        )
        _verify_sandbox_task_environment(
            trace_dir,
            workspace,
            config.docker.platform,
            deadline=deadline,
        )
        _remaining_task_seconds(deadline, phase="sandbox preflight")

        _seed_runtime_tool_resource_kb(
            trace_dir,
            config,
            source_dir=shared_kb_dir,
        )

        sidecar = _start_sidecar(
            trace_dir=trace_dir,
            port=sidecar_port,
            config=config,
            workspace=workspace,
            repo=task_repo_key(task),
            deadline=deadline,
        )
        _configure_openclaw(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            sidecar_port=sidecar_port,
            workspace=workspace,
            config=config,
            deadline=deadline,
        )
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
        _remaining_task_seconds(deadline, phase="agent setup")
        exit_code = _run_openclaw_agent(
            trace_dir=trace_dir,
            openclaw_home=openclaw_home,
            workspace=workspace,
            sidecar_port=sidecar_port,
            task=task,
            config=config,
            task_deadline=deadline,
        )
        timeout_record = _read_json_object(trace_dir / "task-timeout.json")
        if exit_code == 124 and isinstance(timeout_record, dict):
            error = str(timeout_record.get("message") or "task timed out")
        _remaining_task_seconds(deadline, phase="result collection")
        _cleanup_runtime_artifacts(workspace, deadline=deadline)
        _collect_patch(trace_dir, workspace, task, deadline=deadline)
    except TaskDeadlineExceeded as exc:
        exit_code = 124
        error = str(exc)
        _write_timeout_record(
            trace_dir,
            scope="task",
            message=error,
            configured_seconds=config.batch.task_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        error = (
            "task timed out while waiting for a setup subprocess: "
            f"{exc.cmd!r}"
        )
        _write_timeout_record(
            trace_dir,
            scope="task",
            message=error,
            configured_seconds=config.batch.task_timeout_seconds,
        )
    except ContainerCleanupError:
        # The task-local sidecar/agent may still be able to mutate its KB.
        # Propagate instead of returning a result that _run_one would publish.
        raise
    except Exception as exc:
        error = str(exc)
        _write_text(trace_dir / "host_sandbox_error.txt", traceback.format_exc())
    except KeyboardInterrupt:
        error = "interrupted by user"
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            _cleanup_openclaw_sandbox_containers(
                trace_dir,
                workspace,
                timeout_seconds=_TASK_CLEANUP_TIMEOUT_SECONDS,
                strict=True,
            )
        except BaseException as exc:
            cleanup_error = exc
        finally:
            try:
                if sidecar is not None:
                    _stop_process(sidecar)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            if cleanup_error is not None and error is None:
                error = f"task cleanup failed: {cleanup_error}"
            _write_result_summary(trace_dir, task, workspace, exit_code, error)
        if cleanup_error is not None:
            raise cleanup_error

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


def _export_testbed_from_image(
    image: str,
    workspace: Path,
    pull_policy: str,
    platform: str = "",
    *,
    deadline: float | None = None,
) -> None:
    docker = _require_executable("docker")
    if pull_policy != "never":
        pull = [docker, "pull", *_docker_platform_args(platform), image]
        if pull_policy == "missing" and not platform:
            inspect = subprocess.run(
                [docker, "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=_remaining_task_seconds(deadline, phase="task image inspection"),
            )
            if inspect.returncode == 0:
                pull = []
        if pull:
            if deadline is None:
                _run_checked(pull, "docker_pull")
            else:
                _run_checked(
                    pull,
                    "docker_pull",
                    timeout=_remaining_task_seconds(deadline, phase="task image pull"),
                )

    create = subprocess.run(
        [docker, "create", *_docker_platform_args(platform), image],
        capture_output=True,
        text=True,
        check=True,
        timeout=_remaining_task_seconds(deadline, phase="task container creation"),
    )
    container_id = create.stdout.strip()
    try:
        copy_command = [docker, "cp", f"{container_id}:/testbed/.", str(workspace)]
        if deadline is None:
            _run_checked(copy_command, "docker_cp_testbed")
        else:
            _run_checked(
                copy_command,
                "docker_cp_testbed",
                timeout=_remaining_task_seconds(deadline, phase="repository export"),
            )
    finally:
        try:
            subprocess.run(
                [docker, "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=_TASK_CLEANUP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerCleanupError(
                f"timed out removing export container {container_id}"
            ) from exc


def _make_sandbox_workspace_writable(workspace: Path) -> None:
    """Expose a sudo-exported repository to OpenClaw's unprivileged uid.

    ``docker cp`` preserves image ownership, while this runner normally runs
    as root for BPF. OpenClaw's tool container must still be able to traverse
    and edit the isolated task tree.
    """

    workspace.chmod(workspace.stat().st_mode | 0o777)
    for path in workspace.rglob("*"):
        # Do not follow a repository symlink and chmod a target outside this
        # isolated workspace.
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(path.stat().st_mode | 0o777)
        elif path.is_file():
            path.chmod(path.stat().st_mode | 0o666)


def _ensure_openclaw_sandbox_image(
    task_image: str,
    trace_dir: Path,
    platform: str = "",
    *,
    deadline: float | None = None,
) -> None:
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
        timeout=_remaining_task_seconds(deadline, phase="sandbox image inspection"),
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
            timeout=_remaining_task_seconds(deadline, phase="task image inspection"),
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
            timeout=_remaining_task_seconds(deadline, phase="sandbox image tagging"),
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw_sandbox_image_tag_failed exit={result.returncode}: "
            f"{_tail_text(log_path, 2000)}"
        )


def _verify_sandbox_launcher(
    trace_dir: Path,
    workspace: Path,
    platform: str = "",
    *,
    deadline: float | None = None,
) -> None:
    """Verify launcher mode and the environment its payload will inherit."""

    docker = _require_executable("docker")
    log_path = trace_dir / "launcher-preflight.log"
    container_name = (
        f"{_sandbox_container_prefix(workspace)}launcher-preflight-"
        f"{os.getpid()}-{threading.get_ident()}"
    )
    result = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
            *_docker_platform_args(platform),
            "--network",
            "none",
            "--env",
            "CLAW_LAUNCH_MODE=fork-exec",
            "--user",
            "65534:65534",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--workdir",
            "/workspace",
            "--entrypoint",
            "/bin/sh",
            "openclaw-sandbox:bookworm-slim",
            "/workspace/.claw/bin/claw-launch",
            "diagnose",
        ],
        capture_output=True,
        text=True,
        timeout=_remaining_task_seconds(deadline, phase="launcher preflight"),
    )
    _write_text(log_path, (result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        raise RuntimeError(
            "sandbox_launcher_preflight_failed: the mounted claw-launch must "
            "be readable and select a supported fork-exec runtime in the "
            f"sandbox: {_tail_text(log_path, 2000)}"
        )
    python_markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if not any((workspace / marker).is_file() for marker in python_markers):
        return
    try:
        diagnostics = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        diagnostics = {}
    python3 = diagnostics.get("payload_python3")
    if not (
        isinstance(python3, str)
        and python3.startswith(
            (
                "/opt/miniconda3/envs/testbed/bin/",
                "/opt/conda/envs/testbed/bin/",
            )
        )
        and diagnostics.get("payload_pip") == "/workspace/.claw/bin/pip"
        and diagnostics.get("payload_pip3") == "/workspace/.claw/bin/pip3"
    ):
        raise RuntimeError(
            "sandbox_launcher_payload_environment_failed: managed-wrapper "
            "payload must resolve testbed python and mounted pip wrappers: "
            f"{_tail_text(log_path, 2000)}"
        )


def _verify_sandbox_task_environment(
    trace_dir: Path,
    workspace: Path,
    platform: str = "",
    *,
    deadline: float | None = None,
) -> None:
    """Fail early when a Python task would fall back outside its testbed env."""

    python_markers = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
    )
    if not any((workspace / marker).is_file() for marker in python_markers):
        return
    docker = _require_executable("docker")
    log_path = trace_dir / "sandbox-runtime-preflight.log"
    container_name = (
        f"{_sandbox_container_prefix(workspace)}runtime-preflight-"
        f"{os.getpid()}-{threading.get_ident()}"
    )
    script = (
        "set -eu\n"
        "printf 'PATH=%s\\n' \"$PATH\"\n"
        "printf 'python3=%s\\n' \"$(command -v python3)\"\n"
        "python3 -c 'import sys; print(sys.executable)'\n"
        "python3 -m pip --version\n"
        "printf 'pip=%s\\n' \"$(command -v pip)\"\n"
        "pip --version\n"
        "printf 'pip3=%s\\n' \"$(command -v pip3)\"\n"
        "pip3 --version\n"
    )
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--name",
                container_name,
                *_docker_platform_args(platform),
                "--network",
                "none",
                "--env",
                f"PATH={_SANDBOX_TASK_PATH}",
                "--user",
                "65534:65534",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace",
                "--workdir",
                "/workspace",
                "--entrypoint",
                "/bin/sh",
                "openclaw-sandbox:bookworm-slim",
                "-c",
                script,
            ],
            stdout=log,
            stderr=log,
            text=True,
            timeout=_remaining_task_seconds(
                deadline,
                phase="sandbox runtime preflight",
            ),
        )
    if result.returncode != 0:
        raise RuntimeError(
            "sandbox_task_environment_preflight_failed: the Python task image "
            "must expose its testbed python and pip to the OpenClaw sandbox: "
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
        f'export PATH="{_SANDBOX_TASK_PATH}"\n'
        "export CLAW_LAUNCHER_PYTHONPATH=/workspace/.claw/scheduler/src\n"
        "export PYTHONPATH=\"$CLAW_LAUNCHER_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}\"\n"
        # Keep the launcher on the image's modern system Python even when the
        # payload PATH selects an older project-specific testbed interpreter.
        # The forked /bin/sh payload still inherits _SANDBOX_TASK_PATH.
        "_CLAW_LAUNCHER_PYTHON=/usr/bin/python3\n"
        "if [ ! -x \"$_CLAW_LAUNCHER_PYTHON\" ]; then "
        "echo 'claw-launch: /usr/bin/python3 is unavailable' >&2; exit 127; fi\n"
        "exec \"$_CLAW_LAUNCHER_PYTHON\" -m agent_scheduler.launcher \"$@\"\n",
        encoding="utf-8",
    )
    # The runner is commonly invoked through ``sudo -E`` for BPF access.
    # A restrictive root umask can otherwise leave the intermediate .claw
    # directories at 0700 and this script at 0711.  The sandbox user then
    # cannot traverse/read the interpreted script and every exec fails with
    # exit 126 before claw-launch can claim the execution.  Use explicit
    # container-facing permissions instead of merely adding execute bits.
    _make_sandbox_runtime_readable(workspace / ".claw", target_src)
    launcher.chmod(0o755)
    for pip_name in ("pip", "pip3"):
        pip_wrapper = target_bin / pip_name
        pip_wrapper.write_text(
            "#!/bin/sh\nexec python3 -m pip \"$@\"\n",
            encoding="utf-8",
        )
        pip_wrapper.chmod(0o755)


def _make_sandbox_runtime_readable(runtime_root: Path, scheduler_src: Path) -> None:
    """Make the mounted launcher runtime readable by the sandbox uid.

    Only the private ``.claw`` runtime is changed.  Task repository modes are
    preserved, and regular scheduler source files do not gain execute bits.
    """

    for directory in (
        runtime_root,
        runtime_root / "scheduler",
        scheduler_src,
        runtime_root / "bin",
    ):
        directory.chmod(directory.stat().st_mode | 0o555)
    for path in scheduler_src.rglob("*"):
        if path.is_dir():
            path.chmod(path.stat().st_mode | 0o555)
        elif path.is_file():
            path.chmod(path.stat().st_mode | 0o444)


def _start_sidecar(
    *,
    trace_dir: Path,
    port: int,
    config: RunnerConfig,
    workspace: Path,
    repo: str,
    deadline: float | None = None,
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
            "AGENT_SCHEDULER_TOOL_RESOURCE_REPO": repo,
            "AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR": str(trace_dir / "tool-resource"),
        }
    )
    stdout = (trace_dir / "sidecar-stdout.txt").open("w", encoding="utf-8")
    stderr = (trace_dir / "sidecar-stderr.txt").open("w", encoding="utf-8")
    try:
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
    finally:
        # Popen duplicates/inherits these handles.  The parent must not retain
        # two descriptors per task across a long serial benchmark.
        stdout.close()
        stderr.close()
    try:
        if deadline is None:
            _wait_ready(port)
        else:
            remaining = _remaining_task_seconds(deadline, phase="sidecar startup")
            if remaining is not None and remaining < 60.0:
                _wait_ready(port, timeout_seconds=remaining)
            else:
                _wait_ready(port)
    except BaseException:
        # Do not let an unreturned process keep mutating this task's KB while
        # the batch runner publishes it for the next task.
        _stop_process(process)
        raise
    return process


def _prepare_batch_tool_resource_kb(
    shared_kb_dir: Path,
    config: RunnerConfig,
) -> None:
    """Initialize one run-scoped shared KB from the tracked cold-start seed."""

    source_dir = config.repo_root / "traces" / "tool-resource"
    try:
        _validate_kb_snapshot_pair(source_dir)
        shared_kb_dir.mkdir(parents=True, exist_ok=False)
        for filename in _TOOL_RESOURCE_KB_SCHEMAS:
            _atomic_copy(source_dir / filename, shared_kb_dir / filename)
        _validate_kb_snapshot_pair(shared_kb_dir)
    except KnowledgeBaseSyncError:
        raise
    except Exception as exc:
        raise KnowledgeBaseSyncError(
            f"failed to initialize shared KB {shared_kb_dir}: {exc}"
        ) from exc


def _seed_runtime_tool_resource_kb(
    trace_dir: Path,
    config: RunnerConfig,
    *,
    source_dir: Path | None = None,
) -> None:
    """Copy the repo's pre-seeded predictor KBs to the task trace directory.

    The RuntimeToolResourceKB predictor needs cold-start training data for
    continuous p90 latency/CPU/memory estimates.  The repo ships a small
    synthetic runtime snapshot and a clause-latency snapshot.  Without them,
    continuous predictions and clause latency-bucket predictions have no
    cold-start evidence.
    """
    dest_dir = trace_dir / "tool-resource"
    dest_dir.mkdir(parents=True, exist_ok=True)
    source_dir = source_dir or config.repo_root / "traces" / "tool-resource"
    try:
        _validate_kb_snapshot_pair(source_dir)
        for filename in _TOOL_RESOURCE_KB_SCHEMAS:
            dest = dest_dir / filename
            source = source_dir / filename
            if not dest.exists():
                _atomic_copy(source, dest)
        _validate_kb_snapshot_pair(dest_dir)
    except KnowledgeBaseSyncError:
        raise
    except Exception as exc:
        raise KnowledgeBaseSyncError(
            f"failed to seed task KB {dest_dir} from {source_dir}: {exc}"
        ) from exc


def _publish_tool_resource_kb(trace_dir: Path, shared_kb_dir: Path) -> None:
    """Publish a completed task's valid KB generation for the next serial task."""

    source_dir = trace_dir / "tool-resource"
    # Stage and validate every file before committing any one. Preserve a
    # complete rollback generation so an I/O failure during replace cannot
    # expose a mixed generation to the next task.
    try:
        _validate_kb_snapshot_pair(source_dir)
        _validate_kb_snapshot_pair(shared_kb_dir)
        with tempfile.TemporaryDirectory(
            prefix=".kb-publish-",
            dir=shared_kb_dir.parent,
        ) as temporary_root:
            root = Path(temporary_root)
            staged_dir = root / "staged"
            backup_dir = root / "backup"
            staged_dir.mkdir()
            backup_dir.mkdir()
            for filename in _TOOL_RESOURCE_KB_SCHEMAS:
                _atomic_copy(source_dir / filename, staged_dir / filename)
                _atomic_copy(shared_kb_dir / filename, backup_dir / filename)
            _validate_kb_snapshot_pair(staged_dir)
            _validate_kb_snapshot_pair(backup_dir)
            try:
                for filename in _TOOL_RESOURCE_KB_SCHEMAS:
                    os.replace(staged_dir / filename, shared_kb_dir / filename)
                _validate_kb_snapshot_pair(shared_kb_dir)
            except Exception as commit_exc:
                try:
                    for filename in _TOOL_RESOURCE_KB_SCHEMAS:
                        _atomic_copy(backup_dir / filename, shared_kb_dir / filename)
                    _validate_kb_snapshot_pair(shared_kb_dir)
                except Exception as rollback_exc:
                    raise KnowledgeBaseSyncError(
                        "shared KB publish and rollback both failed: "
                        f"publish={commit_exc}; rollback={rollback_exc}"
                    ) from rollback_exc
                raise KnowledgeBaseSyncError(
                    f"shared KB publish failed and was rolled back: {commit_exc}"
                ) from commit_exc
    except KnowledgeBaseSyncError:
        raise
    except Exception as exc:
        raise KnowledgeBaseSyncError(
            f"failed to publish task KB {source_dir} to {shared_kb_dir}: {exc}"
        ) from exc


def _validate_kb_snapshot_pair(directory: Path) -> None:
    for filename, schema_prefix in _TOOL_RESOURCE_KB_SCHEMAS.items():
        path = directory / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KnowledgeBaseSyncError(
                f"required KB snapshot is missing: {path}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeBaseSyncError(
                f"invalid KB snapshot {path}: {exc}"
            ) from exc
        schema = payload.get("schema") if isinstance(payload, dict) else None
        if schema != schema_prefix:
            raise KnowledgeBaseSyncError(
                f"invalid KB snapshot schema in {path}: {schema!r}; "
                f"expected {schema_prefix!r}"
            )
        if filename == "clause-lattice-time-kb.json":
            _validate_lattice_time_kb_snapshot(path, payload)
        else:
            if payload.get("max_prefix_depth") != 4:
                raise KnowledgeBaseSyncError(
                    f"invalid KB snapshot max_prefix_depth in {path}: "
                    f"{payload.get('max_prefix_depth')!r}"
                )
            for field in ("public", "repo"):
                if not isinstance(payload.get(field), dict):
                    raise KnowledgeBaseSyncError(
                        f"invalid KB snapshot {path}: {field!r} must be an object"
                    )
        if not isinstance(payload.get("pending"), list):
            raise KnowledgeBaseSyncError(
                f"invalid KB snapshot {path}: 'pending' must be an array"
            )
        try:
            # Use the scheduler's own deserializer as the final authority so a
            # snapshot accepted here is guaranteed to be loadable by sidecar.
            from tool_resource.runtime_kb import (
                ClauseResourceKB,
                RuntimeToolResourceKB,
            )
            if filename == "runtime-tool-resource-kb.json":
                RuntimeToolResourceKB.from_json_obj(payload)
            elif filename == "clause-resource-kb.json":
                ClauseResourceKB.from_json_obj(payload)
        except Exception as exc:
            raise KnowledgeBaseSyncError(
                f"scheduler rejected KB snapshot {path}: {exc}"
            ) from exc


def _validate_lattice_time_kb_snapshot(path: Path, payload: dict[str, Any]) -> None:
    """Validate the portable flat-lattice snapshot without scheduler imports.

    The benchmark runner is also imported from environments that expose the
    installed ``tool_resource`` package but not this checkout's new
    ``tool_time`` module.  Keep the atomic hand-off validator self-contained;
    the sidecar still uses ``LatticeTimeKB.from_json_obj`` when it loads the
    snapshot.
    """

    expected_generation = {
        "mode": "bounded",
        "max_optional_features": 6,
        "min_partial_support": 1,
        "max_nodes_per_signature": 4_096,
        "node_occurrence_budget": 20_000,
        "max_shrinkage_candidates": 512,
    }
    if payload.get("node_generation") != expected_generation:
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB node_generation in {path}: "
            f"{payload.get('node_generation')!r}"
        )
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise KnowledgeBaseSyncError(
            f"invalid KB snapshot {path}: 'observations' must be an array"
        )
    pending = payload.get("pending")
    if not isinstance(pending, list):
        raise KnowledgeBaseSyncError(
            f"invalid KB snapshot {path}: 'pending' must be an array"
        )
    for collection_name, rows in (("observations", observations), ("pending", pending)):
        for index, row in enumerate(rows):
            _validate_lattice_time_observation(
                path,
                row,
                location=f"{collection_name}[{index}]",
            )
    last_query_ts = payload.get("last_query_ts")
    if last_query_ts is not None and not _is_finite_number(last_query_ts):
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB last_query_ts in {path}: {last_query_ts!r}"
        )


def _validate_lattice_time_observation(
    path: Path,
    row: Any,
    *,
    location: str,
) -> None:
    if not isinstance(row, dict):
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: must be an object"
        )
    allowed = {
        "repo",
        "bin",
        "argv",
        "ts_start",
        "ts_end",
        "latency_ms",
        "peak_cpu_cores",
        "sampled_peak_rss_mb",
        "cpu_ns_cumulative",
        "in_loop",
        "in_pipe",
        "in_subst",
        "pipeline_position",
    }
    unknown = sorted(row.keys() - allowed)
    if unknown:
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            f"unknown fields {unknown!r}"
        )
    required = {"repo", "bin", "argv", "ts_start", "ts_end", "latency_ms"}
    missing = sorted(required - row.keys())
    if missing:
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            f"missing {missing!r}"
        )
    if (
        not isinstance(row["repo"], str)
        or not isinstance(row["bin"], str)
        or not row["bin"]
    ):
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            "repo must be a string and bin must be a non-empty string"
        )
    argv = row["argv"]
    if not isinstance(argv, list) or not argv or not all(
        isinstance(value, str) for value in argv
    ):
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            "argv must be a non-empty string array"
        )
    ts_start = row["ts_start"]
    ts_end = row["ts_end"]
    if not (_is_finite_number(ts_start) and _is_finite_number(ts_end)):
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            "timestamps must be finite numbers"
        )
    if float(ts_end) < float(ts_start):
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            "ts_end precedes ts_start"
        )
    latency_ms = row.get("latency_ms")
    if not _is_finite_number(latency_ms) or float(latency_ms) <= 0.0:
        raise KnowledgeBaseSyncError(
            f"invalid lattice KB observation {location} in {path}: "
            "latency_ms must be a positive finite number"
        )


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_host_tool_resource_preflight(
    trace_dir: Path,
    config: RunnerConfig,
    *,
    deadline: float | None = None,
) -> None:
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
    from tool_resource.mvdan_client import ensure_compatible_adapter
    from tool_resource.telemetry import (
        _bpf_runtime_diagnostics,
        _ensure_bcc_importable,
        validate_clause_telemetry_smoke,
        validate_clause_telemetry_runtime,
    )
    bcc = _ensure_bcc_importable()
    payload["bcc_import"] = {
        "ok": True,
        "module": getattr(bcc, "__name__", None),
        "path": getattr(bcc, "__file__", None),
    }
    payload["bpf_runtime"] = _bpf_runtime_diagnostics()
    try:
        adapter_path = ensure_compatible_adapter()
        payload["mvdan_adapter"] = {
            "ok": True,
            "path": str(adapter_path),
            "architecture": platform.machine(),
        }
        validate_clause_telemetry_runtime(
            container_executable="docker",
            concurrency=1,
            workers=1,
        )
        payload["semantic_smoke"] = validate_clause_telemetry_smoke()
    except Exception as exc:
        if "mvdan_adapter" not in payload:
            payload["mvdan_adapter"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
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
        timeout=_remaining_task_seconds(deadline, phase="host telemetry preflight"),
    )
    output = result.stdout
    if result.stdout.strip() and result.stderr.strip():
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            output = result.stdout.rstrip() + "\n\n[stderr]\n" + result.stderr
        else:
            if isinstance(payload, dict):
                payload["preflight_stderr"] = result.stderr.rstrip()
                output = json.dumps(payload, indent=2)
    elif not result.stdout.strip():
        output = result.stderr
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
    deadline: float | None = None,
) -> None:
    openclaw = _require_executable("openclaw")
    env = _openclaw_env(openclaw_home, sidecar_port, config, workspace)
    plugin_dir = config.repo_root / config.bundle.plugin_source
    if deadline is None:
        _ensure_plugin_built(trace_dir, plugin_dir)
    else:
        _ensure_plugin_built(trace_dir, plugin_dir, deadline=deadline)
    plugin_install_dir = _stage_plugin_for_openclaw_if_needed(
        trace_dir=trace_dir,
        plugin_dir=plugin_dir,
    )
    _remaining_task_seconds(deadline, phase="plugin staging")
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
        _run_logged_before_deadline(
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
                _LOCAL_PROXY_API_KEY,
                "--custom-model-id",
                config.llm.model,
            ],
            env,
            log,
            "openclaw_onboard",
            deadline,
        )
        _run_logged_before_deadline(
            [openclaw, "plugins", "install", "--link", str(plugin_install_dir)],
            env,
            log,
            "plugin_install",
            deadline,
        )
        _run_logged_before_deadline(
            [openclaw, "plugins", "enable", "agent-scheduler"],
            env,
            log,
            "plugin_enable",
            deadline,
        )
        patch = subprocess.run(
            [openclaw, "config", "patch", "--stdin"],
            input=sandbox_config,
            stdout=log,
            stderr=log,
            text=True,
            env=env,
            timeout=_remaining_task_seconds(deadline, phase="OpenClaw config patch"),
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
    task_deadline: float | None = None,
) -> int:
    _remaining_task_seconds(task_deadline, phase="agent startup")
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
            "CLAW_LAUNCH_MODE": "fork-exec",
            "CLAW_LAUNCH_DEBUG": "1",
        }
    )
    prompt_path = trace_dir / "agent_prompt.txt"
    stdout_path = trace_dir / "agent-stdout.txt"
    stderr_path = trace_dir / "agent-stderr.txt"
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")
    _log(f"[agent] starting (trace: {trace_dir})")

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
    try:
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name == "posix"),
        )
    except BaseException:
        stdout_file.close()
        stderr_file.close()
        raise
    discovery.start()

    # ── Tee agent output to trace files + console ──────────────────
    _write_lock = threading.Lock()

    # ── Patterns to suppress from console (still written to log file) ──
    # These are OpenClaw internal operational logs that add noise without
    # meaningful per-turn information.  The plugin's consoleMode=verbose
    # provides the structured turn-by-turn output.
    _NOISY_PATTERNS = [
        "[provider-transport-fetch]",
        "[model-fetch]",
    ]

    def _is_noisy(line: str) -> bool:
        return any(p in line for p in _NOISY_PATTERNS)

    def _tee(pipe: Any, log_file: Any, tag: str) -> None:
        try:
            for line in pipe:
                with _write_lock:
                    log_file.write(line)
                    log_file.flush()
                if not _is_noisy(line):
                    _log(f"[{tag}] {line.rstrip()}")
        except (ValueError, OSError):
            pass

    tee_stdout = threading.Thread(
        target=_tee,
        args=(process.stdout, stdout_file, "agent"),
        daemon=True,
    )
    tee_stderr = threading.Thread(
        target=_tee,
        args=(process.stderr, stderr_file, "agent:err"),
        daemon=True,
    )
    tee_stdout.start()
    tee_stderr.start()

    try:
        agent_deadline = (
            time.monotonic() + config.batch.agent_timeout_seconds
            if config.batch.agent_timeout_seconds > 0
            else None
        )
        effective_deadline = min(
            value
            for value in (task_deadline, agent_deadline)
            if value is not None
        ) if task_deadline is not None or agent_deadline is not None else None
        timeout = (
            max(0.001, effective_deadline - time.monotonic())
            if effective_deadline is not None
            else None
        )
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_agent_process_and_confirm(process)
        scope = (
            "task"
            if task_deadline is not None
            and (agent_deadline is None or task_deadline <= agent_deadline)
            else "agent"
        )
        configured_seconds = (
            config.batch.task_timeout_seconds
            if scope == "task"
            else config.batch.agent_timeout_seconds
        )
        message = (
            f"{scope} timed out after {configured_seconds}s"
            if configured_seconds > 0
            else f"{scope} timed out"
        )
        _write_timeout_record(
            trace_dir,
            scope=scope,
            message=message,
            configured_seconds=configured_seconds,
        )
        return 124
    except KeyboardInterrupt:
        # Ctrl-C is also fail-closed: do not publish a KB snapshot unless the
        # agent process group has definitely stopped.
        _kill_agent_process_and_confirm(process)
        raise
    finally:
        # All cleanup steps are protected from secondary interrupts so
        # Ctrl-C never leaves the process in an unreachable zombie state.
        stop_discovery.set()
        _join_thread_safe(discovery, timeout=2)

        # Close process pipes so tee threads exit their read loops
        for pipe_attr in ("stdout", "stderr"):
            pipe = getattr(process, pipe_attr, None)
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

        _join_thread_safe(tee_stdout, timeout=2)
        _join_thread_safe(tee_stderr, timeout=2)

        for f in (stdout_file, stderr_file):
            try:
                f.close()
            except OSError:
                pass


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
            "tools": {
                # Background process sessions split one logical command across
                # exec/process calls and therefore cannot provide an exact
                # one-call/one-process resource lifecycle.  Keep this required
                # telemetry route synchronous.
                "deny": ["process"],
                "exec": {
                    # OpenClaw's supported sandbox-exec PATH extension.  The
                    # launcher repeats the complete value so the forked shell
                    # also inherits it regardless of gateway sanitisation.
                    "pathPrepend": _SANDBOX_TASK_PATH.split(":"),
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
                            "reportTimeoutMs": 10000,
                            "failOpen": True,
                            "sendRawParams": False,
                            "recordRawTrace": False,
                            "logLevel": "warn",
                            "consoleMode": "verbose",
                            "executionBackend": "managed-wrapper",
                            "launcherPath": "/workspace/.claw/bin/claw-launch",
                            "launcherInterpreter": "/bin/sh",
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
                "CLAW_LAUNCH_MODE": "fork-exec",
                "CLAW_LAUNCH_DEBUG": "1",
            },
        },
        indent=2,
    )


def _sandbox_container_prefix(workspace: Path) -> str:
    return sandbox_container_prefix(workspace)


def _docker_platform_args(platform: str) -> list[str]:
    return ["--platform", platform] if platform else []


def _cleanup_openclaw_sandbox_containers(
    trace_dir: Path,
    workspace: Path,
    *,
    timeout_seconds: float = _TASK_CLEANUP_TIMEOUT_SECONDS,
    strict: bool = False,
) -> None:
    """Remove stale OpenClaw sandbox containers for this task workspace.

    OpenClaw scopes sandbox containers by prefix.  Reusing a stale container can
    leave Docker exec stuck with a host workspace cwd that is outside the
    container mount namespace, so start each SWE-Rebench task from a fresh
    sandbox container.
    """
    docker = _require_executable("docker")
    prefix = _sandbox_container_prefix(workspace)
    log_path = trace_dir / "sandbox-container-cleanup.log"
    cleanup_deadline = time.monotonic() + max(0.001, timeout_seconds)
    try:
        listed = subprocess.run(
            [docker, "ps", "-aq", "--filter", f"name={prefix}"],
            capture_output=True,
            text=True,
            timeout=max(0.001, cleanup_deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired as exc:
        _write_text(log_path, f"docker_ps_timed_out prefix={prefix}\n")
        if strict:
            raise ContainerCleanupError(
                f"timed out listing sandbox containers for prefix {prefix}"
            ) from exc
        return
    if listed.returncode != 0:
        _write_text(
            log_path,
            f"docker_ps_failed exit={listed.returncode}\n{listed.stdout}{listed.stderr}",
        )
        if strict:
            raise ContainerCleanupError(
                f"failed to list sandbox containers for prefix {prefix}"
            )
        return

    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not container_ids:
        _write_text(log_path, f"no stale containers for prefix {prefix}\n")
        return

    try:
        removed = subprocess.run(
            [docker, "rm", "-f", *container_ids],
            capture_output=True,
            text=True,
            timeout=max(0.001, cleanup_deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired as exc:
        _write_text(
            log_path,
            f"docker_rm_timed_out prefix={prefix}\n"
            f"containers={json.dumps(container_ids)}\n",
        )
        if strict:
            raise ContainerCleanupError(
                f"timed out removing sandbox containers for prefix {prefix}"
            ) from exc
        return
    _write_text(
        log_path,
        f"prefix={prefix}\ncontainers={json.dumps(container_ids)}\n"
        f"exit={removed.returncode}\n{removed.stdout}{removed.stderr}",
    )
    if strict and removed.returncode != 0:
        raise ContainerCleanupError(
            f"failed to remove sandbox containers for prefix {prefix}: "
            f"exit={removed.returncode}"
        )


def _ensure_plugin_built(
    trace_dir: Path,
    plugin_dir: Path,
    *,
    deadline: float | None = None,
) -> None:
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
            timeout=_remaining_task_seconds(deadline, phase="plugin build"),
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
    # A sudo -E benchmark must not inherit credentials/targets for the user's
    # long-running gateway. This run creates an isolated OPENCLAW_HOME; any
    # stale gateway variable can make sessions_spawn announce to the local
    # gateway with a mismatched token even though ``openclaw agent --local`` is
    # healthy.
    for name in [key for key in env if key.startswith("OPENCLAW_GATEWAY_")]:
        env.pop(name, None)
    # OpenClaw communicates with the local Scheduler proxy.  It must not pass
    # the real upstream credential to its own process or Docker sandbox.
    env.pop("AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY", None)
    env.update(
        {
            "OPENCLAW_HOME": str(openclaw_home),
            "OPENCLAW_STATE_DIR": str(openclaw_home / ".openclaw"),
            "OPENCLAW_CONFIG_PATH": str(
                openclaw_home / ".openclaw" / "openclaw.json"
            ),
            "OPENCLAW_WORKSPACE_DIR": str(
                workspace
                if workspace is not None
                else openclaw_home / ".openclaw" / "workspace"
            ),
            "VLLM_API_KEY": _LOCAL_PROXY_API_KEY,
            "LLM_API_KEY": _LOCAL_PROXY_API_KEY,
            "CLAW_SCHEDULER_ENDPOINT": f"http://host.docker.internal:{sidecar_port}",
            "CLAW_EXEC_WORKDIR": "/workspace",
            "CLAW_SANDBOX_HOST_WORKSPACE": str(workspace) if workspace is not None else "",
            "CLAW_SANDBOX_CONTAINER_WORKSPACE": "/workspace",
            "CLAW_ENABLE_CGROUP": "1",
            "CLAW_LAUNCH_MODE": "fork-exec",
            "CLAW_LAUNCH_DEBUG": "1",
        }
    )
    if config.docker.platform:
        # OpenClaw 2026.7.x rejects ``sandbox.docker.platform``.  Its Docker
        # CLI subprocess inherits this standard Docker setting, while every
        # runner-owned pull/create/run still gets an explicit ``--platform``.
        env["DOCKER_DEFAULT_PLATFORM"] = config.docker.platform
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
        timeout=_DISCOVERY_COMMAND_TIMEOUT_SECONDS,
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
        timeout=_DISCOVERY_COMMAND_TIMEOUT_SECONDS,
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
        timeout=_DISCOVERY_COMMAND_TIMEOUT_SECONDS,
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


def _write_task_inputs(
    trace_dir: Path,
    task: TaskDef,
    config: RunnerConfig,
    workspace: Path,
    bundle_dir: Path | None = None,
    *,
    shared_kb_dir: Path | None = None,
) -> None:
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
    bundle_fingerprint = _read_json_object(
        bundle_dir / "bundle-source-fingerprint.json"
        if bundle_dir is not None
        else None
    )
    _write_text(
        trace_dir / "task_manifest.json",
        json.dumps(
            {
                "task_id": task.instance_id,
                "repo": task_repo_key(task),
                "image": task.image,
                "base_commit": task.base_commit,
                "model": config.llm.model,
                "openclaw_model_ref": config.llm.openclaw_model_ref,
                "runtime_mode": "host-openclaw-sandbox",
                "workspace": str(workspace),
                "runner_config": str(config.config_path or ""),
                "shared_kb_dir": str(shared_kb_dir) if shared_kb_dir else None,
                "bundle_source_fingerprint": bundle_fingerprint,
                "problem_statement_bytes": len(task.problem_statement),
                "hint_text_bytes": len(task.hint_text),
            },
            indent=2,
        )
        + "\n",
    )


def _read_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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


def _cleanup_runtime_artifacts(
    workspace: Path,
    *,
    deadline: float | None = None,
) -> None:
    for name in _RUNTIME_ARTIFACTS:
        _remaining_task_seconds(deadline, phase="runtime artifact cleanup")
        path = workspace / name
        if not path.exists():
            continue
        if _git_tracks_path(workspace, name, deadline=deadline):
            continue
        if path.is_dir():
            shutil.rmtree(path, onerror=_chmod_and_retry)
        else:
            path.unlink()


def _git_tracks_path(
    workspace: Path,
    relative_path: str,
    *,
    deadline: float | None = None,
) -> bool:
    result = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--error-unmatch", "--", relative_path],
        capture_output=True,
        text=True,
        timeout=_remaining_task_seconds(deadline, phase="tracked artifact check"),
    )
    return result.returncode == 0


def _collect_patch(
    trace_dir: Path,
    workspace: Path,
    task: TaskDef,
    *,
    deadline: float | None = None,
) -> None:
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--short"],
        capture_output=True,
        text=True,
        timeout=_remaining_task_seconds(deadline, phase="git status collection"),
    )
    diff_stat = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--stat"],
        capture_output=True,
        text=True,
        timeout=_remaining_task_seconds(deadline, phase="git diff stat collection"),
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
    patch = subprocess.run(
        diff_cmd,
        capture_output=True,
        text=True,
        timeout=_remaining_task_seconds(deadline, phase="model patch collection"),
    )
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


def _write_timeout_record(
    trace_dir: Path,
    *,
    scope: str,
    message: str,
    configured_seconds: int,
) -> None:
    _write_text(
        trace_dir / "task-timeout.json",
        json.dumps(
            {
                "scope": scope,
                "message": message,
                "configured_seconds": configured_seconds,
            },
            indent=2,
        )
        + "\n",
    )


def _wait_ready(port: int, *, timeout_seconds: float | None = None) -> None:
    task_budget_limited = timeout_seconds is not None
    deadline = time.monotonic() + (
        max(0.001, timeout_seconds) if timeout_seconds is not None else 60.0
    )
    url = f"http://127.0.0.1:{port}/health/ready"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            with urllib.request.urlopen(
                url,
                timeout=max(0.001, min(1.0, remaining)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if (
                    response.status == 200
                    and isinstance(payload, dict)
                    and payload.get("service") == "clawtune-scheduler"
                    and payload.get("schema_version") == "scheduler.health.v1"
                    and payload.get("ready") is True
                ):
                    return
        except Exception:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.5, remaining))
    if task_budget_limited:
        raise TaskDeadlineExceeded(
            "task timed out during sidecar startup; whole-task wall-clock "
            "budget exhausted"
        )
    raise RuntimeError(f"sidecar_not_ready port={port}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reset_directory(
    path: Path,
    *,
    docker_cleanup_image: str | None = None,
    docker_platform: str = "",
    deadline: float | None = None,
) -> None:
    if path.exists():
        try:
            shutil.rmtree(path, onerror=_chmod_and_retry)
        except PermissionError:
            if docker_cleanup_image is None:
                raise
            _reset_directory_with_docker(
                path,
                docker_cleanup_image,
                docker_platform,
                deadline=deadline,
            )
    _remaining_task_seconds(deadline, phase="workspace reset")
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
    try:
        function(path)
    except OSError as exc:
        if function is os.rmdir and getattr(exc, "errno", None) in {39, errno.ENOTEMPTY}:
            shutil.rmtree(path, onerror=_chmod_and_retry)
            return
        raise


def _reset_directory_with_docker(
    path: Path,
    image: str,
    platform: str = "",
    *,
    deadline: float | None = None,
) -> None:
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
            *_docker_platform_args(platform),
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
        timeout=_remaining_task_seconds(deadline, phase="Docker workspace reset"),
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


def _run_checked(
    cmd: list[str],
    label: str,
    *,
    timeout: float | None = None,
) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{label}_failed exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def _run_logged_before_deadline(
    cmd: list[str],
    env: dict[str, str],
    log: Any,
    label: str,
    deadline: float | None,
) -> None:
    if deadline is None:
        _run_logged(cmd, env, log, label)
        return
    _run_logged(
        cmd,
        env,
        log,
        label,
        timeout=_remaining_task_seconds(deadline, phase=label),
    )


def _run_logged(
    cmd: list[str],
    env: dict[str, str],
    log: Any,
    label: str,
    *,
    timeout: float | None = None,
) -> None:
    log.write(f"=== {label} ===\n")
    result = subprocess.run(
        cmd,
        stdout=log,
        stderr=log,
        text=True,
        env=env,
        timeout=timeout,
    )
    log.write(f"\nexit={result.returncode}\n\n")
    if result.returncode != 0:
        raise RuntimeError(f"{label}_failed exit={result.returncode}")


def _join_thread_safe(thread: threading.Thread, *, timeout: float | None = None) -> None:
    """Join a thread, ignoring KeyboardInterrupt so cleanup is never abandoned."""
    try:
        thread.join(timeout=timeout)
    except KeyboardInterrupt:
        pass


def _kill_agent_process_and_confirm(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix" and isinstance(process, _SUBPROCESS_POPEN_TYPE):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise ContainerCleanupError(
            "OpenClaw agent process group did not exit within 5s after kill"
        ) from exc


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise RuntimeError("sidecar did not exit after terminate and kill")
    if process.poll() is None:
        raise RuntimeError("sidecar exit could not be confirmed")
