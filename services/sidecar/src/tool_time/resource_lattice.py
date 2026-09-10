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
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from tool_resource.runtime_kb import ClauseObservation, is_pipeline_dependent_consumer
from tool_time._lattice_vendor.features import generate_context_nodes
from tool_time._lattice_vendor.nodes import _compute_loso_risk, _loo_mse_log
from tool_time._lattice_vendor.normalize import FeatureSet, normalize_command
from tool_time._lattice_vendor.schemas import NodeStats, Observation
from tool_time._lattice_vendor.selector import predict as select_prediction
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances

# Scales are numerical conditioning only; public predictions use declared units.
RESOURCE_TARGETS = {
    "cpu_time_seconds": ("core_seconds", 1.0),
    "cpu_avg_cores": ("cores", 1.0),
    "cpu_peak_cores": ("cores", 1.0),
    "memory_peak_rss_bytes": ("bytes", 1024.0 * 1024.0),
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
    if nonnegative(row.peak_cpu_cores):
        values["cpu_peak_cores"] = float(row.peak_cpu_cores)
    if nonnegative(row.sampled_peak_rss_mb):
        value = float(row.sampled_peak_rss_mb) * 1024 * 1024
        if math.isfinite(value):
            values["memory_peak_rss_bytes"] = value
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
    nodes: dict[FeatureSet, NodeStats]
    global_log_var: float
    global_log_std: float

    def predict(
        self, repo: str, argv: Sequence[str], target: str, algorithm: str,
        *, threshold: float | None = None,
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
        features, _ = normalize_command(command, repo=repo)
        candidates = [node for fs, node in self.nodes.items() if fs.issubset(features)]
        # An unrelated global workload is not a resource estimate for this tool.
        if not candidates:
            return unavailable("no_matching_resource_context")
        if (
            algorithm == "shrinkage" and features not in self.nodes
            and len(candidates) > config._MAX_SHRINKAGE_CANDIDATES
        ):
            return unavailable("lattice_candidate_limit_exceeded")
        if algorithm == "max_cardinality":
            selected = max(candidates, key=lambda node: (
                len(node.features), node.count, tuple(sorted(node.features))
            ))
            risk = None
        else:
            result = select_prediction(
                command, self.nodes, repo=repo,
                global_log_var=self.global_log_var, global_log_std=self.global_log_std,
                beta=0.0, gamma=0.0, delta=config._DOMINANCE_DELTA,
                risk_method=algorithm, exact_match_shortcut=(algorithm == "shrinkage"),
                context_sample_alpha=config._CONTEXT_SAMPLE_ALPHA, estimator="median",
                shrinkage_kappa=config._SHRINKAGE_KAPPA, loso_min_signatures=2,
                specificity_risk_tolerance=config._SPECIFICITY_RISK_TOLERANCE,
                risk_weight=config._LOSO_RISK_WEIGHT,
            )
            selected = self.nodes[frozenset(result.selected_features)]
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


def build_resource_states(observations: Sequence[ClauseObservation], *, load: bool = False) -> dict[str, ResourceState]:
    """Build per-target statistics, retaining zero values and target eligibility.

Use the vendored node/selector data model without its positive-duration-only
builder. Risk remains log1p(value/scale); zero CPU/RSS is a valid measurement.
"""
    from tool_time import lattice_kb as config

    states = {}
    def measured(row: ClauseObservation) -> dict[str, float]:
        if load and (
            row.in_loop or row.in_subst or is_pipeline_dependent_consumer(row)
        ):
            return {}
        values = resource_values(row)
        if load and nonnegative(row.latency_ms):
            values["duration_ms"] = float(row.latency_ms)
        return values

    for target, (_unit, scale) in (LOAD_TARGETS if load else RESOURCE_TARGETS).items():
        training = [
            Observation(cmd=shlex.join(row.argv), repo=row.repo, duration_s=values[target] / scale)
            for row in observations if target in (values := measured(row))
        ]
        maximum = config._effective_max_optional_features(training)
        samples: dict[FeatureSet, list[float]] = defaultdict(list)
        signatures: dict[FeatureSet, dict[FeatureSet, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        all_logs = []
        for row in training:
            features, core = normalize_command(row.cmd, repo=row.repo)
            all_logs.append(math.log1p(row.duration_s))
            for fs in generate_context_nodes(
                features, core, mode=config._NODE_MODE, max_optional_features=maximum,
                always_keep_exact=True,
            ):
                samples[fs].append(row.duration_s)
                signatures[fs][features].append(row.duration_s)
        global_std = statistics.stdev(all_logs) if len(all_logs) > 1 else 0.5
        nodes = {}
        for fs, values in samples.items():
            logs = [math.log1p(value) for value in values]
            std = statistics.stdev(logs) if len(logs) > 1 else global_std
            signature_means = [
                statistics.mean(math.log1p(value) for value in group)
                for group in signatures[fs].values()
            ]
            nodes[fs] = NodeStats(
                features=fs, durations=values, count=len(values),
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
                loso_risk=_compute_loso_risk(signatures[fs]),
            )
        compute_shrinkage_variances(
            nodes, kappa=config._SHRINKAGE_KAPPA, global_log_var=global_std ** 2
        )
        states[target] = ResourceState(nodes, global_std ** 2, global_std)
    return states
