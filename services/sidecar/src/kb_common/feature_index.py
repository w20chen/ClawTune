"""Exact containment indexes. They do not choose or create knowledge nodes."""
from __future__ import annotations


class SubsetIndex:
    """A feature trie returning subsets in insertion order."""

    def __init__(self, nodes=()):
        self._trie = {}
        self._size = 0
        # Optional, generation-local memoization of a caller's parent policy.
        self.parent_sets = {}
        for node in nodes:
            self.add(node)

    def add(self, node):
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
        terminal = (self._size, node)
        self._size += 1
        if not ordered:
            branch[None] = terminal
        elif isinstance(branch.get(ordered[-1]), dict):
            branch[ordered[-1]][None] = terminal
        else:
            branch[ordered[-1]] = terminal

    def subsets(self, features):
        allowed = set(features)
        stack = [self._trie]
        matches = []
        while stack:
            branch = stack.pop()
            if len(branch) > len(allowed) + 1:
                if None in branch:
                    matches.append(branch[None])
                children = (branch[f] for f in allowed if f in branch)
            else:
                if None in branch:
                    matches.append(branch[None])
                children = (child for f, child in branch.items() if f is not None and f in allowed)
            for child in children:
                if isinstance(child, tuple):
                    matches.append(child)
                else:
                    stack.append(child)
        for _, node in sorted(matches):
            yield node


class SupersetIndex:
    """Intersect postings to find records containing every requested feature."""

    def __init__(self, entries=()):
        self.features = {}
        self.postings = {}
        for key, features in entries:
            self.add(key, features)

    def add(self, key, features):
        self.features[key] = features
        for feature in features:
            self.postings.setdefault(feature, set()).add(key)

    def supersets(self, features):
        if not features:
            return set(self.features)
        postings = sorted((self.postings.get(f, ()) for f in features), key=len)
        result = set(postings[0])
        for posting in postings[1:]:
            result.intersection_update(posting)
            if not result:
                break
        return result

    def contains_superset(self, features):
        if not features:
            return bool(self.features)
        smallest = min((self.postings.get(f, ()) for f in features), key=len)
        return any(features <= self.features[key] for key in smallest)
