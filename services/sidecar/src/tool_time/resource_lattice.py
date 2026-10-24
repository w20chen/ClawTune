"""Resource views over the time lattice's raw clause observations.

No heavy labels are stored. Each measured target selects its own context and
returns empirical quantiles. RSS is clause-lineage RSS, never cgroup memory.
CPU peak follows the collector's fixed 500 ms wall-window convention.
"""
from __future__ import annotations

import math
import shlex
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any, Mapping, Sequence

from command_parser import ParseContext, default_service
from command_parser.models import CommandFeatures
from command_parser.service import observation_features
from tool_resource.runtime_kb import ClauseObservation, is_pipeline_dependent_consumer
from tool_time._lattice_vendor.features import CoverageIndex, generate_context_nodes
from tool_time._lattice_vendor.nodes import _compute_loso_risk, _loo_mse_log
from tool_time._lattice_vendor.normalize import FeatureSet
from tool_time._lattice_vendor.schemas import NodeStats, Observation
from tool_time._lattice_vendor.selector import predict as select_prediction
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances
from tool_time.query import query_nodes
from kb_common import DEFAULT_MAX_OPTIONAL_FEATURES, validate_max_optional_features

# Scales are numerical conditioning only; public predictions use declared units.
RESOURCE_TARGETS = {
    "cpu_time_seconds": ("core_seconds", 1.0),
    "cpu_avg_cores": ("cores", 1.0),
    "cpu_peak_cores": ("cores", 1.0),
    "sampled_peak_rss_bytes": ("bytes", 1024.0 * 1024.0),
    "memory_total_peak_bytes": ("bytes", 1024.0 * 1024.0),
    "memory_extra_peak_bytes": ("bytes", 1024.0 * 1024.0),
}
LOAD_TARGETS = {"duration_ms": ("ms", 1000.0), **RESOURCE_TARGETS}


