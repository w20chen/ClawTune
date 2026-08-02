"""Context node generation from command feature sets.

Generates historical context nodes from observed commands. Each node
is a subset of features that retains the core identity (tool, target,
repo, env).

Two generation modes:
- ``exhaustive``: Generate all optional-feature subsets (for validation).
- ``bounded``: Limit subsets to ``max_optional_features`` optional features.
  Always keeps the full exact node regardless of the limit.
"""

from __future__ import annotations

from itertools import combinations
from typing import FrozenSet, Iterable, List

from tool_time._lattice_vendor.normalize import FeatureSet


def generate_context_nodes(
    features: FeatureSet,
    core: FeatureSet,
    *,
    mode: str = "bounded",
    max_optional_features: int = 6,
    always_keep_exact: bool = True,
) -> List[FeatureSet]:
    """Generate context nodes from a command's feature set.

    Args:
        features: The complete feature set (core + optional).
        core: Core identity features (tool, target, repo, env_id).
        mode: ``"exhaustive"`` or ``"bounded"``.
        max_optional_features: Max optional features in partial nodes
            (bounded mode only).
        always_keep_exact: Always include the full exact node.

    Returns:
        A list of frozenset nodes. The exact node is always the last
        element when ``always_keep_exact=True``.
    """
    optional = sorted(features - core)

    if mode == "exhaustive":
        nodes: List[FeatureSet] = []
        for size in range(len(optional) + 1):
            for subset in combinations(optional, size):
                nodes.append(frozenset(set(core).union(subset)))
        return nodes

    # Bounded mode
    nodes = []
    max_opt = min(max_optional_features, len(optional))

    for size in range(max_opt + 1):
        for subset in combinations(optional, size):
            nodes.append(frozenset(set(core).union(subset)))

    # Always include the full node
    if always_keep_exact and features not in nodes:
        nodes.append(features)

    return nodes


def estimate_node_count(
    num_optional: int,
    mode: str = "bounded",
    max_optional_features: int = 6,
) -> int:
    """Estimate how many nodes would be generated for a command.

    Args:
        num_optional: Number of optional features.
        mode: Generation mode.
        max_optional_features: Cap for bounded mode.

    Returns:
        Estimated node count.
    """
    if mode == "exhaustive":
        return 2 ** num_optional

    total = 0
    limit = min(max_optional_features, num_optional)
    for k in range(limit + 1):
        # comb(n, k)
        import math
        total += math.comb(num_optional, k)
    # +1 for the full node if not already counted
    if num_optional > max_optional_features:
        total += 1
    return total
