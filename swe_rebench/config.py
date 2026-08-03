"""
SWE-Rebench integration configuration.

Loads YAML config and applies environment-variable overrides so that
secrets (API keys) never need to be stored in the config file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_subst(value: str) -> str:
    """Replace ``${VAR}`` or ``$VAR`` patterns with environment values."""
    pattern = re.compile(r"\$\{(\w+)\}|\$(\w+)")
    def _repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return os.environ.get(name, "")
    return pattern.sub(_repl, value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_runtime_mode(value: str) -> str:
    # "host-openclaw-container" describes the same topology in user-facing
    # terms: OpenClaw/sidecar on the host, tool execution in its Docker
    # sandbox container.  Keep one canonical internal name.
    if value == "host-openclaw-container":
        return "host-openclaw-sandbox"
    return value


@dataclass
class RuntimeConfig:
    mode: str = "host-openclaw-sandbox"
    stage2_required: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuntimeConfig":
        mode = normalize_runtime_mode(str(d.get("mode", "host-openclaw-sandbox")))
        if mode not in {"container-openclaw", "host-openclaw-sandbox"}:
            raise ValueError(
                "runtime.mode must be 'container-openclaw', "
                "'host-openclaw-sandbox', or its alias "
                "'host-openclaw-container'"
            )
        return cls(
            mode=mode,
            stage2_required=_as_bool(
                d.get("ebpf_required", d.get("stage2_required", True))
            ),
        )


@dataclass
class LLMConfig:
    api_key: str = ""
    api_key_file: Path | None = None
    upstream_base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    openclaw_model_ref: str = "vllm/deepseek-v4-flash"

    @classmethod
    def from_dict(cls, d: dict[str, Any], repo_root: Path) -> "LLMConfig":
        api_key_file = _resolve_api_key_file(d.get("api_key_file"), repo_root)
        return cls(
            api_key=_resolve_api_key(d, repo_root, api_key_file),
            api_key_file=api_key_file,
            upstream_base_url=_env_subst(str(d.get("upstream_base_url", "https://api.deepseek.com"))),
            model=str(d.get("model", "deepseek-v4-flash")),
            openclaw_model_ref=str(d.get("openclaw_model_ref", "vllm/deepseek-v4-flash")),
        )


@dataclass
class DockerConfig:
    host: str = "unix:///var/run/docker.sock"
    platform: str = ""
    memory_limit: str = "8g"
    cpus: int = 4
    network_mode: str = "bridge"
    dns_servers: list[str] = field(default_factory=list)
    pull_policy: str = "missing"
    cap_add: list[str] = field(default_factory=list)
    privileged: bool = False
    cgroupns_mode: str = ""
    cgroup_mount_rw: bool = False
    cgroup_required: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DockerConfig":
        return cls(
            host=str(d.get("host", "unix:///var/run/docker.sock")),
            platform=str(os.getenv("SWE_REBENCH_DOCKER_PLATFORM") or d.get("platform", "")),
            memory_limit=str(d.get("memory_limit", "8g")),
            cpus=int(d.get("cpus", 4)),
            network_mode=str(d.get("network_mode", "bridge")),
            dns_servers=list(d.get("dns_servers", [])),
            pull_policy=str(d.get("pull_policy", "missing")),
            cap_add=list(d.get("cap_add", [])),
            privileged=_as_bool(d.get("privileged", False)),
            cgroupns_mode=str(d.get("cgroupns_mode", "")),
            cgroup_mount_rw=_as_bool(d.get("cgroup_mount_rw", False)),
            cgroup_required=_as_bool(d.get("cgroup_required", False)),
        )


@dataclass
class BatchConfig:
    task_timeout_seconds: int = 1800
    agent_timeout_seconds: int = 0
    parallelism: int = 1
    retry_failed: int = 0
    continue_on_error: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BatchConfig":
        parallelism = int(d.get("parallelism", 1))
        if parallelism < 1:
            parallelism = 1
        return cls(
            task_timeout_seconds=int(d.get("task_timeout_seconds", 1800)),
            agent_timeout_seconds=int(d.get("agent_timeout_seconds", 0)),
            parallelism=parallelism,
            retry_failed=int(d.get("retry_failed", 0)),
            continue_on_error=bool(d.get("continue_on_error", True)),
        )


@dataclass
class OutputConfig:
    trace_root: Path = Path("swe_rebench/traces")
    report_path: Path = Path("swe_rebench/report.json")
    flat_export_dir: Path | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any], repo_root: Path) -> "OutputConfig":
        flat_raw = d.get("flat_export_dir", "")
        flat = Path(flat_raw) if flat_raw else None
        if flat is not None and not flat.is_absolute():
            flat = repo_root / flat
        trace_root = Path(str(d.get("trace_root", "swe_rebench/traces")))
        if not trace_root.is_absolute():
            trace_root = repo_root / trace_root
        report_path = Path(str(d.get("report_path", "swe_rebench/report.json")))
        if not report_path.is_absolute():
            report_path = repo_root / report_path
        return cls(trace_root=trace_root, report_path=report_path, flat_export_dir=flat)


@dataclass
class BundleConfig:
    plugin_source: str = "packages/openclaw-plugin"
    scheduler_source: str = "services/scheduler"
    output_dir: str = "swe_rebench/.runtime/bundle"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BundleConfig":
        return cls(
            plugin_source=str(d.get("plugin_source", "packages/openclaw-plugin")),
            scheduler_source=str(d.get("scheduler_source", "services/scheduler")),
            output_dir=str(d.get("output_dir", "swe_rebench/.runtime/bundle")),
        )


@dataclass
class AgentConfig:
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        return cls(
            extra_args=list(d.get("extra_args", [])),
        )


@dataclass
class RunnerConfig:
    runtime: RuntimeConfig
    llm: LLMConfig
    docker: DockerConfig
    batch: BatchConfig
    output: OutputConfig
    bundle: BundleConfig
    agent: AgentConfig
    repo_root: Path
    config_path: Path | None = None

    @classmethod
    def from_yaml(cls, path: str | Path, repo_root: Path | None = None) -> "RunnerConfig":
        """Load configuration from a YAML file.

        Falls back gracefully if PyYAML is not installed by trying a
        basic key-value parser first.  For full YAML support install
        ``pyyaml``.
        """
        path = Path(path)
        if repo_root is None:
            repo_root = path.parent.resolve()
        raw = _load_yaml_safe(path)
        return cls(
            runtime=RuntimeConfig.from_dict(raw.get("runtime", {})),
            llm=LLMConfig.from_dict(raw.get("llm", {}), repo_root),
            docker=DockerConfig.from_dict(raw.get("docker", {})),
            batch=BatchConfig.from_dict(raw.get("batch", {})),
            output=OutputConfig.from_dict(raw.get("output", {}), repo_root),
            bundle=BundleConfig.from_dict(raw.get("bundle", {})),
            agent=AgentConfig.from_dict(raw.get("agent", {})),
            repo_root=repo_root,
            config_path=path.resolve(),
        )


def _load_yaml_safe(path: Path) -> dict[str, Any]:
    """Load YAML with PyYAML if available, else a minimal parser."""
    try:
        import yaml  # type: ignore[import-untyped]
        with open(path, encoding="utf-8") as fh:
            result = yaml.safe_load(fh)
        return result if isinstance(result, dict) else {}
    except ImportError:
        pass
    # Minimal fallback ── only handles the flat subset used by our config.
    result: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None
    current_key = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith(":"):
            current_key = stripped[:-1].strip()
            current_section = {}
            result[current_key] = current_section
            continue
        if not raw.startswith((" ", "\t")):
            current_section = None
        if current_section is not None and ":" in stripped:
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            current_section[k] = v
    return result


def _resolve_api_key_file(value: Any, repo_root: Path) -> Path | None:
    raw = os.getenv("LLM_API_KEY_FILE") or str(value or "swe_rebench/llm_api_key.txt")
    if not raw:
        return None
    path = Path(_env_subst(raw)).expanduser()
    return path if path.is_absolute() else repo_root / path


def _resolve_api_key(d: dict[str, Any], repo_root: Path, api_key_file: Path | None) -> str:
    configured = _env_subst(str(d.get("api_key", ""))).strip()
    if configured:
        return configured
    from_file = _read_api_key_file(api_key_file)
    if from_file:
        return from_file
    return _read_dotenv_api_key(repo_root / ".env")


def _read_api_key_file(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            return value
    return ""


def _read_dotenv_api_key(path: Path) -> str:
    if not path.exists():
        return ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if sep == "=" and key.strip() == "LLM_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""
