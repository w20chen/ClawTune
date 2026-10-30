"""Query constraints and exact-observation preference shared by Lattice views."""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import field, dataclass, replace
from typing import Any

from command_parser.models import CommandFeatures
from tool_time._lattice_vendor.features import CoverageIndex
from tool_time._lattice_vendor.nodes import _loo_mse_log
from tool_time._lattice_vendor.normalize import FeatureSet
from tool_time._lattice_vendor.schemas import NodeStats


@dataclass
class LatticeQueryCache:
    """Scratch data for one prediction, never persisted on a live KB.

    Published node maps are immutable. Identity plus query constraints separates
    targets, measurement partitions and generations even if this scratch object
    is passed through multiple KB adapters. Retain owners to prevent id reuse.
    """

    candidates: dict = field(default_factory=dict)
    selections: dict = field(default_factory=dict)
    summaries: dict = field(default_factory=dict)
    owners: dict[int, Any] = field(default_factory=dict)

    def query(self, nodes, query, *, global_log_std, index=None):
        key = (id(nodes), id(index), query.features, query.core, global_log_std)
        if key not in self.candidates:
            self.owners[id(nodes)] = nodes
            self.owners[id(index)] = index
            self.candidates[key] = query_nodes(nodes, query, global_log_std=global_log_std, index=index)
        return self.candidates[key]


def query_nodes(
    nodes: Mapping[FeatureSet, NodeStats], query: CommandFeatures, *, global_log_std: float,
    index: CoverageIndex | None = None,
) -> dict[FeatureSet, NodeStats]:
    """Keep execution identity; prefer own samples only for a full-feature match.

    Coverage statistics stay intact for generalized queries. An exact match
    with own observations returns a separate statistical view, so neither
    queries nor online publication mutate the shared coverage nodes.
    """
    # Own-sample prediction replaces the inherited variance below, so resolving
    # that parent DAG would compute values this exact-match path never uses.
    exact = getattr(nodes, 'raw_get', nodes.get)(query.features)
    if exact is not None and exact.own_durations:
        values = exact.own_durations
        logs = [math.log1p(value) for value in values]
        std = statistics.stdev(logs) if len(logs) > 1 else global_log_std
        own = replace(
            exact, durations=values, count=len(values),
            mean_s=statistics.mean(values), median_s=statistics.median(values),
            geometric_mean_s=(math.exp(statistics.mean(math.log(v) for v in values))
                              if all(v > 0 for v in values) else 0.0),
            mean_log=statistics.mean(logs), std_log=std,
            stderr_log=std / math.sqrt(len(values)),
            loo_mse_log=_loo_mse_log(logs, estimator="median"),
            signature_count=1, signature_log_mean_var=0.0, loso_risk=0.0,
            shrinkage_var=std ** 2,
        )
        return {query.features: own}
    if index is not None:
        # CoverageIndex yields the original mapping order, including ties.
        return {fs: nodes[fs] for fs in index.subsets(query.features) if query.core <= fs}
    return {fs: node for fs, node in nodes.items() if query.core <= fs <= query.features}
