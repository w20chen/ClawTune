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

from kb_common import DEFAULT_MAX_OPTIONAL_FEATURES

from itertools import combinations
from typing import FrozenSet, Iterable, List

from tool_time._lattice_vendor.normalize import FeatureSet


class CoverageIndex:
    """Find generated nodes contained in an observation's complete features."""

    def __init__(self, nodes: Iterable[FeatureSet]) -> None:
        self._trie: dict = {}
        self.parent_sets: dict = {}
        self.generation_order: dict = {}
        self.first_cover: dict = {}
        self._order = None
        for position, node in enumerate(nodes):
            branch = self._trie
            ordered = sorted(node)
            for feature in ordered[:-1]:
                child = branch.get(feature)
                if child is None:
                    child = {}
                    branch[feature] = child
                elif isinstance(child, tuple):
                    child = {None: child}
                    branch[feature] = child
                branch = child
            terminal = (position, node)
            if not ordered:
                branch[None] = terminal
            elif isinstance(branch.get(ordered[-1]), dict):
                branch[ordered[-1]][None] = terminal
            else:
                # Most bounded-lattice nodes are leaves. Store their terminal
                # directly, without a one-item dictionary for every leaf.
                branch[ordered[-1]] = terminal

    def subsets(self, features: FeatureSet) -> Iterable[FeatureSet]:
        allowed = set(features)
        stack = [self._trie]
        matches: list[tuple[int, FeatureSet]] = []
        while stack:
            branch = stack.pop()
            if len(branch) > len(allowed) + 1:
                # A wide branch can contain thousands of unrelated arguments.
                # Probe query features instead of scanning every child.
                if None in branch:
                    matches.append(branch[None])
                for feature in allowed:
                    if feature in branch:
                        child = branch[feature]
                        if isinstance(child, tuple):
                            matches.append(child)
                        else:
                            stack.append(child)
            else:
                for feature, child in branch.items():
                    if feature is None:
                        matches.append(child)
                    elif feature in allowed:
                        if isinstance(child, tuple):
                            matches.append(child)
                        else:
                            stack.append(child)
        if self._order is None:
            matches.sort()
        else:
            first, generation = self._order
            matches.sort(key=lambda item: (first[item[1]], generation[item[1]]))
        for _, node in matches:
            yield node

    def fork_added(self, nodes, first_cover, generation_order):
        """Copy only inserted trie paths; published generations stay intact."""
        result = object.__new__(type(self))
        result.__dict__ = self.__dict__.copy()
        result._trie = self._trie.copy()
        result._order = (first_cover, generation_order)
        owned = {id(result._trie)}
        for node in nodes:
            branch = result._trie
            ordered = sorted(node)
            for feature in ordered[:-1]:
                child = branch.get(feature)
                if child is None:
                    child = {}
                elif isinstance(child, tuple):
                    child = {None: child}
                elif id(child) not in owned:
                    child = child.copy()
                owned.add(id(child))
                branch[feature] = child
                branch = child
            terminal = (generation_order[node], node)
            if not ordered:
                branch[None] = terminal
            elif isinstance(branch.get(ordered[-1]), dict):
                child = branch[ordered[-1]]
                if id(child) not in owned:
                    child = child.copy()
                    owned.add(id(child))
                    branch[ordered[-1]] = child
                child[None] = terminal
            else:
                branch[ordered[-1]] = terminal
        return result


def generate_context_nodes(
    features: FeatureSet,
    core: FeatureSet,
    *,
    mode: str = "bounded",
    max_optional_features: int = DEFAULT_MAX_OPTIONAL_FEATURES,
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
    max_optional_features: int = DEFAULT_MAX_OPTIONAL_FEATURES,
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
