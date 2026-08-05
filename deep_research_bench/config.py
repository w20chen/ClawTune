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

from dataclasses import dataclass
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
