"""Public, shell-independent inputs and outputs for the edge kappa KB."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureQuery:
    features: frozenset[str]
    core_features: frozenset[str]
    tool: str
    normalization_version: str = "shell-normalize-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", frozenset(self.features))
        object.__setattr__(self, "core_features", frozenset(self.core_features))
        if any(not isinstance(feature, str) or not feature for feature in self.features):
            raise ValueError("features must be nonempty strings")
        if not self.tool or {f for f in self.features if f.startswith("tool=")} != {f"tool={self.tool}"}:
            raise ValueError("features must contain exactly one query tool identity")
        if not self.core_features <= self.features:
            raise ValueError("core features must be a subset of features")
        if f"tool={self.tool}" not in self.core_features:
            raise ValueError("tool identity must be a core feature")


@dataclass(frozen=True)
class TimeOutcome:
    event_id: str
    start_time: float
    end_time: float
    task_id: str
    call_id: str
    clause_id: str
    duration_ms: float | None = None
    censor_lower_ms: float | None = None
    trusted: bool = True
    label_source: str = "clause"


@dataclass(frozen=True)
class TrainingEvent:
    query: FeatureQuery
    outcome: TimeOutcome
    source_command: str | None = None


@dataclass(frozen=True)
class ParentEvidence:
    parent_id: str
    parent_features: tuple[str, ...]
    dropped_features: tuple[str, ...]
    difference_count: int
    kappa: float
    mixture_share: float
    distribution: tuple[float, ...]


@dataclass(frozen=True)
class BucketPrediction:
    probabilities: tuple[float, ...] | None
    bucket: int | None
    node_id: str
    exact_match: bool
    observation_count: int
    evidence: tuple[ParentEvidence, ...]
    evidence_union_count: int
    evidence_overlap_ratio: float
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class UpdateReport:
    committed: int = 0
    weight_updates: int = 0
    weight_update_skipped: int = 0
    skipped_nodes: int = 0
    boundary_hits: int = 0
