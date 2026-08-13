"""Clause-level execution-time prediction backed by one mixed lattice KB.

The algorithm implementation is vendored from the ``latt`` project under
``tool_time._lattice_vendor``.  This module is deliberately a thin adapter:
ClawTune owns clause telemetry and JSON snapshots, while the vendored code owns
feature normalization, node statistics, shrinkage, and LOSO selection.

Common and repository-specific knowledge are not separate stores.  ``repo`` is
an optional lattice feature, so each observation contributes both common nodes
without that feature and repo-specific nodes with it to the same node mapping.
"""

from __future__ import annotations

import heapq
import math
import shlex
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from tool_resource.features import shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import ClauseObservation
from tool_time._lattice_vendor.features import estimate_node_count
from tool_time._lattice_vendor.nodes import build_nodes
from tool_time._lattice_vendor.normalize import FeatureSet, normalize_command
from tool_time._lattice_vendor.schemas import NodeStats, Observation, PredictionResult
from tool_time._lattice_vendor.selector import predict as select_prediction
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances


LATTICE_TIME_ALGORITHMS = ("shrinkage", "loso", "max_cardinality")
LATTICE_TIME_KB_SCHEMA = "clause_lattice_time_kb_v1"

_NODE_MODE = "bounded"
_MAX_OPTIONAL_FEATURES = 6
_MIN_PARTIAL_SUPPORT = 1
_MAX_NODES_PER_SIGNATURE = 4_096
_NODE_OCCURRENCE_BUDGET = 20_000
_MAX_SHRINKAGE_CANDIDATES = 512
_SHRINKAGE_KAPPA = 5.0
_CONTEXT_SAMPLE_ALPHA = 0.03
_DOMINANCE_DELTA = 0.15
_SPECIFICITY_RISK_TOLERANCE = 0.5
_LOSO_RISK_WEIGHT = 1.0

_ObservationKey = tuple[str, str, tuple[str, ...], float, float, float]
_NodeState = tuple[dict[FeatureSet, NodeStats], float, float, float]


@dataclass(frozen=True)
class LatticeTimePrediction:
    """One algorithm's point prediction for one executable clause."""

    algorithm: str
    prediction_ms: float | None
    selected_features: tuple[str, ...]
    evidence_count: int
    selected_risk: float | None
    exact_match: bool | None
    fallback: str | None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.algorithm not in LATTICE_TIME_ALGORITHMS:
            raise ValueError(f"unsupported lattice time algorithm {self.algorithm!r}")
        if (self.prediction_ms is None) == (self.unavailable_reason is None):
            raise ValueError(
                "lattice prediction requires exactly one of prediction_ms or "
                "unavailable_reason"
            )
        if self.prediction_ms is not None and (
            not math.isfinite(self.prediction_ms) or self.prediction_ms < 0.0
        ):
            raise ValueError("prediction_ms must be finite and non-negative")
        if self.evidence_count < 0:
            raise ValueError("evidence_count must be non-negative")


@dataclass(frozen=True)
class ClauseLatticeTimePredictions:
    """All lattice point predictions for one eBPF-observable static clause."""

    clause_index: int
    bin: str
    argv: tuple[str, ...]
    predictions: tuple[LatticeTimePrediction, ...]


