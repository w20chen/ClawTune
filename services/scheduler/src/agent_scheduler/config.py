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
    tool_profiles_path: Path | None = None
    auth_token: str | None = None
    trace_dir: Path = Path("traces")
    trace_max_messages_bytes: int = 131_072  # 128 KiB, matches plugin default
    resource_poll_interval_ms: int = 50
    resource_timeline_max_points: int = 2_000
    sandbox_cgroup_path: str | None = None
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
        profile = os.getenv("AGENT_SCHEDULER_TOOL_PROFILES")
        trace = os.getenv("AGENT_SCHEDULER_TRACE_DIR")
        return cls(
            policy=os.getenv("AGENT_SCHEDULER_POLICY", "observe-only"),
            max_global_concurrency=int(os.getenv("AGENT_SCHEDULER_MAX_GLOBAL_CONCURRENCY", "4")),
            lease_ttl_ms=int(os.getenv("AGENT_SCHEDULER_LEASE_TTL_MS", "300000")),
            admission_wait_ms=int(os.getenv("AGENT_SCHEDULER_ADMISSION_WAIT_MS", "5000")),
            tool_profiles_path=_resolve_path(profile, env_base) if profile else None,
            auth_token=os.getenv("AGENT_SCHEDULER_TOKEN"),
            trace_dir=_resolve_path(trace, env_base) if trace else Path("traces"),
            trace_max_messages_bytes=int(os.getenv("AGENT_SCHEDULER_TRACE_MAX_MESSAGES_BYTES", "131072")),
            resource_poll_interval_ms=int(os.getenv("AGENT_SCHEDULER_RESOURCE_POLL_INTERVAL_MS", "50")),
            resource_timeline_max_points=int(os.getenv("AGENT_SCHEDULER_RESOURCE_TIMELINE_MAX_POINTS", "2000")),
            sandbox_cgroup_path=os.getenv("AGENT_SCHEDULER_SANDBOX_CGROUP_PATH"),
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
