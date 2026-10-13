"""Node statistics: build, store, and query historical context nodes.

With subset coverage enabled, each generated node aggregates duration samples
from every command whose feature set contains the node's features.
"""

from __future__ import annotations

from kb_common import DEFAULT_MAX_OPTIONAL_FEATURES

import math
from functools import lru_cache
from collections import defaultdict
from datetime import datetime
from statistics import mean, median, stdev
from typing import Dict, FrozenSet, List, Sequence, Tuple

from tool_time._lattice_vendor.normalize import FeatureSet
from tool_time._lattice_vendor.schemas import NodeStats, Observation


def _loo_mse_log(values: Sequence[float], estimator: str = "median") -> float:
    """Leave-one-out MSE in log-space.

    For each sample, predict using the configured estimator (default median)
    of all *other* samples.  Returns the mean squared error.
    """
    if len(values) < 2:
        return 0.0

    n = len(values)
    sorted_vals = sorted(values)
    errors: List[float] = []

    if estimator == "median":
        # Precompute indices for median of n-1 elements after removing each position
        for i, v in enumerate(sorted_vals):
            # Median of remaining n-1 values
            mid = (n - 2) // 2
            # Removing i shifts only indices at/after i; keep the same
            # operands and arithmetic without allocating n-1 slices n times.
            left = sorted_vals[mid + (i <= mid)]
            if (n - 1) % 2 == 1:
                pred = left
            else:
                right = sorted_vals[mid + 1 + (i <= mid + 1)]
                pred = (left + right) / 2.0
            errors.append((v - pred) ** 2)
    elif estimator == "mean":
        total = sum(values)
        for v in values:
            pred = (total - v) / (n - 1)
            errors.append((v - pred) ** 2)
    else:
        # geometric_mean: compute product-based LOO
        import math
        log_total = sum(math.log(v) for v in values)
        for v in values:
            pred = math.exp((log_total - math.log(v)) / (n - 1))
            errors.append((v - pred) ** 2)

    return mean(errors)


def _compute_loso_risk(
    signature_raw: Dict[FeatureSet, List[float]],
    *, log_median=None,
) -> float:
    """Compute Leave-One-Signature-Out (LOSO) prediction risk for a node.

    For a node C covering m distinct command signatures, the LOSO risk is:

        z_q = log(1 + median(raw durations for signature q))
        z_hat_{C,-q} = mean({z_{q'} : q' != q})
        R_C = (1/m) * sum_{q} (z_q - z_hat_{C,-q})^2

    This measures how well node C predicts an unseen signature based on
    the other signatures it covers.  Low LOSO risk means the node's
    predictions generalize well across different concrete commands.

    Args:
        signature_raw: Mapping from exact feature set → list of raw
            durations (in seconds) for that signature under this node.

    Returns:
        LOSO risk value.  Returns 0.0 if m < 2 (not computable).
    """
    m = len(signature_raw)
    if m < 2:
        return 0.0

    # Step 1: compute z_q for each signature
    z_values: List[float] = []
    for raw_durations in signature_raw.values():
        z_q = (math.log1p(median(raw_durations)) if log_median is None
               else log_median(tuple(raw_durations)))
        z_values.append(z_q)

    # Step 2: LOSO prediction and squared error
    z_sum = sum(z_values)
    errors: List[float] = []
    for z_q in z_values:
        z_hat = (z_sum - z_q) / (m - 1)
        errors.append((z_q - z_hat) ** 2)

    return mean(errors)