class LatticeTimeKB:
    """One unlayered clause lattice shared by all three point predictors.

    Historical observations supplied at startup are committed training data.
    Newly completed eBPF clauses are buffered and become visible only when
    ``ts_end <`` the next query's ``ts_start``, matching ClawTune's existing
    causal clause-KB rule.  Raw observations are retained in the snapshot so a
    rebuild can exactly recompute LOSO signature medians after online updates.
    """

    def __init__(self) -> None:
        self._observations: list[ClauseObservation] = []
        self._pending: list[tuple[float, int, ClauseObservation]] = []
        self._pending_seq = 0
        self._last_query_ts: float | None = None
        self._nodes: dict[FeatureSet, NodeStats] = {}
        self._global_log_var = 0.0
        self._global_log_std = 0.5
        self._global_median_s = 0.0
        self._dirty = False
        self._data_generation = 0
        self._prepared_all_generation = -1
        self._prepared_all_state: _NodeState | None = None

    @classmethod
    def fit(cls, observations: Iterable[ClauseObservation]) -> LatticeTimeKB:
        kb = cls()
        kb.merge_historical(observations)
        return kb

    @property
    def observation_count(self) -> int:
        return len(self._observations)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def node_count(self) -> int:
        self._ensure_nodes()
        return len(self._nodes)

    def merge_historical(self, observations: Iterable[ClauseObservation]) -> int:
        """Merge a replayable corpus while preserving true sample multiplicity.

        Snapshot observations form the baseline multiset.  A replayed trace
        corpus contributes only occurrences beyond that baseline, so repeated
        executions with identical legacy fallback timestamps remain distinct
        without double-counting snapshot-plus-trace startup input.
        """

        baseline = Counter(_observation_key(item) for item in self._observations)
        baseline.update(_observation_key(item[2]) for item in self._pending)
        seen: Counter[_ObservationKey] = Counter()
        added = 0
        for observation in observations:
            if not _eligible_observation(observation):
                continue
            key = _observation_key(observation)
            seen[key] += 1
            if seen[key] <= baseline[key]:
                continue
            self._observations.append(observation)
            added += 1
        if added:
            self._dirty = True
            self._data_generation += 1
            self._prepared_all_state = None
        return added

    def observe_completed_clause(self, observation: ClauseObservation) -> bool:
        """Buffer one valid eBPF clause for a strictly later query."""

        if not _eligible_observation(observation):
            return False
        heapq.heappush(
            self._pending,
            (observation.ts_end, self._pending_seq, observation),
        )
        self._pending_seq += 1
        self._data_generation += 1
        self._prepared_all_state = None
        return True

    def prepare(self) -> None:
        """Build nodes outside the latency-sensitive prediction path."""

        self._ensure_nodes()
        if self._pending:
            pending = [observation for _, _, observation in self._pending]
            self._prepared_all_state = _build_node_state(
                [*self._observations, *pending]
            )
            self._prepared_all_generation = self._data_generation

    def predict_clauses(
        self,
        repo: str,
        clauses: Sequence[Mapping[str, Any]],
        ts_start: float,
        *,
        parse_failed: bool = False,
        shell_command: bool = True,
    ) -> tuple[ClauseLatticeTimePredictions, ...]:
        """Predict every exec-producing clause independently; never compose them."""

        self._advance(ts_start)
        outcomes: list[ClauseLatticeTimePredictions] = []
        for clause_index, clause in enumerate(clauses):
            bin_ = str(clause["bin"])
            argv = tuple(str(value) for value in clause["argv"])
            if shell_command and not shell_bin_requires_exec_evidence(
                bin_, argv[0] if argv else None
            ):
                continue
            if not bin_ or not argv:
                continue
            predictions = (
                tuple(
                    _unavailable_prediction(algorithm, "parse_failed")
                    for algorithm in LATTICE_TIME_ALGORITHMS
                )
                if parse_failed
                else self._predict_clause(repo, argv)
            )
            outcomes.append(
                ClauseLatticeTimePredictions(
                    clause_index=clause_index,
                    bin=bin_,
                    argv=argv,
                    predictions=predictions,
                )
            )
        return tuple(outcomes)

    def _predict_clause(
        self,
        repo: str,
        argv: tuple[str, ...],
    ) -> tuple[LatticeTimePrediction, ...]:
        self._ensure_nodes()
        if not self._nodes:
            return tuple(
                _unavailable_prediction(algorithm, "no_lattice_time_evidence")
                for algorithm in LATTICE_TIME_ALGORITHMS
            )

        command = shlex.join(argv)
        query_features, _ = normalize_command(command, repo=repo)
        predictions: list[LatticeTimePrediction] = []
        for algorithm in LATTICE_TIME_ALGORITHMS:
            try:
                if algorithm == "max_cardinality":
                    prediction = self._predict_max_cardinality(query_features)
                elif (
                    algorithm == "shrinkage"
                    and query_features not in self._nodes
                    and _matching_node_count(self._nodes, query_features)
                    > _MAX_SHRINKAGE_CANDIDATES
                ):
                    prediction = _unavailable_prediction(
                        algorithm,
                        "lattice_candidate_limit_exceeded",
                    )
                else:
                    prediction = self._predict_vendored(command, repo, algorithm)
            except (KeyError, TypeError, ValueError) as exc:
                prediction = _unavailable_prediction(
                    algorithm,
                    f"lattice_prediction_error:{type(exc).__name__}",
                )
            predictions.append(prediction)
        return tuple(predictions)

    def _predict_vendored(
        self,
        command: str,
        repo: str,
        algorithm: str,
    ) -> LatticeTimePrediction:
        result = select_prediction(
            command,
            self._nodes,
            repo=repo,
            global_log_var=self._global_log_var,
            global_log_std=self._global_log_std,
            beta=0.0,
            gamma=0.0,
            delta=_DOMINANCE_DELTA,
            risk_method=algorithm,
            # Explicit (shrinkage): return the exact node's median when the
            # query's feature set exists in the lattice.  The vendored
            # selector already auto-enables this for ``shrinkage``/``loso``;
            # passing it here pins the intent against selector refactors.
            exact_match_shortcut=(algorithm == "shrinkage"),
            context_sample_alpha=_CONTEXT_SAMPLE_ALPHA,
            estimator="median",
            shrinkage_kappa=_SHRINKAGE_KAPPA,
            loso_min_signatures=2,
            specificity_risk_tolerance=_SPECIFICITY_RISK_TOLERANCE,
            risk_weight=_LOSO_RISK_WEIGHT,
        )
        return _prediction_from_result(algorithm, result)

    def _predict_max_cardinality(
        self,
        query_features: FeatureSet,
    ) -> LatticeTimePrediction:
        best: NodeStats | None = None
        best_count = -1
        for features, stats in self._nodes.items():
            if features.issubset(query_features) and len(features) > best_count:
                best_count = len(features)
                best = stats
        if best is None:
            return LatticeTimePrediction(
                algorithm="max_cardinality",
                prediction_ms=self._global_median_s * 1000.0,
                selected_features=(),
                evidence_count=len(self._observations),
                selected_risk=None,
                exact_match=False,
                fallback="global",
            )
        return LatticeTimePrediction(
            algorithm="max_cardinality",
            prediction_ms=best.median_s * 1000.0,
            selected_features=tuple(sorted(best.features)),
            evidence_count=best.count,
            selected_risk=None,
            exact_match=best.features == query_features,
            fallback=None,
        )

    def _advance(self, ts_start: float) -> None:
        if not math.isfinite(ts_start):
            raise ValueError("query ts_start must be finite")
        if self._last_query_ts is not None and ts_start < self._last_query_ts:
            raise ValueError(
                f"backdated lattice query at {ts_start} after {self._last_query_ts}"
            )
        self._last_query_ts = ts_start
        absorbed = False
        while self._pending and self._pending[0][0] < ts_start:
            _, _, observation = heapq.heappop(self._pending)
            self._observations.append(observation)
            absorbed = True
        if absorbed:
            if (
                not self._pending
                and self._prepared_all_state is not None
                and self._prepared_all_generation == self._data_generation
            ):
                self._install_node_state(self._prepared_all_state)
                self._prepared_all_state = None
            else:
                self._dirty = True

    def _ensure_nodes(self) -> None:
        if not self._dirty:
            return
        self._install_node_state(_build_node_state(self._observations))

    def _install_node_state(self, state: _NodeState) -> None:
        (
            self._nodes,
            self._global_log_var,
            self._global_log_std,
            self._global_median_s,
        ) = state
        self._dirty = False

    def to_json_obj(self) -> dict[str, Any]:
        """Return the unlayered observation log needed for exact future rebuilds."""

        return {
            "schema": LATTICE_TIME_KB_SCHEMA,
            "node_generation": {
                "mode": _NODE_MODE,
                "max_optional_features": _MAX_OPTIONAL_FEATURES,
                "min_partial_support": _MIN_PARTIAL_SUPPORT,
                "max_nodes_per_signature": _MAX_NODES_PER_SIGNATURE,
                "node_occurrence_budget": _NODE_OCCURRENCE_BUDGET,
                "max_shrinkage_candidates": _MAX_SHRINKAGE_CANDIDATES,
            },
            "observations": [
                asdict(observation)
                for observation in sorted(
                    self._observations, key=_observation_sort_key
                )
            ],
            "pending": [
                asdict(observation)
                for _, _, observation in sorted(self._pending)
            ],
            "last_query_ts": self._last_query_ts,
        }

    @classmethod
    def from_json_obj(cls, obj: Mapping[str, Any]) -> LatticeTimeKB:
        if obj.get("schema") != LATTICE_TIME_KB_SCHEMA:
            raise ValueError(f"unsupported lattice KB schema {obj.get('schema')!r}")
        expected_generation = {
            "mode": _NODE_MODE,
            "max_optional_features": _MAX_OPTIONAL_FEATURES,
            "min_partial_support": _MIN_PARTIAL_SUPPORT,
            "max_nodes_per_signature": _MAX_NODES_PER_SIGNATURE,
            "node_occurrence_budget": _NODE_OCCURRENCE_BUDGET,
            "max_shrinkage_candidates": _MAX_SHRINKAGE_CANDIDATES,
        }
        if obj.get("node_generation") != expected_generation:
            raise ValueError("lattice KB node-generation configuration differs")
        observation_rows = obj.get("observations")
        if not isinstance(observation_rows, list):
            raise ValueError("lattice KB observations must be an array")
        pending_rows = obj.get("pending")
        if not isinstance(pending_rows, list):
            raise ValueError("lattice KB pending must be an array")
        kb = cls()
        kb.merge_historical(
            _observation_from_json(row) for row in observation_rows
        )
        for row in pending_rows:
            kb.observe_completed_clause(_observation_from_json(row))
        last_query_ts = obj.get("last_query_ts")
        if last_query_ts is not None:
            if (
                not isinstance(last_query_ts, (int, float))
                or isinstance(last_query_ts, bool)
                or not math.isfinite(last_query_ts)
            ):
                raise ValueError("lattice KB last_query_ts must be finite or null")
            kb._last_query_ts = float(last_query_ts)
        return kb


