"""
Docker container management for swe-rebench task execution.

Uses the Docker SDK for Python (``docker`` package) to pull images,
create containers with volume mounts, wait for completion, and clean up.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swe_rebench.config import DockerConfig
from swe_rebench.sandbox import sandbox_container_prefix


def _docker_host_socket(host: str) -> str | None:
    """Extract a Unix socket path from a Docker host URL.

    Returns ``None`` if the host is not a Unix socket (e.g. TCP).
    """
    if host.startswith("unix://"):
        return host[len("unix://"):]
    # Also handle plain paths used by some Docker clients.
    if host.startswith("/") and not host.startswith(("tcp://", "npipe://", "fd://")):
        return host
    return None


def _docker_platform_args(platform: str) -> list[str]:
    return ["--platform", platform] if platform else []


def _resolve_kernel_build(build_path: Path) -> Path:
    """Resolve the host kernel's ``build`` link, requiring its target."""
    return build_path.resolve(strict=True)


def _container_kernel_header_volumes(
    docker_host: str,
    *,
    kernel_release: str | None = None,
    modules_root: Path = Path("/lib/modules"),
    header_root: Path = Path("/usr/src"),
) -> dict[str, dict[str, str]]:
    """Return narrow read-only mounts for the local host's running kernel.

    BCC compiles against the *host* kernel even though it runs in the task
    container.  Distribution headers installed in the image can therefore be
    the wrong version.  Only local Linux Docker daemons are eligible: paths on
    this machine are not meaningful to a remote daemon.

    Discovery is deliberately fail-open because Stage-2 is best effort in the
    container runtime.  The resolved build target must remain below /usr/src;
    an unexpected link can never turn into an arbitrary host-path mount.
    """
    if sys.platform != "linux" or _docker_host_socket(docker_host) is None:
        return {}

    try:
        release = kernel_release if kernel_release is not None else os.uname().release
    except (AttributeError, OSError):
        return {}
    if not release or release in {".", ".."} or "/" in release or "\\" in release:
        return {}

    module_dir = modules_root / release
    if not module_dir.is_dir():
        return {}

    try:
        resolved_header_root = header_root.resolve(strict=True)
        header_dir = _resolve_kernel_build(module_dir / "build")
    except (OSError, RuntimeError):
        return {}
    if not header_dir.is_dir() or (
        header_dir != resolved_header_root
        and resolved_header_root not in header_dir.parents
    ):
        _log(
            "[warn] container kernel headers skipped: resolved build target "
            f"is outside {resolved_header_root}: {header_dir}"
        )
        return {}

    module_path = str(module_dir)
    header_path = str(header_dir)
    _log(
        "[info] container kernel headers: mounting host "
        f"{module_path} and {header_path} read-only"
    )
    return {
        module_path: {"bind": module_path, "mode": "ro"},
        header_path: {"bind": header_path, "mode": "ro"},
    }


@dataclass
class ContainerResult:
    """Outcome of a single container run."""
    task_id: str
    image: str
    exit_code: int | None
    error: str | None = None
    trace_dir: Path | None = None
    trace_files: list[Path] = field(default_factory=list)
    duration_seconds: float = 0.0
    container_id: str | None = None


def get_docker_client(config: DockerConfig) -> Any:
    """Return a configured Docker SDK client.

    Falls back gracefully if the ``docker`` package is not installed.
    """
    try:
        import docker  # type: ignore[import-untyped]
        if config.host.startswith("unix://"):
            return docker.DockerClient(base_url=config.host)
        return docker.DockerClient(base_url=config.host)
    except ImportError:
        _log("[warn] docker Python SDK not installed; using CLI fallback.")
        return None


def pull_image(client: Any, image: str, policy: str = "missing", platform: str = "") -> bool:
    """Pull a Docker image.  Returns True on success."""
    if policy == "never":
        return True
    if client is not None:
        try:
            if policy == "always":
                client.images.pull(image, platform=platform or None)
            elif policy == "missing":
                if platform:
                    client.images.pull(image, platform=platform or None)
                else:
                    try:
                        client.images.get(image)
                    except Exception:
                        client.images.pull(image)
            return True
        except Exception as exc:
            _log(f"[error] pull {image}: {exc}")
            return False
    else:
        import subprocess
        flag = "--always" if policy == "always" else ""
        cmd = ["docker", "pull", *_docker_platform_args(platform)]
        if flag:
            cmd.append(flag)
        cmd.append(image)
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0


