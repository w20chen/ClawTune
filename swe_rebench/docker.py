"""
Docker container management for swe-rebench task execution.

Uses the Docker SDK for Python (``docker`` package) to pull images,
create containers with volume mounts, wait for completion, and clean up.
"""

from __future__ import annotations

import os
import subprocess
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

    Discovery is deliberately fail-open because eBPF telemetry is best effort in the
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


def _container_tracefs_volumes(
    docker_host: str,
    *,
    tracefs_roots: tuple[Path, ...] = (
        Path("/sys/kernel/tracing"),
        Path("/sys/kernel/debug/tracing"),
    ),
) -> dict[str, dict[str, str]]:
    """Expose the local host tracefs needed by an in-container BCC collector.

    A privileged container still has its own mount namespace.  Its tracefs
    directory can therefore exist but be empty, making BCC compilation appear
    healthy until the first tracepoint attach fails.  Bind only a known tracefs
    root that exposes the scheduler exit tracepoint and dynamic-kprobe control
    file used by the collector.

    The mount is read-write because BCC may create dynamic kprobe events there.
    This does not expand the default security boundary: container-openclaw's
    complete telemetry configuration is already privileged.  Remote Docker
    daemons and non-Linux runners are skipped because their host paths are not
    local to this process.
    """
    if sys.platform != "linux" or _docker_host_socket(docker_host) is None:
        return {}

    tracepoint = Path("events/sched/sched_process_exit/id")
    for root in tracefs_roots:
        try:
            if (
                not root.is_dir()
                or not (root / tracepoint).is_file()
                or not (root / "kprobe_events").is_file()
            ):
                continue
        except OSError:
            continue
        root_path = str(root)
        _log(f"[info] container tracefs: mounting host {root_path} read-write")
        return {
            root_path: {"bind": root_path, "mode": "rw"},
        }
    return {}


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


class ContainerCleanupError(RuntimeError):
    """A task container's exit/removal could not be confirmed."""


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
        flag = "--always" if policy == "always" else ""
        cmd = ["docker", "pull", *_docker_platform_args(platform)]
        if flag:
            cmd.append(flag)
        cmd.append(image)
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0


def local_image_available(client: Any, image: str, platform: str = "") -> bool:
    """Return whether a local image exists and matches the requested platform."""

    expected = _normalized_image_platform(platform)
    try:
        if client is not None:
            attrs = client.images.get(image).attrs
            if expected is None:
                return True
            actual = _normalized_image_platform(
                f"{attrs.get('Os', '')}/{attrs.get('Architecture', '')}"
            )
            return actual == expected

        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}",
                image,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        if expected is None:
            return True
        return _normalized_image_platform(result.stdout.strip()) == expected
    except Exception:
        return False


def _normalized_image_platform(value: str) -> tuple[str, str] | None:
    parts = value.strip().lower().split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    aliases = {
        "x86_64": "amd64",
        "x64": "amd64",
        "aarch64": "arm64",
    }
    return parts[0], aliases.get(parts[1], parts[1])