def _build_node_state(
    observations: Sequence[ClauseObservation],
) -> _NodeState:
    ordered = sorted(observations, key=_observation_sort_key)
    training = [
        Observation(
            cmd=shlex.join(observation.argv),
            duration_s=float(observation.latency_ms) / 1000.0,
            repo=observation.repo,
            clause_index=0,
        )
        for observation in ordered
        if observation.latency_ms is not None
    ]
    if not training:
        return {}, 0.0, 0.5, 0.0
    effective_max_optional_features = _effective_max_optional_features(training)
    nodes, global_log_var, global_log_std = build_nodes(
        training,
        mode=_NODE_MODE,
        max_optional_features=effective_max_optional_features,
        always_keep_exact=True,
        min_partial_support=_MIN_PARTIAL_SUPPORT,
        estimator="median",
        split_compounds=False,
    )
    compute_shrinkage_variances(
        nodes,
        kappa=_SHRINKAGE_KAPPA,
        global_log_var=global_log_var,
    )
    global_median_s = statistics.median(
        observation.duration_s for observation in training
    )
    return nodes, global_log_var, global_log_std, global_median_s


def _effective_max_optional_features(
    observations: Sequence[Observation],
) -> int:
    optional_counts: list[int] = []
    for observation in observations:
        features, core = normalize_command(
            observation.cmd,
            repo=observation.repo,
            cwd=observation.cwd,
            env_id=observation.env_id,
        )
        optional_counts.append(len(features - core))
    for maximum in range(_MAX_OPTIONAL_FEATURES, -1, -1):
        estimates = [
            estimate_node_count(count, mode=_NODE_MODE, max_optional_features=maximum)
            for count in optional_counts
        ]
        if (
            max(estimates, default=0) <= _MAX_NODES_PER_SIGNATURE
            and sum(estimates) <= _NODE_OCCURRENCE_BUDGET
        ):
            return maximum
    return 0