def run_container(
    client: Any,
    image: str,
    task_id: str,
    bundle_dir: Path,
    trace_dir: Path,
    problem_statement: str,
    config: DockerConfig,
    llm_api_key: str,
    llm_upstream_url: str,
    llm_model: str = "",
    openclaw_model_ref: str = "",
    timeout_seconds: int = 1800,
    env_extra: dict[str, str] | None = None,
    stage2_required: bool = False,
) -> ContainerResult:
    """Run a single task container and return the result.

    Parameters
    ----------
    client:
        Docker SDK client, or ``None`` for CLI fallback.
    image:
        swe-rebench Docker image name.
    task_id:
        Unique task identifier (used for trace directory naming).
    bundle_dir:
        Host path to the runtime bundle (mounted at ``/claw``).
    trace_dir:
        Host path for trace output (mounted at ``/traces``).
    problem_statement:
        The task problem statement passed as ``PROBLEM_STATEMENT`` env var.
    config:
        Docker configuration.
    llm_api_key:
        LLM API key.
    llm_upstream_url:
        LLM upstream base URL.
    llm_model:
        Provider model name exposed by the sidecar.
    openclaw_model_ref:
        OpenClaw model reference passed to the agent command.
    timeout_seconds:
        Maximum wall-clock time for the container.
    env_extra:
        Additional environment variables to pass.
    stage2_required:
        Whether the in-container sidecar must fail closed when Stage-2 eBPF
        telemetry cannot be started.  False keeps container-openclaw usable
        through its documented best-effort fallback.
    """
    trace_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    docker_exec_container_prefix = sandbox_container_prefix(f"docker:{task_id}")

    environment = {
        "PROBLEM_STATEMENT": problem_statement,
        "TASK_INSTANCE_ID": task_id,
        "TASK_IMAGE": image,
        "TASK_BASE_COMMIT": "",
        "TASK_HINT_TEXT": "",
        "LLM_API_KEY": llm_api_key,
        "LLM_UPSTREAM_BASE_URL": llm_upstream_url,
        "LLM_MODEL": llm_model,
        "OPENCLAW_MODEL_REF": openclaw_model_ref,
        "CLAW_CGROUP_REQUIRED": "1" if config.cgroup_required else "0",
        "CLAW_CGROUP_ROOT": "/sys/fs/cgroup/claw",
        # Enable DockerExecObserver so read/write/edit tools get
        # independent PID/cgroup attribution via docker-exec events.
        "AGENT_SCHEDULER_DOCKER_EXEC_OBSERVER": "true",
        "AGENT_SCHEDULER_DOCKER_EXEC_CONTAINER_PREFIX": docker_exec_container_prefix,
    }
    if env_extra:
        environment.update(env_extra)
    # Runtime policy is runner-owned. Dataset-provided task environment must
    # not silently turn a best-effort container run into fail-closed Stage-2
    # (or weaken an explicitly required run).
    environment["AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED"] = (
        "true" if stage2_required else "false"
    )

    volumes = {
        str(bundle_dir.resolve()): {"bind": "/claw", "mode": "ro"},
        str(trace_dir.resolve()): {"bind": "/traces", "mode": "rw"},
    }
    # Mount Docker socket so OpenClaw can use Docker sandbox and
    # the sidecar's DockerExecObserver can watch exec events.
    # Derive the socket path from the configured Docker host.
    _host_socket = _docker_host_socket(config.host)
    if _host_socket is not None and os.path.exists(_host_socket):
        volumes[_host_socket] = {"bind": "/var/run/docker.sock", "mode": "rw"}
    if config.cgroup_mount_rw:
        volumes["/sys/fs/cgroup"] = {"bind": "/sys/fs/cgroup", "mode": "rw"}
    # BCC must compile for the host kernel. Mount only the exact module tree
    # and resolved header directory, and only for a local Linux Docker daemon.
    # Missing or suspicious paths leave Stage-2 in its existing fail-open mode.
    volumes.update(_container_kernel_header_volumes(config.host))

    if client is not None:
        return _run_container_sdk(
            client, image, task_id, volumes, environment,
            config, timeout_seconds, trace_dir, started,
        )
    else:
        return _run_container_cli(
            image, task_id, volumes, environment,
            config, timeout_seconds, trace_dir, started,
        )