def build_nodes(
    observations: Sequence[Observation],
    *,
    mode: str = "bounded",
    max_optional_features: int = DEFAULT_MAX_OPTIONAL_FEATURES,
    always_keep_exact: bool = True,
    min_partial_support: int = 1,
    estimator: str = "median",
    split_compounds: bool = True,
    subset_coverage: bool = False,
    context_cache: dict | None = None,
    statistics_cache=None,
    order_metadata=None,
) -> Tuple[Dict[FeatureSet, NodeStats], float, float]:
    """Build context nodes from historical observations.

    Args:
        observations: List of command observations.
        mode: Node generation mode (``"exhaustive"`` or ``"bounded"``).
        max_optional_features: Cap for bounded mode.
        always_keep_exact: Always keep full-command nodes.
        min_partial_support: Minimum samples for partial nodes.
            Full nodes are never filtered.
        split_compounds: If True, split compound commands (``&&``,
            ``||``, ``;``, ``|``) into individual clauses and process
            each clause independently.  Each clause inherits the
            observation's duration.
        subset_coverage: Reaggregate the generated node set using every
            observation whose features contain the node's features.
        context_cache: Optional build-local reuse of immutable feature sets
            across resource views. Never stores observations or statistics.

    Returns:
        Tuple of ``(nodes_dict, global_log_var, global_log_std)``.
    """
    from tool_time._lattice_vendor.features import CoverageIndex, generate_context_nodes
    from tool_time._lattice_vendor.normalize import normalize_command, split_all_clauses

    # Collect durations per node
    node_durations: Dict[FeatureSet, List[float]] = defaultdict(list)
    node_signature_logs: Dict[FeatureSet, Dict[FeatureSet, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    node_signature_raw: Dict[FeatureSet, Dict[FeatureSet, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    node_last_updated: Dict[FeatureSet, str] = {}
    all_log_durations: List[float] = []
    exact_feature_sets: set[FeatureSet] = set()
    normalized: list[tuple[FeatureSet, float, str]] = []
    contexts = {} if context_cache is None else context_cache
    generated_counts: dict[FeatureSet, int] = defaultdict(int)

    first_cover = {}
    for observation_index, obs in enumerate(observations):
        if obs.duration_s <= 0:
            raise ValueError(f"Duration must be positive, got {obs.duration_s}: cmd={obs.cmd}")

        # Split compound commands into clauses; each clause is an
        # independent simple command that inherits the observation's
        # total duration (best-effort when per-clause timing is unavailable).
        if split_compounds and obs.command_features is None:
            clauses = split_all_clauses(obs.cmd)
        else:
            clauses = [obs.cmd]

        for clause_cmd in clauses:
            if obs.command_features is not None:
                from command_parser.models import CommandFeatures
                record = CommandFeatures.from_dict(obs.command_features)
                features, core = record.features, record.core
            else:
                features, core = normalize_command(
                    clause_cmd, repo=obs.repo, cwd=obs.cwd, env_id=obs.env_id,
                    is_clause=not split_compounds or len(clauses) > 1,
                )
            exact_feature_sets.add(features)
            if subset_coverage:
                normalized.append((features, obs.duration_s, obs.timestamp))
            log_duration = math.log1p(obs.duration_s)
            all_log_durations.append(log_duration)

            key = (features, core, mode, max_optional_features, always_keep_exact)
            if key not in contexts:
                contexts[key] = generate_context_nodes(
                    features, core, mode=mode, max_optional_features=max_optional_features,
                    always_keep_exact=always_keep_exact,
                )
            for node_fs in contexts[key]:
                generated_counts[node_fs] += 1
                if subset_coverage:
                    # This pass defines existence/support only. Samples are
                    # accumulated once, by complete subset coverage below.
                    continue
                if order_metadata is not None and node_fs not in node_durations:
                    first_cover[node_fs] = observation_index
                node_durations[node_fs].append(obs.duration_s)
                node_signature_logs[node_fs][features].append(log_duration)
                node_signature_raw[node_fs][features].append(obs.duration_s)
                if obs.timestamp:
                    node_last_updated[node_fs] = _latest_timestamp(
                        node_last_updated.get(node_fs, ""),
                        obs.timestamp,
                    )

    if subset_coverage:
        # Bounded generation defines which nodes exist.  It does not define
        # their coverage: an existing node can be a subset of an observation
        # even when that observation did not generate the node.
        coverage = CoverageIndex(generated_counts)
        @lru_cache(maxsize=4096)
        def covered_nodes(features, _index=coverage):
            return tuple(_index.subsets(features))
        node_durations = defaultdict(list)
        node_signature_logs = defaultdict(lambda: defaultdict(list))
        node_signature_raw = defaultdict(lambda: defaultdict(list))
        node_last_updated = {}
        for observation_index, (features, duration, timestamp) in enumerate(normalized):
            log_duration = math.log1p(duration)
            for node_fs in covered_nodes(features):
                if order_metadata is not None and node_fs not in node_durations:
                    first_cover[node_fs] = observation_index
                node_durations[node_fs].append(duration)
                node_signature_logs[node_fs][features].append(log_duration)
                node_signature_raw[node_fs][features].append(duration)
                if timestamp:
                    node_last_updated[node_fs] = _latest_timestamp(
                        node_last_updated.get(node_fs, ""), timestamp,
                    )
        covered_nodes.cache_clear()
        del covered_nodes, coverage
    del contexts, normalized

    # Global statistics
    global_log_mean = mean(all_log_durations) if all_log_durations else 0.0
    global_log_std = stdev(all_log_durations) if len(all_log_durations) >= 2 else 0.5
    global_log_var = global_log_std ** 2

    # Build NodeStats for each node
    @lru_cache(maxsize=4096)
    def signature_log_mean(values):
        return mean(values)

    @lru_cache(maxsize=4096)
    def signature_log_median(values):
        return math.log1p(median(values))

    def compute_statistics(durations, log_groups, signature_raw):
        logs = [math.log1p(v) for v in durations]
        std_log = stdev(logs) if len(logs) >= 2 else global_log_std
        stderr_log = std_log / math.sqrt(len(logs)) if len(logs) >= 2 else global_log_std
        geo_mean = math.exp(sum(math.log(v) for v in durations) / len(durations)) if durations else 0.0
        signature_log_means = [
            signature_log_mean(signature_logs)
            for signature_logs in log_groups
        ]
        signature_count = len(signature_log_means)
        signature_log_mean_var = (
            stdev(signature_log_means) ** 2
            if len(signature_log_means) >= 2
            else 0.0
        )

        return dict(
            mean_s=mean(durations),
            median_s=median(durations),
            geometric_mean_s=geo_mean,
            count=len(durations),
            loo_mse_log=_loo_mse_log(logs, estimator=estimator),
            stderr_log=stderr_log,
            std_log=std_log,
            mean_log=mean(logs),
            signature_count=signature_count,
            signature_log_mean_var=signature_log_mean_var,
            loso_risk=_compute_loso_risk(dict(enumerate(signature_raw)), log_median=signature_log_median),
        )

    @lru_cache(maxsize=4096)
    def statistics_for(durations, log_groups, signature_raw):
        if statistics_cache is None:
            return compute_statistics(durations, log_groups, signature_raw)
        key = ("time", estimator, global_log_std.hex() if len(durations) < 2 else None,
               tuple(v.hex() for v in durations),
               tuple(tuple(v.hex() for v in group) for group in log_groups),
               tuple(tuple(v.hex() for v in group) for group in signature_raw))
        return statistics_cache.get_or_compute(
            key, lambda: compute_statistics(durations, log_groups, signature_raw))

    nodes: Dict[FeatureSet, NodeStats] = {}
    for fs, durations in node_durations.items():
        # Filter partial nodes by support, but never filter exact nodes.
        if fs not in exact_feature_sets and generated_counts[fs] < min_partial_support:
            continue
        nodes[fs] = NodeStats(
            features=fs, durations=list(durations),
            own_durations=list(node_signature_raw.get(fs, {}).get(fs, [])),
            last_updated=node_last_updated.get(fs, ""),
            **statistics_for(tuple(durations),
                tuple(tuple(group) for group in node_signature_logs.get(fs, {}).values()),
                tuple(tuple(group) for group in node_signature_raw.get(fs, {}).values())),
        )

    if order_metadata is not None:
        order_metadata.update(generation_order={fs: i for i, fs in enumerate(generated_counts)},
                              first_cover=first_cover)
    return nodes, global_log_var, global_log_std


def _latest_timestamp(current: str, candidate: str) -> str:
    """Return the later timestamp, preserving the original string."""
    if not current:
        return candidate
    current_dt = _parse_iso(current)
    candidate_dt = _parse_iso(candidate)
    if current_dt is not None and candidate_dt is not None:
        return candidate if candidate_dt > current_dt else current
    return max(current, candidate)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def get_estimate(stats: NodeStats, estimator: str = "median") -> float:
    """Get point prediction from node statistics.

    Args:
        stats: Node statistics.
        estimator: ``"mean"``, ``"median"``, or ``"geometric_mean"``.

    Returns:
        Predicted duration in seconds.
    """
    if estimator == "mean":
        return stats.mean_s
    elif estimator == "geometric_mean":
        return stats.geometric_mean_s
    else:  # median (default)
        return stats.median_s
