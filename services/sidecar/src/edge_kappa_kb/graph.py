"""Bounded feature graph and subset lookup."""

from __future__ import annotations

import hashlib
import json
from itertools import combinations

from .types import FeatureQuery


def node_id(features: frozenset[str]) -> str:
    payload = json.dumps(sorted(features), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bounded_nodes(query: FeatureQuery, max_optional_features: int = 6) -> set[frozenset[str]]:
    optional = sorted(query.features - query.core_features)
    result = {query.features}
    for size in range(min(len(optional), max_optional_features) + 1):
        result.update(frozenset(query.core_features.union(part)) for part in combinations(optional, size))
    return result


class FeatureGraph:
    def __init__(self, signatures: set[frozenset[str]]) -> None:
        self.signatures = dict(sorted((node_id(f), f) for f in signatures))
        self.by_features = {f: key for key, f in self.signatures.items()}
        self.index: dict[str, set[str]] = {}
        for key, features in self.signatures.items():
            for feature in features:
                self.index.setdefault(feature, set()).add(key)
        self._subset_cache: dict[frozenset[str], frozenset[str]] = {}
        self.parents: dict[str, tuple[str, ...]] = {}
        for child_id, child in self.signatures.items():
            candidates = self.subsets(child) - {child_id}
            tool = next(f for f in child if f.startswith("tool="))
            candidates = {key for key in candidates if tool in self.signatures[key]}
            self.parents[child_id] = self._maximal(candidates)

    def subsets(self, features: frozenset[str]) -> set[str]:
        if not features:
            return set()
        cached = self._subset_cache.get(features)
        if cached is not None:
            return set(cached)
        # Every node has one tool feature. Scanning that broad posting for each
        # lookup would make graph construction quadratic in a common tool.
        optional = [feature for feature in features if not feature.startswith("tool=")]
        candidates = set().union(*(self.index.get(feature, set()) for feature in optional))
        tool = next((feature for feature in features if feature.startswith("tool=")), None)
        root = self.by_features.get(frozenset({tool})) if tool else None
        if root:
            candidates.add(root)
        matching = frozenset(key for key in candidates if self.signatures[key] <= features)
        self._subset_cache[features] = matching
        return set(matching)

    def _maximal(self, candidates: set[str]) -> tuple[str, ...]:
        return tuple(sorted(key for key in candidates if not any(
            self.signatures[key] < self.signatures[other] for other in candidates
        )))

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
        for feature in features:
            self.index.setdefault(feature, set()).add(key)
        self._subset_cache.clear()
        return key