def _matching_node_count(
    nodes: Mapping[FeatureSet, NodeStats],
    query_features: FeatureSet,
) -> int:
    count = 0
    for features in nodes:
        if features.issubset(query_features):
            count += 1
            if count > _MAX_SHRINKAGE_CANDIDATES:
                break
    return count


def _prediction_from_result(
    algorithm: str,
    result: PredictionResult,
) -> LatticeTimePrediction:
    return LatticeTimePrediction(
        algorithm=algorithm,
        prediction_ms=result.prediction_s * 1000.0,
        selected_features=tuple(result.selected_features),
        evidence_count=result.selected_sample_count,
        selected_risk=result.selected_risk,
        exact_match=result.exact_match,
        fallback=result.fallback or None,
    )


def _unavailable_prediction(algorithm: str, reason: str) -> LatticeTimePrediction:
    return LatticeTimePrediction(
        algorithm=algorithm,
        prediction_ms=None,
        selected_features=(),
        evidence_count=0,
        selected_risk=None,
        exact_match=None,
        fallback=None,
        unavailable_reason=reason,
    )


def _eligible_observation(observation: ClauseObservation) -> bool:
    value = observation.latency_ms
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0.0
        and bool(observation.argv)
        and bool(observation.bin)
    )


def _observation_key(observation: ClauseObservation) -> _ObservationKey:
    assert observation.latency_ms is not None
    return (
        observation.repo,
        observation.bin,
        observation.argv,
        observation.ts_start,
        observation.ts_end,
        observation.latency_ms,
    )