def _run_container_sdk(
    client: Any,
    image: str,
    task_id: str,
    volumes: dict[str, dict[str, str]],
    environment: dict[str, str],
    config: DockerConfig,
    timeout_seconds: int,
    trace_dir: Path,
    started: float,
) -> ContainerResult:
    """Run via Docker Python SDK."""
    import docker  # type: ignore[import-untyped]

    mem_limit: str | None = config.memory_limit if config.memory_limit else None
    nano_cpus: int | None = int(config.cpus * 1e9) if config.cpus else None

    try:
        container = client.containers.run(
            image=image,
            entrypoint=["/claw/entrypoint.sh"],
            volumes=volumes,
            environment=environment,
            detach=True,
            mem_limit=mem_limit,
            nano_cpus=nano_cpus,
            network_mode=config.network_mode,
            cap_add=config.cap_add if config.cap_add else None,
            dns=config.dns_servers if config.dns_servers else None,
            privileged=config.privileged,
            cgroupns=config.cgroupns_mode or None,
            platform=config.platform or None,
        )
        container_id = container.id
        _log(f"[{task_id}] container {container_id[:12]} started")
        log_thread = _stream_sdk_container_logs(container, task_id)

        try:
            result = container.wait(timeout=timeout_seconds if timeout_seconds > 0 else None)
            exit_code = result.get("StatusCode", -1)
            error = None
        except (docker.errors.APIError, Exception) as exc:
            _log(f"[{task_id}] wait error: {exc}")
            try:
                container.kill()
            except Exception:
                pass
            exit_code = 124
            error = f"container_timeout_or_wait_failed: {exc}"
        finally:
            _join_log_thread(log_thread)
            _write_sdk_container_log(container, trace_dir)
            try:
                container.remove(force=True)
            except Exception:
                pass

    except Exception as exc:
        _log(f"[{task_id}] container failed: {exc}")
        duration = time.monotonic() - started
        return ContainerResult(
            task_id=task_id, image=image, exit_code=-1,
            error=str(exc), trace_dir=trace_dir,
            duration_seconds=duration,
        )

    duration = time.monotonic() - started
    trace_files = _find_traces(trace_dir)
    return ContainerResult(
        task_id=task_id, image=image, exit_code=exit_code,
        error=error, trace_dir=trace_dir, trace_files=trace_files,
        duration_seconds=duration, container_id=container_id,
    )


def _run_container_cli(
    image: str,
    task_id: str,
    volumes: dict[str, dict[str, str]],
    environment: dict[str, str],
    config: DockerConfig,
    timeout_seconds: int,
    trace_dir: Path,
    started: float,
) -> ContainerResult:
    """Run via ``docker`` CLI as fallback."""
    import subprocess

    cmd = ["docker", "run", "--detach", *_docker_platform_args(config.platform)]

    # Volumes
    for host_path, vol_cfg in volumes.items():
        mode = vol_cfg.get("mode", "rw")
        cmd.extend(["-v", f"{host_path}:{vol_cfg['bind']}:{mode}"])

    # Environment
    for k, v in environment.items():
        cmd.extend(["-e", f"{k}={v}"])

    # Entrypoint
    cmd.extend(["--entrypoint", "/claw/entrypoint.sh"])

    # Resource limits
    if config.memory_limit:
        cmd.extend(["--memory", config.memory_limit])
    if config.cpus:
        cmd.extend(["--cpus", str(config.cpus)])

    # Network
    if config.network_mode:
        cmd.extend(["--network", config.network_mode])

    # DNS
    for dns in config.dns_servers:
        cmd.extend(["--dns", dns])

    # Caps
    for cap in config.cap_add:
        cmd.extend(["--cap-add", cap])

    if config.privileged:
        cmd.append("--privileged")
    if config.cgroupns_mode:
        cmd.extend(["--cgroupns", config.cgroupns_mode])

    cmd.append(image)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        container_id = result.stdout.strip()
        _log(f"[{task_id}] container {container_id[:12]} started (CLI fallback)")
        log_process, log_thread = _stream_cli_container_logs(container_id, task_id)

        # Wait for container, then capture logs before removing it.
        wait_cmd = ["docker", "wait", container_id]
        wait_result: subprocess.CompletedProcess[str]
        if timeout_seconds > 0:
            try:
                wait_result = subprocess.run(
                    wait_cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                _log(f"[{task_id}] timeout, killing container")
                subprocess.run(["docker", "kill", container_id], capture_output=True)
                subprocess.run(["docker", "wait", container_id], capture_output=True, text=True)
                _stop_cli_log_stream(log_process, log_thread)
                _write_cli_container_log(container_id, trace_dir)
                subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, text=True)
                duration = time.monotonic() - started
                return ContainerResult(
                    task_id=task_id, image=image, exit_code=124,
                    error=f"container timed out after {timeout_seconds}s",
                    trace_dir=trace_dir,
                    trace_files=_find_traces(trace_dir),
                    duration_seconds=duration,
                    container_id=container_id,
                )
        else:
            wait_result = subprocess.run(wait_cmd, capture_output=True, text=True, check=True)
        exit_code = int(wait_result.stdout.strip())
        _stop_cli_log_stream(log_process, log_thread)
        _write_cli_container_log(container_id, trace_dir)
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, text=True)

    except subprocess.CalledProcessError as exc:
        _log(f"[{task_id}] CLI error: {exc}")
        if exc.stderr:
            _log(f"  stderr: {exc.stderr.strip()}")
        duration = time.monotonic() - started
        return ContainerResult(
            task_id=task_id, image=image, exit_code=-1,
            error=str(exc), trace_dir=trace_dir,
            duration_seconds=duration,
        )

    duration = time.monotonic() - started
    trace_files = _find_traces(trace_dir)
    return ContainerResult(
        task_id=task_id, image=image, exit_code=exit_code,
        trace_dir=trace_dir, trace_files=trace_files,
        duration_seconds=duration, container_id=container_id,
    )


