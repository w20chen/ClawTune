"""Bayesian shrinkage variance estimation for context nodes.

Implements a hierarchical shrinkage estimator that pools variance
information from parent (more general) nodes to stabilize variance
estimates for nodes with small sample sizes.

For a node C:
    R_C = ((m_C - 1) * s_C^2 + κ * v_parent(C)) / ((m_C - 1) + κ)

where:
- m_C: number of commands matching node C
- s_C^2: sample variance of log-duration for node C
- v_parent(C): median of R_P for immediate parent nodes P ⊂ C
- κ: shrinkage strength hyperparameter (default 5)

When m_C = 1: R_C = v_parent(C) (pure shrinkage to parent).
Top-level nodes (no parents): use own statistics directly.
Top-level nodes with single sample: marked as cold-start.
"""

from __future__ import annotations

import math
from functools import lru_cache
from statistics import median, stdev
from typing import Dict, List

from tool_time._lattice_vendor.features import CoverageIndex
from tool_time._lattice_vendor.normalize import FeatureSet
from tool_time._lattice_vendor.schemas import NodeStats


def find_immediate_parents(
    node_fs: FeatureSet,
    all_nodes: Dict[FeatureSet, NodeStats],
    *,
    _by_size: Dict[int, List[FeatureSet]] | None = None,
    index: CoverageIndex | None = None,
) -> List[FeatureSet]:
    """Return the largest-cardinality layer of existing strict subsets.

    This preserves the original selector's layer policy, which can differ from
    all inclusion-maximal subsets in a sparse lattice. Small layers use direct
    combination lookup; an optional index avoids enumerating large absent layers.
    Both paths return the original combinations(list(node_fs), size) order.
    _by_size is ignored for backward compatibility.
    """
    k = len(node_fs)
    if k == 0:
        return []

    features = list(node_fs)

    # Try removing 1 feature at a time: O(k) hash lookups
    for size in range(k - 1, -1, -1):
        if index is not None and math.comb(k, size) > max(64, min(4096, len(all_nodes))):
            # Preserve the existing largest-cardinality-layer policy, including
            # combinations() order, without enumerating absent intermediate nodes.
            matches = [fs for fs in index.subsets(node_fs) if len(fs) <= size]
            if not matches:
                return []
            largest = max(map(len, matches))
            positions = {feature: i for i, feature in enumerate(features)}
            return sorted((fs for fs in matches if len(fs) == largest),
                          key=lambda fs: tuple(sorted(positions[f] for f in fs)))
        parents: List[FeatureSet] = []
        # Only check combinations of 'size' features removed from C
        # We need all (k - size)-element subsets, i.e., keep 'size' features
        # More efficient: generate all size-sized subsets of features
        from itertools import combinations
        for combo in combinations(features, size):
            candidate = frozenset(combo)
            # Combinations of a feature set are already unique. A list scan
            # here makes wide full-feature nodes quadratic in parent count.
            if candidate in all_nodes:
                parents.append(candidate)
        if parents:
            return parents

    return []


def compute_shrinkage_variances(
    nodes: Dict[FeatureSet, NodeStats],
    *,
    kappa: float = 5.0,
    global_log_var: float = 0.0,
    index: CoverageIndex | None = None,
    reuse_sample_std: bool = False,
) -> None:
    """Compute shrinkage variance R_C for all nodes, mutating NodeStats in-place.

    Processes nodes in topological order (by feature count, ascending)
    so that parent variances are always computed before children.

    After this call, each ``NodeStats`` will have:
    - ``shrinkage_var``: the shrinkage-estimated log-space variance R_C
    - ``cold_start``: True if the node is top-level with a single sample

    Args:
        nodes: All context nodes with statistics (mutated in-place).
        kappa: Shrinkage strength hyperparameter (default 5).
        global_log_var: Global log-duration variance, used as fallback
            for cold-start top-level nodes.
    """
    # Sort nodes by feature count (ascending) for topological processing
    sorted_fs = sorted(nodes.keys(), key=lambda fs: len(fs))

    @lru_cache(maxsize=4096)
    def sample_variance(values):
        return stdev([math.log1p(v) for v in values]) ** 2

    for fs in sorted_fs:
        m = nodes[fs].count
        s2 = (nodes[fs].std_log ** 2 if reuse_sample_std
              else sample_variance(tuple(nodes[fs].durations))) if m >= 2 else 0.0

        # Find immediate parents that exist in the node structure
        parents = find_immediate_parents(fs, nodes, index=index)
        if index is not None:
            # Retain canonical keys, not freshly allocated combination sets.
            index.parent_sets[fs] = tuple(nodes[p].features for p in parents)

        if not parents:
            # ── Top-level node: no parents ──────────────────────────
            if m >= 2:
                nodes[fs].shrinkage_var = s2
                nodes[fs].cold_start = False
            else:
                # Single sample, no parents → cold start
                nodes[fs].shrinkage_var = global_log_var
                nodes[fs].cold_start = True
        else:
            # ── Has parents: compute v_parent(C) ─────────────────────
            parent_vars: List[float] = []
            for p in parents:
                p_var = nodes[p].shrinkage_var
                # Only use parents that are NOT cold-start
                if not nodes[p].cold_start:
                    parent_vars.append(p_var)

            if not parent_vars:
                # All parents are cold-start; fall back to own stats
                if m >= 2:
                    nodes[fs].shrinkage_var = s2
                    nodes[fs].cold_start = False
                else:
                    nodes[fs].shrinkage_var = global_log_var
                    nodes[fs].cold_start = True
            else:
                v_parent = median(parent_vars)

                if m == 1:
                    # Pure shrinkage to parent (formula collapses)
                    nodes[fs].shrinkage_var = v_parent
                else:
                    # Bayesian shrinkage estimator
                    numerator = (m - 1) * s2 + kappa * v_parent
                    denominator = (m - 1) + kappa
                    nodes[fs].shrinkage_var = numerator / denominator

                nodes[fs].cold_start = False


def get_effective_variance(
    stats: NodeStats,
    *,
    global_log_var: float = 0.0,
) -> float:
    """Get the effective log-space variance for a node.

    Returns ``shrinkage_var`` if computed, otherwise falls back
    to ``std_log ** 2`` or ``global_log_var``.

    Args:
        stats: Node statistics.
        global_log_var: Fallback global variance.

    Returns:
        Effective log-space variance.
    """
    if stats.shrinkage_var > 0:
        return stats.shrinkage_var
    if stats.count >= 2:
        return stats.std_log ** 2
    return global_log_var
