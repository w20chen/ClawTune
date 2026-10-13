"""Bounded feature graph and subset lookup."""

from __future__ import annotations

import hashlib
import json
from itertools import combinations

from .types import FeatureQuery
from kb_common import DEFAULT_MAX_OPTIONAL_FEATURES
from kb_common.feature_index import SubsetIndex, SupersetIndex


def node_id(features: frozenset[str]) -> str:
    payload = json.dumps(sorted(features), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bounded_nodes(query: FeatureQuery, max_optional_features: int = DEFAULT_MAX_OPTIONAL_FEATURES) -> set[frozenset[str]]:
    optional = sorted(query.features - query.core_features)
    result = {query.features}
    for size in range(min(len(optional), max_optional_features) + 1):
        result.update(frozenset(query.core_features.union(part)) for part in combinations(optional, size))
    return result


class FeatureGraph:
    def __init__(self, signatures: set[frozenset[str]], *, build_parents: bool = True) -> None:
        self.signatures = dict(sorted((node_id(f), f) for f in signatures))
        self.by_features = {f: key for key, f in self.signatures.items()}
        self._coverage = SubsetIndex(self.by_features)
        self.index: dict[str, set[str]] = {}
        for key, features in self.signatures.items():
            for feature in features:
                self.index.setdefault(feature, set()).add(key)
        self._subset_cache: dict[frozenset[str], frozenset[str]] = {}
        self.parents: dict[str, tuple[str, ...]] = {}
        if build_parents:
            self.parents = self.canonical_parents()
        else:
            # The full constructor also rejects nodes without a tool feature.
            # Loading stored edges must retain that validation.
            for features in self.signatures.values():
                next(f for f in features if f.startswith("tool="))

    def canonical_parents(self) -> dict[str, tuple[str, ...]]:
        parents = {}
        for child_id, child in self.signatures.items():
            # Construction visits every node once; retaining all those
            # coverage sets would turn each later cache update into a graph scan.
            candidates = {self.by_features[fs] for fs in self._coverage.subsets(child)} - {child_id}
            tool = next(f for f in child if f.startswith("tool="))
            candidates = {key for key in candidates if tool in self.signatures[key]}
            parents[child_id] = self._maximal(candidates)
        return parents

    def subsets(self, features: frozenset[str]) -> set[str]:
        if not features:
            return set()
        cached = self._subset_cache.get(features)
        if cached is not None:
            return set(cached)
        matching = frozenset(self.by_features[fs] for fs in self._coverage.subsets(features))
        # This is only a disposable lookup cache, not retained knowledge.
        if len(self._subset_cache) >= 4096:
            self._subset_cache.pop(next(iter(self._subset_cache)))
        self._subset_cache[features] = matching
        return set(matching)

    def _maximal(self, candidates: set[str]) -> tuple[str, ...]:
        # Equal-size nodes cannot strictly contain one another. Keep the
        # inclusion-maximal antichain, including smaller incomparable nodes.
        # Index only completed larger layers; this is NOT a largest-layer rule.
        from itertools import groupby
        maximal = []
        larger = SupersetIndex()
        ordered = sorted(candidates, key=lambda key: -len(self.signatures[key]))
        for _, layer in groupby(ordered, key=lambda key: len(self.signatures[key])):
            selected = [key for key in layer if not larger.contains_superset(self.signatures[key])]
            maximal.extend(selected)
            for key in selected:
                larger.add(key, self.signatures[key])
        return tuple(sorted(maximal))

    def query_parents(self, features: frozenset[str]) -> tuple[str, ...]:
        candidates = self.subsets(features)
        candidates.discard(self.by_features.get(features, ""))
        return self._maximal(candidates)

    def add_full(self, features: frozenset[str]) -> str:
        """Add only an unseen exact node; existing historical edges stay intact."""
        existing = self.by_features.get(features)
        if existing:
            return existing
        parents = self.query_parents(features)
        key = node_id(features)
        self.signatures[key] = features
        self.by_features[features] = key
        self.parents[key] = parents
        self._coverage.add(features)
        for feature in features:
            self.index.setdefault(feature, set()).add(key)
        # Only cached queries containing the new node gain a match.
        for query, matching in list(self._subset_cache.items()):
            if features <= query:
                self._subset_cache[query] = matching | {key}
        return key