def run_container(
    client: Any,
    image: str,
    task_id: str,
    runtime_assets_dir: Path,
    trace_dir: Path,
    problem_statement: str,
    config: DockerConfig,
    llm_api_key: str,
    llm_upstream_url: str,
    llm_model: str = "",
    openclaw_model_ref: str = "",
    timeout_seconds: int = 1800,
    env_extra: dict[str, str] | None = None,
    ebpf_required: bool = False,
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
    runtime_assets_dir:
        Host path to the runtime assets (mounted at ``/clawtune``).
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
    ebpf_required:
        Whether the in-container sidecar must fail closed when eBPF
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
        "CLAWTUNE_CGROUP_REQUIRED": "1" if config.cgroup_required else "0",
        "CLAWTUNE_CGROUP_ROOT": "/sys/fs/cgroup/clawtune",
        # Enable DockerExecObserver so read/write/edit tools get
        # independent PID/cgroup attribution via docker-exec events.
        "CLAWTUNE_DOCKER_EXEC_OBSERVER": "true",
        "CLAWTUNE_DOCKER_EXEC_CONTAINER_PREFIX": docker_exec_container_prefix,
    }
    if env_extra:
        environment.update(env_extra)
    # Runtime policy is runner-owned. Dataset-provided task environment must
    # not silently turn a best-effort container run into fail-closed eBPF telemetry
    # (or weaken an explicitly required run).
    environment["CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED"] = (
        "true" if ebpf_required else "false"
    )

    volumes = {
        str(runtime_assets_dir.resolve()): {"bind": "/clawtune", "mode": "ro"},
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
    # Missing or suspicious paths leave eBPF telemetry in its existing fail-open mode.
    volumes.update(_container_kernel_header_volumes(config.host))
    # Privileged containers do not inherit the host tracefs mount through their
    # mount namespace.  Without this narrow bind, BCC imports and compiles but
    # every tracepoint attach fails at first use.
    volumes.update(_container_tracefs_volumes(config.host))

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
    mem_limit: str | None = config.memory_limit if config.memory_limit else None
    nano_cpus: int | None = int(config.cpus * 1e9) if config.cpus else None

    container_id: str | None = None
    try:
        container = client.containers.run(
            image=image,
            entrypoint=["/clawtune/entrypoint.sh"],
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
        try:
            container_id = container.id
            _log(f"[{task_id}] container {container_id[:12]} started")
            log_thread: threading.Thread | None = None
            try:
                log_thread = _stream_sdk_container_logs(container, task_id)
                try:
                    result = container.wait(
                        timeout=timeout_seconds if timeout_seconds > 0 else None
                    )
                    exit_code = result.get("StatusCode", -1)
                    error = None
                except Exception as exc:
                    _log(f"[{task_id}] wait error: {exc}")
                    try:
                        container.kill()
                    except Exception:
                        pass
                    exit_code = 124
                    error = f"container_timeout_or_wait_failed: {exc}"
            finally:
                if log_thread is not None:
                    _join_log_thread(log_thread)
                _write_sdk_container_log(container, trace_dir)
        finally:
            try:
                container.remove(force=True)
            except Exception as exc:
                raise ContainerCleanupError(
                    f"failed to remove task container {container_id or '<unknown>'}: {exc}"
                ) from exc

    except ContainerCleanupError:
        raise
    except Exception as exc:
        _log(f"[{task_id}] container failed: {exc}")
        duration = time.monotonic() - started
        return ContainerResult(
            task_id=task_id, image=image, exit_code=-1,
            error=str(exc), trace_dir=trace_dir,
            trace_files=_find_traces(trace_dir),
            duration_seconds=duration, container_id=container_id,
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
    cmd.extend(["--entrypoint", "/clawtune/entrypoint.sh"])

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

    container_id: str | None = None
    container_stopped = False
    log_process: Any | None = None
    log_thread: threading.Thread | None = None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        container_id = result.stdout.strip()
        if not container_id:
            raise ContainerCleanupError(
                "docker run succeeded without returning a container id"
            )
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
                exit_code = 124
                error = f"container timed out after {timeout_seconds}s"
            else:
                container_stopped = True
                exit_code = int(wait_result.stdout.strip())
                error = None
        else:
            wait_result = subprocess.run(wait_cmd, capture_output=True, text=True, check=True)
            container_stopped = True
            exit_code = int(wait_result.stdout.strip())
            error = None

    except subprocess.CalledProcessError as exc:
        _log(f"[{task_id}] CLI error: {exc}")
        if exc.stderr:
            _log(f"  stderr: {exc.stderr.strip()}")
        exit_code = -1
        error = str(exc)
        if exc.stderr and exc.stderr.strip():
            error = f"{error}: {exc.stderr.strip()}"
    finally:
        if container_id:
            # A failed wait or an interrupt must not leave the in-container
            # sidecar writing snapshots after the batch publishes this task's
            # KB.  Confirm exit before collecting logs and removing it.
            if not container_stopped:
                subprocess.run(
                    ["docker", "kill", container_id],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                subprocess.run(
                    ["docker", "wait", container_id],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            _stop_cli_log_stream(log_process, log_thread)
            try:
                _write_cli_container_log(container_id, trace_dir)
            finally:
                remove_result = subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if remove_result.returncode != 0:
                    detail = (remove_result.stderr or remove_result.stdout or "").strip()
                    raise ContainerCleanupError(
                        f"failed to remove task container {container_id}"
                        + (f": {detail}" if detail else "")
                    )

    duration = time.monotonic() - started
    trace_files = _find_traces(trace_dir)
    return ContainerResult(
        task_id=task_id, image=image, exit_code=exit_code,
        error=error, trace_dir=trace_dir, trace_files=trace_files,
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