def _observation_sort_key(
    observation: ClauseObservation,
) -> tuple[float, float, str, str, tuple[str, ...]]:
    return (
        observation.ts_end,
        observation.ts_start,
        observation.repo,
        observation.bin,
        observation.argv,
    )


def _observation_from_json(row: Any) -> ClauseObservation:
    if not isinstance(row, Mapping):
        raise ValueError("lattice KB observation must be an object")
    values = dict(row)
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
    unknown = sorted(values.keys() - allowed)
    if unknown:
        raise ValueError(f"lattice KB observation has unknown fields {unknown!r}")
    required = {"repo", "bin", "argv", "ts_start", "ts_end", "latency_ms"}
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(f"lattice KB observation is missing fields {missing!r}")
    if not isinstance(values["repo"], str):
        raise ValueError("lattice KB observation repo must be a string")
    if not isinstance(values["bin"], str) or not values["bin"]:
        raise ValueError("lattice KB observation bin must be a non-empty string")
    argv = values.get("argv")
    if not isinstance(argv, (list, tuple)) or not all(
        isinstance(item, str) for item in argv
    ) or not argv:
        raise ValueError("lattice KB observation argv must be non-empty strings")
    values["argv"] = tuple(argv)
    for field in ("ts_start", "ts_end", "latency_ms"):
        value = values[field]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ValueError(f"lattice KB observation {field} must be finite")
        values[field] = float(value)
    if values["latency_ms"] <= 0.0:
        raise ValueError("lattice KB observation latency_ms must be positive")
    return ClauseObservation(**values)


__all__ = [
    "LATTICE_TIME_ALGORITHMS",
    "LATTICE_TIME_KB_SCHEMA",
    "ClauseLatticeTimePredictions",
    "LatticeTimeKB",
    "LatticeTimePrediction",
]
