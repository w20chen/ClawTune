"""
Deep Research Bench integration configuration.

Loads YAML config and applies environment-variable overrides, reusing the
proven dataclasses from ``swe_rebench.config`` (LLM/API-key resolution,
runtime mode, Docker, batch, output, bundle, agent) and adding the
research-specific ``sandbox`` (a very basic tool container) and ``dataset``
sections.

Unlike SWE-Rebench there is no per-task Docker image and no Stage-2 eBPF
clause telemetry: research tools (read/edit/web) are measured with the
sandbox-container / per-PID scope.  ``runtime.stage2_required`` therefore
defaults to ``false``; the separate ``runtime.gate_required`` flag (default
``true``) enables the relaxed LLM + tool-span telemetry gate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swe_rebench.config import (
    AgentConfig,
    BatchConfig,
    BundleConfig,
    DockerConfig,
    LLMConfig,
    OutputConfig,
    RuntimeConfig,
    RunnerConfig,
    _as_bool,
    _env_subst,
    _load_yaml_safe,
)


@dataclass
class DatasetConfig:
    """DeepResearchBench dataset + task-field configuration.

    Mirrors agent-test-bench ``configs/benchmarks/deep-research-bench.yaml``:
    the harness is ``muset-ai/DeepResearch-Bench-Dataset`` (split ``test``)
    with one ``generated_reports/openai-deepresearch.jsonl`` data file whose
    rows carry ``id`` / ``prompt`` / ``article``.
    """

    harness_dataset: str = "muset-ai/DeepResearch-Bench-Dataset"
    harness_split: str = "test"
    data_files: str = "generated_reports/openai-deepresearch.jsonl"
    id_field: str = "id"
    question_field: str = "prompt"
    answer_field: str = "article"
    topic_field: str = ""
    difficulty_field: str = ""
    domain_field: str = ""
    reference_kind: str = "generated_report"
    selection_n: int = 32
    selection_seed: int = 42
    prompt_template: str = "default"
    default_max_iterations: int = 100

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DatasetConfig":
        return cls(
            harness_dataset=str(
                d.get("harness_dataset", "muset-ai/DeepResearch-Bench-Dataset")
            ),
            harness_split=str(d.get("harness_split", "test")),
            data_files=str(
                d.get("data_files", "generated_reports/openai-deepresearch.jsonl")
            ),
            id_field=str(d.get("id_field", "id")),
            question_field=str(d.get("question_field", "prompt")),
            answer_field=str(d.get("answer_field", "article")),
            topic_field=str(d.get("topic_field", "")),
            difficulty_field=str(d.get("difficulty_field", "")),
            domain_field=str(d.get("domain_field", "")),
            reference_kind=str(d.get("reference_kind", "generated_report")),
            selection_n=int(d.get("selection_n", 32)),
            selection_seed=int(d.get("selection_seed", 42)),
            prompt_template=str(d.get("prompt_template", "default")),
            default_max_iterations=int(d.get("default_max_iterations", 100)),
        )


@dataclass
class SandboxConfig:
    """The very basic Docker image used as the OpenClaw tool sandbox."""

    image: str = "python:3.11-slim"
    workdir: str = "/workspace"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SandboxConfig":
        return cls(
            image=str(d.get("image", "python:3.11-slim")),
            workdir=str(d.get("workdir", "/workspace")),
        )


@dataclass
class WebSearchConfig:
    """Web-search (Tavily) configuration for OpenClaw's built-in ``web_search``.

    ``web_search`` runs in the agent runtime on the host, not inside the
    sandbox, so the provider key must be present in the ``openclaw agent``
    process environment.  Tavily is the default provider and reads the
    ``TAVILY_API_KEY`` environment variable; ``provider`` is pinned into the
    run-scoped OpenClaw config so web search is deterministic even when other
    API-backed provider keys (e.g. Brave) are configured on the host.
    """

    enabled: bool = True
    provider: str = "tavily"
    api_key: str = ""
    api_key_file: Path | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any], repo_root: Path) -> "WebSearchConfig":
        api_key_file = _resolve_web_search_key_file(d.get("api_key_file"), repo_root)
        return cls(
            enabled=_as_bool(d.get("enabled", True)),
            provider=str(d.get("provider", "tavily")).strip() or "tavily",
            api_key=_resolve_web_search_api_key(d, repo_root, api_key_file),
            api_key_file=api_key_file,
        )


def _resolve_web_search_key_file(value: Any, repo_root: Path) -> Path | None:
    raw = os.getenv("TAVILY_API_KEY_FILE") or str(
        value or "deep_research_bench/tavily_api_key.txt"
    )
    if not raw:
        return None
    path = Path(_env_subst(raw)).expanduser()
    return path if path.is_absolute() else repo_root / path


def _resolve_web_search_api_key(
    d: dict[str, Any],
    repo_root: Path,
    api_key_file: Path | None,
) -> str:
    """Resolve the web-search key: TAVILY_API_KEY env, api_key / ${TAVILY_API_KEY},
    then the key file, then a ``TAVILY_API_KEY=`` line in the root ``.env``."""
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        return env_key
    configured = _env_subst(str(d.get("api_key", ""))).strip()
    if configured:
        return configured
    if api_key_file is not None and api_key_file.exists():
        for line in api_key_file.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                return value
    env_path = repo_root / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, sep, value = line.partition("=")
            if sep == "=" and key.strip() == "TAVILY_API_KEY":
                return value.strip().strip('"').strip("'")
    return ""


@dataclass
class DRBConfig:
    runtime: RuntimeConfig
    llm: LLMConfig
    docker: DockerConfig
    sandbox: SandboxConfig
    batch: BatchConfig
    output: OutputConfig
    bundle: BundleConfig
    agent: AgentConfig
    dataset: DatasetConfig
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    gate_required: bool = True
    repo_root: Path = Path(".")
    config_path: Path | None = None

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        repo_root: Path | None = None,
    ) -> "DRBConfig":
        """Load configuration from a YAML file (PyYAML with a flat fallback)."""
        path = Path(path)
        if repo_root is None:
            # deep_research_bench/config.yaml -> repository root.
            repo_root = path.resolve().parent.parent
        raw = _load_yaml_safe(path)
        runtime_raw = raw.get("runtime", {})
        runtime = RuntimeConfig.from_dict(runtime_raw)
        # Research tasks never produce Stage-2 exec clause telemetry; the
        # sidecar must not demand it unless the operator explicitly opts in.
        if "stage2_required" not in runtime_raw and "ebpf_required" not in runtime_raw:
            runtime.stage2_required = False
        gate_raw = runtime_raw.get("gate_required")
        gate_required = _as_bool(gate_raw) if gate_raw is not None else True
        return cls(
            runtime=runtime,
            llm=LLMConfig.from_dict(raw.get("llm", {}), repo_root),
            docker=DockerConfig.from_dict(raw.get("docker", {})),
            sandbox=SandboxConfig.from_dict(raw.get("sandbox", {})),
            batch=BatchConfig.from_dict(raw.get("batch", {})),
            output=OutputConfig.from_dict(raw.get("output", {}), repo_root),
            bundle=BundleConfig.from_dict(raw.get("bundle", {})),
            agent=AgentConfig.from_dict(raw.get("agent", {})),
            dataset=DatasetConfig.from_dict(raw.get("dataset", {})),
            web_search=WebSearchConfig.from_dict(raw.get("web_search", {}), repo_root),
            gate_required=gate_required,
            repo_root=repo_root,
            config_path=path.resolve(),
        )

    def to_swe_runner_config(self) -> RunnerConfig:
        """Build the swe-rebench RunnerConfig consumed by host_sandbox helpers."""
        return RunnerConfig(
            runtime=self.runtime,
            llm=self.llm,
            docker=self.docker,
            batch=self.batch,
            output=self.output,
            bundle=self.bundle,
            agent=self.agent,
            repo_root=self.repo_root,
            config_path=self.config_path,
        )
