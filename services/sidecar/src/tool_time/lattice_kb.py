"""Clause-level time and resource prediction backed by one mixed lattice KB.

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
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from tool_resource.features import shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import ClauseObservation, is_pipeline_dependent_consumer
from tool_time._lattice_vendor.features import estimate_node_count
from tool_time._lattice_vendor.nodes import build_nodes
from tool_time._lattice_vendor.normalize import FeatureSet, normalize_command
from tool_time._lattice_vendor.schemas import NodeStats, Observation, PredictionResult
from tool_time._lattice_vendor.selector import predict as select_prediction
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances
from tool_time.resource_lattice import (
    RESOURCE_TARGETS, LOAD_TARGETS, ResourcePrediction, ResourceState, build_resource_states,
    nonnegative, resource_values,
)


LATTICE_TIME_ALGORITHMS = ("shrinkage", "loso", "max_cardinality")
LATTICE_TIME_KB_SCHEMA = "clause_lattice_kb_v3"
_LEGACY_SCHEMA = "clause_lattice_time_kb_v1"

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

_ObservationKey = tuple[Any, ...]
_NodeState = tuple[dict[FeatureSet, NodeStats], float, float, float, dict[str, ResourceState]]


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
    """One raw observation log with independent time and resource lattice views.

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
        self._resource_states: dict[str, ResourceState] = {}
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

        if getattr(self, "_frozen", False):
            return 0
        baseline = Counter(_observation_key(item) for item in self._observations)
        baseline.update(_observation_key(item[2]) for item in self._pending)
        seen: Counter[_ObservationKey] = Counter()
        added = 0
        for observation in observations:
            observation = _sanitize_resources(observation)
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

        if getattr(self, "_frozen", False):
            return False
        observation = _sanitize_resources(observation)
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

    def freeze(self) -> None:
        if self._pending:
            raise ValueError("frozen KB requires a finalized snapshot (pending must be empty)")
        self.prepare()
        self._last_query_ts = None
        self._frozen = True

    def fork_for_update(self) -> "LatticeTimeKB":
        """Copy immutable raw observations for background generation building.

        The owner serializes this short copy with queries, builds the successor
        outside that lock, and publishes the prepared successor atomically.
        Derived nodes are intentionally not copied or shared with the writer.
        """
        successor = type(self)()
        successor._observations = list(self._observations)
        successor._pending = list(self._pending)
        successor._pending_seq = self._pending_seq
        successor._data_generation = self._data_generation
        successor._last_query_ts = self._last_query_ts
        successor._dirty = True
        return successor

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
            if is_pipeline_dependent_consumer(clause):
                continue
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

    def predict_resource_clauses(
        self, repo: str, clauses: Sequence[Mapping[str, Any]], ts_start: float,
        *, parse_failed: bool = False, shell_command: bool = True,
        thresholds: Mapping[str, float] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Query independent resource distributions before execution.

        Optional thresholds are query-time inclusive exceedance requests, not
        persisted labels. Empirical probabilities are not calibrated confidence.
        The caller serializes this query with time prediction under the KB lock.
        """
        thresholds = thresholds or {}
        for target, value in thresholds.items():
            if target not in RESOURCE_TARGETS or not nonnegative(value):
                raise ValueError("resource thresholds require known targets and finite nonnegative values")
        self._advance(ts_start)
        self._ensure_nodes()
        outcomes = []
        for index, clause in enumerate(clauses):
            if is_pipeline_dependent_consumer(clause):
                continue
            bin_ = str(clause["bin"])
            argv = tuple(str(value) for value in clause["argv"])
            if not argv or not bin_ or (shell_command and not shell_bin_requires_exec_evidence(bin_, argv[0])):
                continue
            predictions = []
            for target, (unit, _scale) in RESOURCE_TARGETS.items():
                state_key = target + (":" + clause.get("memory_measurement", "cgroup_v2_memory_current") if target.startswith("memory_") else "")
                state = self._resource_states.get(state_key, ResourceState({}, 0.0, 0.5))
                for algorithm in LATTICE_TIME_ALGORITHMS:
                    try:
                        result = (
                            ResourcePrediction(target, unit, algorithm, unavailable_reason="parse_failed", threshold=thresholds.get(target))
                            if parse_failed else state.predict(repo, argv, target, algorithm, threshold=thresholds.get(target))
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        result = ResourcePrediction(target, unit, algorithm, unavailable_reason=f"lattice_resource_error:{type(exc).__name__}")
                    predictions.append(result.to_dict())
            outcomes.append({
                "clause_index": index, "bin": bin_, "argv": list(argv),
                "scope": "clause_owned_lineage", "memory_metric": "environment_memory",
                "cpu_peak_window_ms": 500, "quantile_method": "median_p50_nearest_rank_p90",
                "predictions": predictions,
            })
        return tuple(outcomes)

    def predict_load_samples(self, repo: str, clauses: Sequence[Mapping[str, Any]],
                             ts_start: float, *, algorithm: str = "shrinkage") -> tuple[dict[str, dict[str, Any]], ...]:
        """Full-target standalone clause evidence, selected independently.

        The shared call composer owns all call-level statistics. Raw samples
        remain internal and are never serialized in a decision payload.
        """
        if algorithm not in LATTICE_TIME_ALGORITHMS:
            raise ValueError("unknown lattice algorithm")
        self._advance(ts_start)
        self._ensure_nodes()
        outcomes = []
        for clause in clauses:
            if is_pipeline_dependent_consumer(clause):
                continue
            targets = {}
            for target, (_, scale) in LOAD_TARGETS.items():
                state_key = "load:" + target
                if target.startswith("memory_"):
                    state_key += ":" + clause.get("memory_measurement", "cgroup_v2_memory_current")
                state = self._resource_states.get(state_key)
                if state is None:
                    continue
                try:
                    result = state.predict(repo, clause["argv"], target, algorithm)
                    if result.unavailable_reason is None:
                        node = state.nodes[frozenset(result.selected_features)]
                        targets[target] = {"values": tuple(v * scale for v in node.durations),
                                           "context": (algorithm, *result.selected_features)}
                except (KeyError, TypeError, ValueError) as exc:
                    targets[target] = {"values": (), "unavailable_reason": f"target_error:{type(exc).__name__}"}
            outcomes.append(targets)
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
                evidence_count=sum(nonnegative(row.latency_ms) and row.latency_ms > 0 for row in self._observations),
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
        if getattr(self, "_frozen", False):
            return
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
            self._resource_states,
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
        resource_only = obj.get("schema") == LATTICE_TIME_KB_SCHEMA
        kb.merge_historical(
            _observation_from_json(row, allow_resource_only=resource_only)
            for row in observation_rows
        )
        for row in pending_rows:
            kb.observe_completed_clause(
                _observation_from_json(row, allow_resource_only=resource_only)
            )
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
        if nonnegative(observation.latency_ms) and observation.latency_ms > 0
    ]
    resources = build_resource_states(ordered)
    resources.update({"load:" + target: state for target, state in build_resource_states(ordered, load=True).items()})
    for measurement in {row.memory_measurement for row in ordered if row.memory_measurement}:
        subset = [row for row in ordered if row.memory_measurement == measurement]
        for load, prefix in [(False, ""), (True, "load:")]:
            for target, state in build_resource_states(subset, load=load).items():
                if target.startswith("memory_"):
                    resources[prefix + target + ":" + measurement] = state
    for key in list(resources):
        if key.removeprefix("load:").startswith("memory_") and key.count(":") <= (1 if key.startswith("load:") else 0):
            del resources[key]

    if not training:
        return {}, 0.0, 0.5, 0.0, resources
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
    return nodes, global_log_var, global_log_std, global_median_s, resources


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


def _sanitize_resources(observation: ClauseObservation) -> ClauseObservation:
    """Mask invalid optional resources without losing valid time/other targets."""
    from tool_resource.commands import normalized_observation
    observation = normalized_observation(observation)
    fields = ("cpu_ns_cumulative", "cpu_peak_cores", "sampled_peak_rss_mb", "memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes")
    invalid = {name: None for name in fields
               if getattr(observation, name) is not None and not nonnegative(getattr(observation, name))}
    if observation.latency_ms is not None and not nonnegative(observation.latency_ms):
        invalid["latency_ms"] = None
    return replace(observation, **invalid) if invalid else observation


def _eligible_observation(observation: ClauseObservation) -> bool:
    return (
        bool(observation.argv) and bool(observation.bin)
        and not is_pipeline_dependent_consumer(observation)
        and ((nonnegative(observation.latency_ms) and observation.latency_ms > 0)
             or bool(resource_values(observation)))
    )


def _observation_key(observation: ClauseObservation) -> _ObservationKey:
    return (
        observation.repo,
        observation.bin,
        observation.argv,
        observation.ts_start,
        observation.ts_end,
        observation.latency_ms,
        observation.cpu_ns_cumulative,
        observation.cpu_peak_cores,
        observation.sampled_peak_rss_mb,
        observation.in_loop,
        observation.in_pipe,
        observation.in_subst,
        observation.pipeline_position,
        observation.memory_baseline_bytes, observation.memory_total_peak_bytes,
        observation.memory_extra_peak_bytes, observation.memory_measurement,
        observation.memory_environment_id, observation.memory_eligible,
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


def _observation_from_json(row: Any, *, allow_resource_only: bool = True) -> ClauseObservation:
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
        "cpu_peak_cores",
        "sampled_peak_rss_mb",
        "cpu_ns_cumulative",
        "in_loop",
        "in_pipe",
        "in_subst",
        "pipeline_position",
        "memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes",
        "memory_measurement", "memory_environment_id", "memory_eligible",
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
        if allow_resource_only and field == "latency_ms" and values[field] is None and any(
            nonnegative(values.get(key)) for key in ("cpu_ns_cumulative", "cpu_peak_cores", "sampled_peak_rss_mb", "memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes")
        ):
            continue
        value = values[field]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ValueError(f"lattice KB observation {field} must be finite")
        values[field] = float(value)
    for field in ("cpu_ns_cumulative", "cpu_peak_cores", "sampled_peak_rss_mb", "memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes"):
        if values.get(field) is not None and not nonnegative(values[field]):
            raise ValueError(f"lattice KB observation {field} must be finite and non-negative")
    if values["latency_ms"] is not None and values["latency_ms"] <= 0.0:
        zero_resource_row = allow_resource_only and values["latency_ms"] == 0 and any(
            nonnegative(values.get(key)) for key in ("cpu_ns_cumulative", "cpu_peak_cores", "sampled_peak_rss_mb", "memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes")
        )
        if not zero_resource_row:
            raise ValueError("lattice KB observation latency_ms must be positive")
    return ClauseObservation(**values)


__all__ = [
    "LATTICE_TIME_ALGORITHMS",
    "LATTICE_TIME_KB_SCHEMA",
    "ClauseLatticeTimePredictions",
    "LatticeTimeKB",
    "LatticeTimePrediction",
]
