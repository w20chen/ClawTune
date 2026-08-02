"""Data schemas and dataclasses for cmdtime predictor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List


@dataclass(frozen=True)
class Observation:
    """A single historical command observation.

    For clause-level data (fine-grained trace with ``clause_telemetry``),
    each clause in a compound command (``&&``, ``||``, ``;``, ``|``) is
    stored as a separate observation with its own ``duration_s`` and
    ``clause_index``.

    Attributes:
        cmd: The shell command string (full command or single clause).
        duration_s: Execution time in seconds.
        repo: Repository identifier (e.g., ``owner/repo``).
        cwd: Working directory.
        env_id: Environment identifier.
        timestamp: ISO timestamp string.
        exit_code: Process exit code.
        tool_name: Tool name (e.g., ``"exec"``, ``"read_file"``).
        clause_index: For clause-level data, the 0-based index of this
            clause within the original compound command.  ``None`` for
            observations not derived from clause telemetry.
    """

    cmd: str
    duration_s: float
    repo: str | None = None
    cwd: str | None = None
    env_id: str | None = None
    timestamp: str | None = None
    exit_code: int | None = None
    tool_name: str | None = None
    clause_index: int | None = None


@dataclass
class NodeStats:
    """Aggregated statistics for one context node."""

    features: FrozenSet[str]
    durations: List[float] = field(default_factory=list)
    mean_s: float = 0.0
    median_s: float = 0.0
    geometric_mean_s: float = 0.0
    count: int = 0
    loo_mse_log: float = 0.0
    stderr_log: float = 0.0
    std_log: float = 0.0
    mean_log: float = 0.0
    last_updated: str = ""
    signature_count: int = 0
    signature_log_mean_var: float = 0.0
    cold_start: bool = False
    shrinkage_var: float = 0.0
    loso_risk: float = 0.0


@dataclass
class CandidateNode:
    """A node activated for a query, with computed risk."""

    stats: NodeStats
    risk: float
    exact: bool
    error_term: float
    uncertainty_term: float
    drift_term: float


@dataclass
class DominanceEdge:
    """A dominance relation: general_node -> specific_node."""

    general_features: FrozenSet[str]
    specific_features: FrozenSet[str]


@dataclass
class PredictionResult:
    """Result of predicting a single command's execution time."""

    cmd: str
    prediction_s: float
    exact_match: bool
    query_features: List[str]
    selected_features: List[str]
    selected_sample_count: int
    selected_risk: float
    activated_count: int
    frontier_count: int
    activated_nodes: List[CandidateNode]
    frontier_nodes: List[CandidateNode]
    dominance_edges: List[DominanceEdge]
    fallback: str = ""  # "" = normal, "tool" = tool fallback, "global" = global fallback
    estimator: str = "median"  # which estimator was used for the prediction


@dataclass
class ClauseSegment:
    """One clause in a compound command, with the separator that follows it.

    Example: for ``"grep x | head -5 && wc -l"``:

        ClauseSegment(cmd="grep x", separator="|")
        ClauseSegment(cmd="head -5", separator="&&")
        ClauseSegment(cmd="wc -l", separator="")

    The last clause always has an empty separator.
    """

    cmd: str
    separator: str  # "|", "&&", "||", ";", or "" (last clause)
    clause_index: int = 0

