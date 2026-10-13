"""Query constraints and exact-observation preference shared by Lattice views."""
from __future__ import annotations

import math
import statistics
from dataclasses import replace

from command_parser.models import CommandFeatures
from tool_time._lattice_vendor.nodes import _loo_mse_log
from tool_time._lattice_vendor.normalize import FeatureSet
from tool_time._lattice_vendor.schemas import NodeStats


def query_nodes(
    nodes: dict[FeatureSet, NodeStats], query: CommandFeatures, *, global_log_std: float,
) -> dict[FeatureSet, NodeStats]:
    """Keep execution identity; prefer own samples only for a full-feature match.

    Coverage statistics stay intact for generalized queries. An exact match
    with own observations returns a separate statistical view, so neither
    queries nor online publication mutate the shared coverage nodes.
    """
    exact = nodes.get(query.features)
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
    return {fs: node for fs, node in nodes.items() if query.core <= fs <= query.features}