def _find_traces(directory: Path) -> list[Path]:
    """Find all ``*.jsonl`` trace files in *directory*."""
    if not directory.exists():
        return []
    return sorted(directory.glob("*.jsonl"))


def _write_sdk_container_log(container: Any, trace_dir: Path) -> None:
    try:
        raw = container.logs(stdout=True, stderr=True, timestamps=False)
    except Exception as exc:
        (trace_dir / "container.log").write_text(f"cannot read container logs: {exc}\n", encoding="utf-8")
        return
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    (trace_dir / "container.log").write_text(text, encoding="utf-8")


def _stream_sdk_container_logs(container: Any, task_id: str) -> threading.Thread:
    """Print Docker output as it arrives while retaining the trace copy later."""
    def forward() -> None:
        try:
            for chunk in container.logs(stdout=True, stderr=True, follow=True, stream=True):
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                for line in text.rstrip("\n").splitlines():
                    _log(f"[{task_id}] {line}")
        except Exception as exc:
            # Log streaming is observability only; container.wait remains authoritative.
            _log(f"[{task_id}] live container log stream ended: {exc}")

    thread = threading.Thread(target=forward, name=f"docker-logs-{task_id}", daemon=True)
    thread.start()
    return thread


def _join_log_thread(thread: threading.Thread) -> None:
    """Give a completed container a brief chance to flush its final log lines."""
    thread.join(timeout=5)


def _stream_cli_container_logs(container_id: str, task_id: str) -> tuple[Any | None, threading.Thread | None]:
    """Follow CLI fallback logs without delaying the container wait."""
    import subprocess

    try:
        process = subprocess.Popen(
            ["docker", "logs", "--follow", container_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        _log(f"[{task_id}] cannot start live container log stream: {exc}")
        return None, None

    def forward() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            _log(f"[{task_id}] {line.rstrip()}")

    thread = threading.Thread(target=forward, name=f"docker-cli-logs-{task_id}", daemon=True)
    thread.start()
    return process, thread


def _stop_cli_log_stream(process: Any | None, thread: threading.Thread | None) -> None:
    if process is None:
        return
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
    if thread is not None:
        _join_log_thread(thread)


def _write_cli_container_log(container_id: str, trace_dir: Path) -> None:
    import subprocess

    result = subprocess.run(
        ["docker", "logs", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    text = ""
    if result.stdout:
        text += result.stdout
    if result.stderr:
        text += result.stderr
    if not text and result.returncode != 0:
        text = f"docker logs failed exit={result.returncode}\n"
    (trace_dir / "container.log").write_text(text, encoding="utf-8")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