def nonnegative(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def resource_values(row: ClauseObservation) -> dict[str, float]:
    values: dict[str, float] = {}
    if nonnegative(row.cpu_ns_cumulative):
        seconds = float(row.cpu_ns_cumulative) / 1e9
        values["cpu_time_seconds"] = seconds
        if nonnegative(row.latency_ms) and row.latency_ms > 0:
            values["cpu_avg_cores"] = seconds / (row.latency_ms / 1000.0)
    if nonnegative(row.cpu_peak_cores):
        values["cpu_peak_cores"] = float(row.cpu_peak_cores)
    if nonnegative(row.sampled_peak_rss_mb):
        values["sampled_peak_rss_bytes"] = float(row.sampled_peak_rss_mb) * 1_000_000.0
    for target in ("memory_total_peak_bytes", "memory_extra_peak_bytes"):
        value = getattr(row, target)
        if nonnegative(value):
            values[target] = float(value)
    if "cpu_avg_cores" not in values and "cpu_time_seconds" in row.imputed_targets:
        values["cpu_avg_cores"] = 0.0
    return {key: value for key, value in values.items() if math.isfinite(value)}


@dataclass(frozen=True)
class ResourcePrediction:
    target: str
    unit: str
    algorithm: str
    p50: float | None = None
    p90: float | None = None
    selected_features: tuple[str, ...] = ()
    evidence_count: int = 0
    selected_risk: float | None = None
    exact_match: bool | None = None
    unavailable_reason: str | None = None
    threshold: float | None = None
    probability_ge: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["selected_features"] = list(self.selected_features)
        return result


@dataclass
class ResourceState:
    nodes: Mapping[FeatureSet, NodeStats]
    global_log_var: float
    global_log_std: float
    query_index: CoverageIndex | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Nodes are immutable after construction; replace the whole state on update.
        if self.query_index is None:
            self.query_index = CoverageIndex(self.nodes)

    def predict(
        self, repo: str, argv: Sequence[str], target: str, algorithm: str,
        *, threshold: float | None = None, command_features: CommandFeatures | None = None,
    ) -> ResourcePrediction:
        from tool_time import lattice_kb as config

        unit, scale = LOAD_TARGETS[target]
        def unavailable(reason: str) -> ResourcePrediction:
            return ResourcePrediction(
                target, unit, algorithm, unavailable_reason=reason, threshold=threshold
            )
        if not self.nodes:
            return unavailable("no_lattice_resource_evidence")
        command = shlex.join(argv)
        query = command_features or default_service().argv(argv, ParseContext(repo=repo))
        features = query.features
        candidates = query_nodes(self.nodes, query, global_log_std=self.global_log_std,
                                 index=self.query_index)
        # An unrelated global workload is not a resource estimate for this tool.
        if not candidates:
            return unavailable("no_matching_resource_context")
        if (
            algorithm == "shrinkage" and features not in self.nodes
            and len(candidates) > config._MAX_SHRINKAGE_CANDIDATES
        ):
            return unavailable("lattice_candidate_limit_exceeded")
        if algorithm == "max_cardinality":
            selected = max(candidates.values(), key=lambda node: (
                len(node.features), node.count, tuple(sorted(node.features))
            ))
            risk = None
        else:
            result = select_prediction(
                command, candidates, repo=repo, query_features=features,
                global_log_var=self.global_log_var, global_log_std=self.global_log_std,
                beta=0.0, gamma=0.0, delta=config._DOMINANCE_DELTA,
                risk_method=algorithm, exact_match_shortcut=(algorithm == "shrinkage"),
                context_sample_alpha=config._CONTEXT_SAMPLE_ALPHA, estimator="median",
                shrinkage_kappa=config._SHRINKAGE_KAPPA, loso_min_signatures=2,
                specificity_risk_tolerance=config._SPECIFICITY_RISK_TOLERANCE,
                risk_weight=config._LOSO_RISK_WEIGHT,
            )
            selected = candidates[frozenset(result.selected_features)]
            risk = result.selected_risk
        values = sorted(value * scale for value in selected.durations)
        return ResourcePrediction(
            target, unit, algorithm,
            p50=statistics.median(values),
            p90=values[max(0, math.ceil(0.9 * len(values)) - 1)],
            selected_features=tuple(sorted(selected.features)), evidence_count=len(values),
            selected_risk=risk, exact_match=selected.features == features,
            threshold=threshold,
            probability_ge=(
                sum(value >= threshold for value in values) / len(values)
                if threshold is not None else None
            ),
        )


def build_resource_states(observations: Sequence[ClauseObservation], *, load: bool = False,
                          subset_coverage: bool = True,
                          targets: Sequence[str] | None = None,
                          state_cache: dict | None = None,
                          context_cache: dict | None = None,
                          feature_cache: dict | None = None,
                          statistics_cache=None,
                          max_optional_features: int = DEFAULT_MAX_OPTIONAL_FEATURES) -> dict[str, ResourceState]:
    """Build per-target statistics, retaining zero values and target eligibility.

Use the vendored node/selector data model without its positive-duration-only
builder. Risk remains log1p(value/scale); zero CPU/RSS is a valid measurement.
"""
    from tool_time import lattice_kb as config

    maximum = validate_max_optional_features(max_optional_features)
    states = {}
    features_by_row = {} if feature_cache is None else feature_cache
    def features_for(row):
        key = id(row)
        if key not in features_by_row:
            features_by_row[key] = observation_features(row)
        return features_by_row[key]
    def measured(row: ClauseObservation) -> dict[str, float]:
        if load and (
            row.in_loop or row.in_subst or is_pipeline_dependent_consumer(row)
        ):
            return {}
        values = resource_values(row)
        if load and nonnegative(row.latency_ms):
            values["duration_ms"] = float(row.latency_ms)
        return values

    measured_rows = [(row, measured(row)) for row in observations]
    target_config = LOAD_TARGETS if load else RESOURCE_TARGETS
    for target in target_config if targets is None else targets:
        _unit, scale = target_config[target]
        training = [
            Observation(cmd=shlex.join(row.argv), repo=row.repo, duration_s=values[target] / scale, command_features=features_for(row).to_dict())
            for row, values in measured_rows if target in values
        ]
        records = [CommandFeatures.from_dict(row.command_features) for row in training]
        cache_key = (subset_coverage, maximum, tuple(
            (record.features, record.core, row.duration_s.hex())
            for record, row in zip(records, training)
        ))
        if state_cache is not None and cache_key in state_cache:
            states[target] = state_cache[cache_key]
            continue
        generated: dict[FeatureSet, None] = {}
        contexts = {} if context_cache is None else context_cache
        normalized: list[tuple[FeatureSet, float, list[FeatureSet]]] = []
        all_logs = []
        for row, record in zip(training, records):
            features, core = record.features, record.core
            all_logs.append(math.log1p(row.duration_s))
            key = (features, core, config._NODE_MODE, maximum, True)
            if key not in contexts:
                contexts[key] = generate_context_nodes(
                    features, core, mode=config._NODE_MODE, max_optional_features=maximum,
                    always_keep_exact=True,
                )
            own_nodes = contexts[key]
            normalized.append((features, row.duration_s, own_nodes))
            generated.update(dict.fromkeys(own_nodes))
        coverage = CoverageIndex(generated)
        @lru_cache(maxsize=4096)
        def covered_nodes(features, _index=coverage):
            return tuple(_index.subsets(features))
        samples: dict[FeatureSet, list[float]] = defaultdict(list)
        signatures: dict[FeatureSet, dict[FeatureSet, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        generation_order = {fs: i for i, fs in enumerate(generated)}
        first_cover = {}
        for position, (features, value, own_nodes) in enumerate(normalized):
            for fs in covered_nodes(features) if subset_coverage else own_nodes:
                if fs not in samples:
                    first_cover[fs] = position
                samples[fs].append(value)
                signatures[fs][features].append(value)
        covered_nodes.cache_clear()
        del covered_nodes, coverage, contexts, normalized, generated
        global_std = statistics.stdev(all_logs) if len(all_logs) > 1 else 0.5
        @lru_cache(maxsize=4096)
        def signature_log_mean(values):
            return statistics.mean(math.log1p(value) for value in values)

        @lru_cache(maxsize=4096)
        def signature_log_median(values):
            return math.log1p(statistics.median(values))

        def compute_statistics(values, groups):
            logs = [math.log1p(value) for value in values]
            std = statistics.stdev(logs) if len(logs) > 1 else global_std
            signature_means = [
                signature_log_mean(group)
                for group in groups
            ]
            return dict(
                count=len(values),
                mean_s=statistics.mean(values), median_s=statistics.median(values),
                geometric_mean_s=(
                    math.exp(statistics.mean(math.log(v) for v in values))
                    if all(v > 0 for v in values) else 0.0
                ),
                mean_log=statistics.mean(logs), std_log=std,
                stderr_log=std / math.sqrt(len(values)),
                loo_mse_log=_loo_mse_log(logs, estimator="median"),
                signature_count=len(signature_means),
                signature_log_mean_var=(
                    statistics.variance(signature_means) if len(signature_means) > 1 else 0.0
                ),
                loso_risk=_compute_loso_risk(dict(enumerate(groups)), log_median=signature_log_median),
            )
        @lru_cache(maxsize=4096)
        def statistics_for(values, groups, _prior=global_std, _compute=compute_statistics):
            if statistics_cache is None:
                return _compute(values, groups)
            key = ("resource", _prior.hex() if len(values) < 2 else None,
                   tuple(v.hex() for v in values),
                   tuple(tuple(v.hex() for v in group) for group in groups))
            return statistics_cache.get_or_compute(key, lambda: _compute(values, groups))
        nodes = {}
        for fs, values in samples.items():
            nodes[fs] = NodeStats(
                features=fs, durations=values,
                own_durations=list(signatures[fs].get(fs, [])),
                **statistics_for(tuple(values), tuple(tuple(group) for group in signatures[fs].values())),
            )
        states[target] = ResourceState(nodes, global_std ** 2, global_std)
        states[target].query_index.generation_order = generation_order
        states[target].query_index.first_cover = first_cover
        compute_shrinkage_variances(
            nodes, kappa=config._SHRINKAGE_KAPPA, global_log_var=global_std ** 2,
            index=states[target].query_index, reuse_sample_std=True,
        )
        if state_cache is not None:
            state_cache[cache_key] = states[target]
    return states
