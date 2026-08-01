from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_LLM_UPSTREAM_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class SchedulerConfig:
    policy: str = "observe-only"
    max_global_concurrency: int = 4
    lease_ttl_ms: int = 300_000
    admission_wait_ms: int = 5_000
    tool_resource_trace_paths: tuple[Path, ...] = ()
    tool_resource_stage2_trace_paths: tuple[Path, ...] = ()
    tool_resource_latency_buckets_ms: tuple[float, ...] = (100.0, 500.0, 2_000.0, 10_000.0)
    tool_resource_repo: str = "openclaw"
    tool_resource_artifact_dir: Path | None = None
    tool_resource_container_executable: str = "docker"
    tool_resource_stage2_required: bool = True
    auth_token: str | None = None
    trace_dir: Path = Path("traces")
    trace_max_messages_bytes: int = 131_072  # 128 KiB, matches plugin default
    resource_poll_interval_ms: int = 50
    resource_timeline_max_points: int = 2_000
    sandbox_cgroup_path: str | None = None
    execution_cgroup_root: str | None = None
    sandbox_container_id: str | None = None
    sandbox_root_pid: int | None = None
    docker_exec_observer_enabled: bool = False
    docker_exec_container_prefix: str | None = None
    docker_socket: str = "/var/run/docker.sock"
    llm_proxy_enabled: bool = True
    llm_proxy_upstream_base_url: str | None = None
    llm_proxy_upstream_api_key: str | None = None
    llm_proxy_debug_dump: bool = False
    # Model name spoofing: expose a different model ID to OpenClaw than the
    # real upstream model.  Useful when OpenClaw's provider (vllm, openai)
    # validates model names against its own registry and rejects upstream
    # model IDs it does not recognise.
    #   expose_model  — model ID returned by sidecar /v1/models (what OpenClaw sees)
    #   upstream_model — real model ID sent to the upstream LLM API
    # If expose_model is set, /v1/models returns a synthetic list instead of
    # proxying; if upstream_model is unset, it defaults to expose_model.
    llm_proxy_expose_model: str | None = None
    llm_proxy_upstream_model: str | None = None

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        env_base = load_env_file()
        trace = os.getenv("AGENT_SCHEDULER_TRACE_DIR")
        tool_resource_traces = os.getenv("AGENT_SCHEDULER_TOOL_RESOURCE_TRACES")
        tool_resource_stage2_traces = os.getenv(
            "AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_TRACES",
            os.getenv("AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_TRACES"),
        )
        tool_resource_artifact_dir = os.getenv("AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR")
        return cls(
            policy=os.getenv("AGENT_SCHEDULER_POLICY", "observe-only"),
            max_global_concurrency=int(os.getenv("AGENT_SCHEDULER_MAX_GLOBAL_CONCURRENCY", "4")),
            lease_ttl_ms=int(os.getenv("AGENT_SCHEDULER_LEASE_TTL_MS", "300000")),
            admission_wait_ms=int(os.getenv("AGENT_SCHEDULER_ADMISSION_WAIT_MS", "5000")),
            tool_resource_trace_paths=tuple(
                _resolve_path(item, env_base)
                for item in _split_env_paths(tool_resource_traces)
            ),
            tool_resource_stage2_trace_paths=tuple(
                _resolve_path(item, env_base)
                for item in _split_env_paths(tool_resource_stage2_traces)
            ),
            tool_resource_latency_buckets_ms=tuple(
                _parse_float_list(
                    os.getenv("AGENT_SCHEDULER_TOOL_RESOURCE_LATENCY_BUCKETS_MS"),
                    default=(100.0, 500.0, 2_000.0, 10_000.0),
                )
            ),
            tool_resource_repo=os.getenv("AGENT_SCHEDULER_TOOL_RESOURCE_REPO", "openclaw"),
            tool_resource_artifact_dir=(
                _resolve_path(tool_resource_artifact_dir, env_base)
                if tool_resource_artifact_dir
                else None
            ),
            tool_resource_container_executable=os.getenv(
                "AGENT_SCHEDULER_TOOL_RESOURCE_CONTAINER_EXECUTABLE",
                "docker",
            ),
            tool_resource_stage2_required=os.getenv(
                "AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_REQUIRED",
                os.getenv("AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED", "true"),
            ).lower()
            in {"1", "true", "yes", "on"},
            auth_token=os.getenv("AGENT_SCHEDULER_TOKEN"),
            trace_dir=_resolve_path(trace, env_base) if trace else Path("traces"),
            trace_max_messages_bytes=int(os.getenv("AGENT_SCHEDULER_TRACE_MAX_MESSAGES_BYTES", "131072")),
            resource_poll_interval_ms=int(os.getenv("AGENT_SCHEDULER_RESOURCE_POLL_INTERVAL_MS", "50")),
            resource_timeline_max_points=int(os.getenv("AGENT_SCHEDULER_RESOURCE_TIMELINE_MAX_POINTS", "2000")),
            sandbox_cgroup_path=os.getenv("AGENT_SCHEDULER_SANDBOX_CGROUP_PATH"),
            execution_cgroup_root=os.getenv("AGENT_SCHEDULER_EXECUTION_CGROUP_ROOT"),
            sandbox_container_id=os.getenv("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID"),
            sandbox_root_pid=_optional_int(os.getenv("AGENT_SCHEDULER_SANDBOX_ROOT_PID")),
            docker_exec_observer_enabled=os.getenv("AGENT_SCHEDULER_DOCKER_EXEC_OBSERVER", "false").lower()
            in {"1", "true", "yes", "on"},
            docker_exec_container_prefix=os.getenv("AGENT_SCHEDULER_DOCKER_EXEC_CONTAINER_PREFIX"),
            docker_socket=os.getenv("AGENT_SCHEDULER_DOCKER_SOCKET", "/var/run/docker.sock"),
            llm_proxy_enabled=True,
            llm_proxy_upstream_base_url=os.getenv(
                "AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL",
                DEFAULT_LLM_UPSTREAM_BASE_URL,
            ),
            llm_proxy_upstream_api_key=(
                os.getenv("AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE")
                or os.getenv("AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY")
            ),
            llm_proxy_debug_dump=os.getenv("AGENT_SCHEDULER_LLM_PROXY_DEBUG_DUMP", "false").lower()
            in {"1", "true", "yes", "on"},
            llm_proxy_expose_model=os.getenv("AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL"),
            llm_proxy_upstream_model=os.getenv("AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL"),
        )


def load_env_file() -> Path:
    selected = os.getenv("AGENT_SCHEDULER_ENV_FILE")
    candidates = [Path(selected)] if selected else list(_default_env_candidates())
    for candidate in candidates:
        path = candidate.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists() or not path.is_file():
            continue
        _load_dotenv(path)
        return path.parent
    return Path.cwd()


def _default_env_candidates() -> Iterable[Path]:
    cwd = Path.cwd()
    root = _repo_root()
    yield cwd / ".env"
    yield cwd / ".env.openclaw-recorder"
    yield root / ".env"
    yield root / ".env.openclaw-recorder"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _load_dotenv(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if sep != "=":
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _unquote_env_value(value.strip())


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _path_from_env(name: str, default: str, base: Path) -> Path:
    return _resolve_path(os.getenv(name, default), base)


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _split_env_paths(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    parts: list[str] = []
    for chunk in value.split(os.pathsep):
        parts.extend(item.strip() for item in chunk.split(",") if item.strip())
    return parts


def _parse_float_list(value: str | None, *, default: tuple[float, ...]) -> list[float]:
    if value is None or not value.strip():
        return list(default)
    parsed: list[float] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            parsed.append(float(item))
        except ValueError:
            continue
    return parsed or list(default)
