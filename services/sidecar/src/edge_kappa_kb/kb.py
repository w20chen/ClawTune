"""Independent distributional time KB with learned sharing on containment edges."""

from __future__ import annotations

import copy
import heapq
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from typing import Iterable

from kb_common import DEFAULT_MAX_OPTIONAL_FEATURES, validate_max_optional_features
from kb_common.feature_index import SupersetIndex

from .graph import FeatureGraph, bounded_nodes, node_id
from .types import (
    BucketPrediction, DurationDistribution, FeatureQuery, ParentEvidence, TimeOutcome, TrainingEvent, UpdateReport,
)


SCHEMA = "edge-kappa-kb.v1"
EPSILON = 0.01
SMOOTHING = 0.01
MAX_KAPPA = 100.0


@dataclass
class _Node:
    counts: list[int]
    # Coverage includes observations at all more-specific feature signatures.
    observations: set[str] = field(default_factory=set)
    # Derived from the observation index; never serialized independently.
    own_observations: set[str] = field(default_factory=set)


@dataclass
class _Edge:
    log_kappa: float
    update_count: int = 0
    last_update_id: str | None = None


def _project(values: list[float], lower: float, cap: float) -> tuple[list[float], bool]:
    """Euclidean projection onto x >= lower, sum(x) <= cap."""
    values = [max(lower, x) for x in values]
    if sum(values) <= cap:
        return values, False
    lo, hi = 0.0, max(values)
    for _ in range(80):
        mid = (lo + hi) / 2
        if sum(max(lower, x - mid) for x in values) > cap:
            lo = mid
        else:
            hi = mid
    return [max(lower, x - hi) for x in values], True


class EdgeKappaKB:
    def __init__(
        self, *, bucket_edges_ms: tuple[float, ...] = (100, 500, 2000, 10000),
        k0: float = 1.0, learning_rate: float = 0.1, learn_weights: bool = True,
        shared_child_weight: bool = False,
        max_optional_features: int = DEFAULT_MAX_OPTIONAL_FEATURES,
        replay_seed: int = 42,
        normalization_version: str = "command-features.v1",
    ) -> None:
        if (not bucket_edges_ms or any(not math.isfinite(v) or v <= 0 for v in bucket_edges_ms)
                or tuple(sorted(set(bucket_edges_ms))) != tuple(bucket_edges_ms)):
            raise ValueError("bucket edges must be positive, finite, strictly increasing")
        if not math.isfinite(k0) or k0 <= 0 or k0 > MAX_KAPPA:
            raise ValueError("k0 must be in (0, 100]")
        if not math.isfinite(learning_rate) or learning_rate < 0:
            raise ValueError("learning_rate must be nonnegative and finite")
        validate_max_optional_features(max_optional_features)
        self.bucket_edges_ms = tuple(bucket_edges_ms)
        self.k0 = k0
        self.learning_rate = learning_rate
        self.learn_weights = learn_weights
        self.shared_child_weight = shared_child_weight
        self.max_optional_features = max_optional_features
        self.replay_seed = replay_seed
        self.normalization_version = normalization_version
        self.graph = FeatureGraph(set())
        self.nodes: dict[str, _Node] = {}
        self.edges: dict[tuple[str, str], _Edge] = {}
        self.generation = 0
        self.graph_version = 1
        self.replay_cursor: str | None = None
        self._tokens: dict[str, dict] = {}
        self._pending: list[tuple[float, str, str, dict]] = []
        self._processed: set[str] = set()
        self._completed: set[str] = set()
        self._completion_rows: dict[str, dict] = {}
        self._observation_buckets: dict[str, int] = {}
        self._observation_features: dict[str, frozenset[str]] = {}
        self._observation_index = SupersetIndex()
        from .snapshot import RowTable
        self._snapshot_nodes = RowTable()
        self._snapshot_edges = RowTable()
        self._dirty_snapshot_nodes = set()
        self._dirty_snapshot_edges = set()
        self._snapshot_ready = False
        self._snapshot_new_nodes = []
        self._frozen = False

    @classmethod
    def fit(cls, training_events: Iterable[TrainingEvent], config: dict | None = None) -> EdgeKappaKB:
        events = list(training_events)
        kb = cls(**(config or {}))
        for event in events:
            kb._check_query(event.query)
        # The configured combination order is fixed, just as in LatticeKB.
        signatures: set[frozenset[str]] = set()
        for event in events:
            signatures.update(bounded_nodes(event.query, kb.max_optional_features))
        kb.graph = FeatureGraph(signatures)
        kb.nodes = {key: _Node([0] * kb.bucket_count) for key in kb.graph.signatures}
        kb._init_edges()
        # Task order is deterministic; timestamps from different tasks are not globally comparable.
        tasks: dict[str, list[TrainingEvent]] = {}
        for event in events:
            tasks.setdefault(event.outcome.task_id, []).append(event)
        import hashlib
        for task_id in sorted(tasks, key=lambda key: hashlib.sha256(
                f"{kb.replay_seed}\0{key}".encode()).hexdigest()):
            group = sorted(tasks[task_id], key=lambda item: (
                item.outcome.start_time, item.outcome.event_id))
            for event in group:
                kb.begin(event.query, event.outcome.event_id, event.outcome.start_time,
                         task_id=event.outcome.task_id, call_id=event.outcome.call_id,
                         clause_id=event.outcome.clause_id)
                kb.complete(event.outcome.event_id, event.outcome)
            kb.commit_before(float("inf"))
            kb.replay_cursor = task_id
        return kb

    @property
    def bucket_count(self) -> int:
        return len(self.bucket_edges_ms) + 1

    def _check_query(self, query: FeatureQuery) -> None:
        if query.normalization_version != self.normalization_version:
            raise ValueError("normalization version mismatch")

    def _init_edges(self) -> None:
        for child, parents in self.graph.parents.items():
            if parents:
                for parent in parents:
                    self.edges[(parent, child)] = _Edge(math.log(self.k0 / len(parents)))

    def _covered(self, features: frozenset[str]) -> set[str]:
        return self.graph.subsets(features)

    @staticmethod
    def _query_row(query: FeatureQuery) -> dict:
        return {"features": sorted(query.features),
                "core_features": sorted(query.core_features),
                "tool": query.tool,
                "normalization_version": query.normalization_version}

    def _ensure_full_node(self, query: FeatureQuery) -> None:
        self._check_query(query)
        if query.features in self.graph.by_features:
            return
        child = self.graph.add_full(query.features)
        self.nodes[child] = _Node([0] * self.bucket_count)
        self._dirty_snapshot_nodes.add(child)
        self._snapshot_new_nodes.append(child)
        for prior_id in self._observation_index.supersets(query.features):
            self.nodes[child].observations.add(prior_id)
            self.nodes[child].counts[self._observation_buckets[prior_id]] += 1
            if query.features == self._observation_features[prior_id]:
                self.nodes[child].own_observations.add(prior_id)
        parents = self.graph.parents[child]
        for parent in parents:
            self.edges[(parent, child)] = _Edge(math.log(self.k0 / len(parents)))
            self._dirty_snapshot_edges.add((parent, child))
        self.graph_version += 1

    def _predict(self, query: FeatureQuery, *, commit_projection: bool = False) -> BucketPrediction:
        self._check_query(query)
        exact = self.graph.by_features.get(query.features)
        child = exact or node_id(query.features)
        state = self.nodes.get(child, _Node([0] * self.bucket_count))
        parents = self.graph.parents[child] if exact else self.graph.query_parents(query.features)
        parents = tuple(parent for parent in parents
                        if query.core_features <= self.graph.signatures[parent])
        active: list[tuple[str, float, tuple[float, ...], set[str]]] = []
        for parent in parents:
            parent_state = self.nodes[parent]
            difference = parent_state.observations - state.observations
            d = len(difference)
            counts = [p - c for p, c in zip(parent_state.counts, state.counts)]
            if any(value < 0 for value in counts) or sum(counts) != d:
                raise ValueError("inconsistent parent/child coverage counts")
            if not d:
                continue
            kappa = math.exp(self.edges[(parent, child)].log_kappa) if exact else self.k0 / len(parents)
            distribution = tuple((n + EPSILON / self.bucket_count) / (d + EPSILON)
                                 for n in counts)
            active.append((parent, kappa, distribution, difference))
        if active and sum(item[1] for item in active) > MAX_KAPPA:
            values, _ = _project([item[1] for item in active],
                1e-6 / len(parents), MAX_KAPPA)
            active = [(parent, value, distribution, difference)
                      for (parent, _, distribution, difference), value in zip(active, values)]
            if commit_projection and exact:
                for parent, value, _, _ in active:
                    self.edges[(parent, child)].log_kappa = math.log(value)
                    self._dirty_snapshot_edges.add((parent, child))
        n = len(state.observations)
        if not n and not active:
            return BucketPrediction(None, None, child, bool(exact), 0, (), 0, 0.0,
                                    "no_edge_lattice_evidence")
        total_kappa = sum(item[1] for item in active)
        denominator = n + SMOOTHING + total_kappa
        probabilities = tuple((state.counts[b] + SMOOTHING / self.bucket_count +
            sum(kappa * distribution[b] for _, kappa, distribution, _ in active)) /
            denominator for b in range(self.bucket_count))
        union = set().union(*(item[3] for item in active)) if active else set()
        total_difference = sum(len(item[3]) for item in active)
        evidence = tuple(ParentEvidence(
            parent, tuple(sorted(self.graph.signatures[parent])),
            tuple(sorted(query.features - self.graph.signatures[parent])), len(difference),
            kappa, kappa / denominator, distribution,
        ) for parent, kappa, distribution, difference in active)
        return BucketPrediction(probabilities, max(range(self.bucket_count),
            key=lambda b: (probabilities[b], -b)), child, bool(exact), n,
            evidence, len(union),
            (total_difference - len(union)) / total_difference if total_difference else 0.0)

    def predict(self, query: FeatureQuery) -> BucketPrediction:
        return self._predict(query)

    def predict_duration(self, query: FeatureQuery) -> DurationDistribution:
        """Lift the same coverage/parent mixture to real duration atoms.

        Each child-coverage event has weight 1, including more-specific
        observations. Exact-own observations are recorded separately; they do
        not disable kappa sharing. Each parent-difference event has weight
        kappa/d. Overlapping parent evidence is one atom with summed weight.
        Censored and legacy bucket-only events are never assigned bucket midpoints.
        """
        prediction = self.predict(query)
        covered = self.nodes[prediction.node_id].observations if prediction.exact_match else set()
        weights = {event_id: 1.0 for event_id in covered}
        for evidence in prediction.evidence:
            for event_id in self.nodes[evidence.parent_id].observations - covered:
                weights[event_id] = weights.get(event_id, 0.0) + evidence.kappa / evidence.difference_count
        reason = prediction.unavailable_reason
        durations = {event_id: self._completion_rows.get(event_id, {}).get("duration_ms") for event_id in weights}
        if any(value is None for value in durations.values()):
            reason = "non_exact_duration_evidence"
        if reason:
            return DurationDistribution((), (), len(weights), prediction, reason)
        ordered = sorted(weights, key=lambda event_id: (durations[event_id], event_id))
        return DurationDistribution(tuple(durations[event_id] for event_id in ordered),
                                    tuple(weights[event_id] for event_id in ordered), len(weights), prediction)

    def discard_prediction(self, event_id: str) -> None:
        """Release an uncompleted prediction (e.g. an unexecuted shell branch)."""
        if self._frozen:
            raise ValueError("frozen KB cannot discard an event")
        if event_id in self._completed and event_id not in self._processed:
            raise ValueError("cannot discard a queued completion")
        self._tokens.pop(event_id, None)

    def completion_feedback(self, event_id: str) -> dict:
        """Copy the original token and outcome before commit consumes the token."""
        return copy.deepcopy({"token": self._tokens[event_id],
                              "outcome": self._completion_rows[event_id]})

    def cached_prediction_rejection(self, event_id: str) -> str | None:
        """Do not serve cross-entry predictions retained by older snapshots.

        Keep the original token for causal feedback; recomputing an in-flight
        prediction with newly available observations would change its history.
        """
        token = self._tokens.get(event_id)
        if token and token.get("prediction"):
            core = frozenset(token["query"]["core_features"])
            if any(not core <= frozenset(item["parent_features"])
                   for item in token["prediction"]["evidence"]):
                return "cached_prediction_crosses_execution_entry"
        return None

    def replay_feedback(self, feedback: dict, source: EdgeKappaKB) -> dict:
        """Apply a saved pre-action gradient to current weights, never source weights.

        Union historical parent relations across task branches. Existing edges
        retain their weights; missing edges start at the source graph's prior.
        Saved predictions still determine gradients, not the merged graph's
        current predictions. Return normalized feedback for durable replay.
        """
        if self._frozen:
            raise ValueError("frozen KB cannot replay feedback")
        if any(getattr(self, name) != getattr(source, name) for name in (
                "bucket_edges_ms", "k0", "learning_rate", "learn_weights", "shared_child_weight",
                "max_optional_features", "replay_seed",
                "normalization_version")):
            raise ValueError("incompatible feedback KB configuration")
        token = copy.deepcopy(feedback["token"])
        outcome = TimeOutcome(**feedback["outcome"])
        # Older tokens omit this field. Capture their source parents before
        # unioning graphs so shared-child updates do not touch another task's
        # extra edges. New tokens already preserve the prediction-time sets.
        token.setdefault("parent_sets", {
            child: list(source.graph.parents[child]) for child in token["covered"]
        })
        normalized = {"token": token, "outcome": asdict(outcome)}
        if outcome.event_id in self._completed:
            self.complete(outcome.event_id, outcome)
            return normalized

        imported: set[str] = set()

        def import_node(key: str) -> None:
            if key in imported:
                return
            parents = tuple(token["parent_sets"].get(key, source.graph.parents[key]))
            for parent in parents:
                import_node(parent)
            if key not in self.nodes:
                features = source.graph.signatures[key]
                query = FeatureQuery(features, features, token["query"]["tool"],
                                     self.normalization_version)
                self._ensure_full_node(query)
                # Auto-selected parents reflect merge order, not a task's
                # prediction history. Only retain actual historical edges.
                for parent in self.graph.parents[key]:
                    del self.edges[(parent, key)]
                    self._snapshot_edges.pop((parent, key), None)
                    self._dirty_snapshot_edges.discard((parent, key))
                self.graph.parents[key] = ()
            existing = self.graph.parents[key]
            combined = tuple(sorted(set(existing).union(parents)))
            for parent in parents:
                if parent not in existing:
                    self.edges[(parent, key)] = _Edge(math.log(self.k0 / len(parents)))
                    self._dirty_snapshot_edges.add((parent, key))
            if combined != existing:
                self.graph.parents[key] = combined
                self._dirty_snapshot_nodes.add(key)
                self.graph_version += 1
            imported.add(key)

        for key in token["covered"]:
            import_node(key)
        query_row = token["query"]
        query = FeatureQuery(frozenset(query_row["features"]),
                             frozenset(query_row["core_features"]), query_row["tool"],
                             query_row["normalization_version"])
        self._tokens[outcome.event_id] = token
        # A count-only token must retain that status during replay.
        self.complete(outcome.event_id, outcome,
                      query=query if token.get("weight_update_skipped") else None)
        self.commit_before(float("inf"))
        return normalized

    def begin(self, query: FeatureQuery, event_id: str, start_time: float, *,
              task_id: str | None = None, call_id: str | None = None,
              clause_id: str | None = None) -> tuple[BucketPrediction, str]:
        if self._frozen:
            raise ValueError("frozen KB cannot begin an event")
        if not event_id or not math.isfinite(start_time):
            raise ValueError("event ID and finite start time required")
        self.commit_before(start_time)
        if event_id in self._tokens:
            previous = self._tokens[event_id]
            if previous.get("weight_update_skipped"):
                raise ValueError("event already completed without a prediction token")
            if previous["start_time"] != start_time or previous["query"] != self._query_row(query) or any(
                    value is not None and previous.get(name) != value for name, value in (
                        ("task_id", task_id), ("call_id", call_id), ("clause_id", clause_id))):
                raise ValueError("event ID reused with different query or start time")
            if reason := self.cached_prediction_rejection(event_id):
                return BucketPrediction(None, None, node_id(query.features),
                                        previous["prediction"]["exact_match"],
                                        0, (), 0, 0.0, reason), event_id
            return self._prediction_from_token(self._tokens[event_id]), event_id
        if event_id in self._processed:
            raise ValueError("event already committed")
        self._ensure_full_node(query)
        prediction = self._predict(query, commit_projection=True)
        covered = self._covered(query.features)
        local = {}
        # Save all pre-execution predictions before any count changes.
        for child in sorted(covered):
            local_query = FeatureQuery(self.graph.signatures[child],
                query.core_features & self.graph.signatures[child],
                query.tool, query.normalization_version)
            local[child] = asdict(self._predict(local_query, commit_projection=True))
        self._tokens[event_id] = {"event_id": event_id, "start_time": start_time,
            "task_id": task_id, "call_id": call_id, "clause_id": clause_id,
            "query": self._query_row(query),
            "prediction": asdict(prediction), "local": local, "covered": sorted(covered),
            "parent_sets": {child: list(self.graph.parents[child]) for child in sorted(covered)},
            "generation": self.generation}
        return prediction, event_id

    @staticmethod
    def _prediction_from_token(token: dict) -> BucketPrediction:
        data = dict(token["prediction"])
        data["probabilities"] = (tuple(data["probabilities"])
                                 if data["probabilities"] is not None else None)
        data["evidence"] = tuple(ParentEvidence(
            item["parent_id"], tuple(item["parent_features"]),
            tuple(item["dropped_features"]), item["difference_count"],
            item["kappa"], item["mixture_share"], tuple(item["distribution"]),
        ) for item in data["evidence"])
        return BucketPrediction(**data)

    def complete(self, token_id: str, outcome: TimeOutcome, *,
                 query: FeatureQuery | None = None) -> None:
        """Record a completion; query permits count-only recovery without a valid token."""
        if self._frozen:
            raise ValueError("frozen KB cannot complete an event")
        event_id = outcome.event_id
        if event_id in self._completed:
            if self._completion_rows[event_id] != asdict(outcome):
                raise ValueError("completion retry differs from first outcome")
            token = self._tokens.get(event_id)
            if query is not None and token is not None and token["query"] != self._query_row(query):
                raise ValueError("completion retry differs from first query")
            return
        if not event_id or not outcome.task_id or not outcome.call_id or not outcome.clause_id:
            raise ValueError("outcome task/call/clause identity required")
        if (not math.isfinite(outcome.start_time) or not math.isfinite(outcome.end_time)
                or outcome.end_time < outcome.start_time):
            raise ValueError("invalid start or end time")
        self._bucket(outcome)
        if query is not None:
            self._check_query(query)
        token = self._tokens.get(event_id) if token_id == event_id else None
        matches = (token is not None and not token.get("weight_update_skipped")
                   and outcome.start_time == token["start_time"]
                   and all(token.get(name) is None or token[name] == getattr(outcome, name)
                           for name in ("task_id", "call_id", "clause_id"))
                   and (query is None or token["query"] == self._query_row(query)))
        if not matches:
            if query is None:
                raise ValueError("missing or mismatched prediction token; query required for count-only recovery")
            if event_id in self._processed:
                raise ValueError("event already committed")
            self._ensure_full_node(query)
            self._tokens[event_id] = {"event_id": event_id,
                "start_time": outcome.start_time,
                "task_id": outcome.task_id, "call_id": outcome.call_id,
                "clause_id": outcome.clause_id,
                "query": self._query_row(query), "prediction": None,
                "local": {}, "covered": sorted(self._covered(query.features)),
                "generation": self.generation, "weight_update_skipped": True}
        heapq.heappush(self._pending, (outcome.end_time, event_id, event_id, asdict(outcome)))
        self._completed.add(event_id)
        self._completion_rows[event_id] = asdict(outcome)

    def _bucket(self, outcome: TimeOutcome) -> int | None:
        if outcome.duration_ms is not None and outcome.censor_lower_ms is not None:
            raise ValueError("outcome cannot have both exact and censored duration")
        if not outcome.trusted or outcome.label_source not in {"clause", "single_clause_shell"}:
            return None
        if outcome.duration_ms is not None:
            if not math.isfinite(outcome.duration_ms) or outcome.duration_ms < 0:
                raise ValueError("invalid duration")
            return bisect_right(self.bucket_edges_ms, outcome.duration_ms)
        if outcome.censor_lower_ms is not None:
            if not math.isfinite(outcome.censor_lower_ms) or outcome.censor_lower_ms < 0:
                raise ValueError("invalid censor bound")
            if outcome.censor_lower_ms >= self.bucket_edges_ms[-1]:
                return self.bucket_count - 1
        return None

    def commit_before(self, query_start_time: float) -> UpdateReport:
        if self._frozen:
            raise ValueError("frozen KB cannot commit")
        if math.isnan(query_start_time) or query_start_time == float("-inf"):
            raise ValueError("invalid commit time")
        committed = updates = skipped = skipped_nodes = boundary_hits = 0
        while self._pending and self._pending[0][0] < query_start_time:
            _, _, token_id, row = heapq.heappop(self._pending)
            token = self._tokens.pop(token_id)
            outcome = TimeOutcome(**row)
            bucket = self._bucket(outcome)
            if bucket is None:
                skipped += 1
                self._processed.add(token_id)
                committed += 1
                continue
            covered_now = self.graph.subsets(frozenset(token["query"]["features"]))
            skipped_nodes += len(covered_now.difference(token["covered"]))
            if token.get("weight_update_skipped"):
                skipped += 1
                skipped_nodes += len(token["covered"])
            else:
                for child in token["covered"]:
                    saved = token["local"][child]
                    if saved["probabilities"] is None:
                        skipped_nodes += 1
                        continue
                    active = saved["evidence"]
                    core = (frozenset(token["query"]["core_features"])
                            & self.graph.signatures[child])
                    # Older snapshots/feedback can contain predictions made
                    # using forbidden parents. Their saved probabilities are
                    # not a valid basis for the constrained gradient. Keep
                    # their observations but skip those historical updates.
                    if any(not core <= self.graph.signatures[item["parent_id"]]
                           for item in active):
                        skipped_nodes += 1
                        continue
                    if not self.learn_weights or not active:
                        continue
                    denominator = saved["observation_count"] + SMOOTHING + sum(
                        item["kappa"] for item in active)
                    truth = saved["probabilities"][bucket]
                    gradients = {item["parent_id"]: item["kappa"] / denominator * (
                        1 - item["distribution"][bucket] / truth) for item in active}
                    if self.shared_child_weight:
                        parents = list(token.get("parent_sets", {}).get(child, self.graph.parents[child]))
                        parents = [parent for parent in parents
                                   if core <= self.graph.signatures[parent]]
                        shared_gradient = max(-1.0, min(1.0, sum(gradients.values())))
                        for parent in parents:
                            edge = self.edges[(parent, child)]
                            edge.log_kappa -= self.learning_rate * shared_gradient
                            edge.update_count += 1
                            edge.last_update_id = token_id
                        updates += 1
                    else:
                        parents = list(gradients)
                        for parent, gradient in gradients.items():
                            edge = self.edges[(parent, child)]
                            edge.log_kappa -= self.learning_rate * max(-1.0, min(1.0, gradient))
                            edge.update_count += 1
                            edge.last_update_id = token_id
                            updates += 1
                    lower = 1e-6 / len(self.graph.parents[child])
                    values, hit = _project([math.exp(self.edges[(parent, child)].log_kappa)
                        for parent in parents], lower, MAX_KAPPA)
                    boundary_hits += int(hit or any(value == lower for value in values))
                    for parent, value in zip(parents, values):
                        self.edges[(parent, child)].log_kappa = math.log(value)
                        self._dirty_snapshot_edges.add((parent, child))
            for child in covered_now:
                state = self.nodes[child]
                self._dirty_snapshot_nodes.add(child)
                if token_id not in state.observations:
                    state.observations.add(token_id)
                    state.counts[bucket] += 1
                if self.graph.signatures[child] == frozenset(token["query"]["features"]):
                    state.own_observations.add(token_id)
            self._observation_buckets[token_id] = bucket
            self._observation_features[token_id] = frozenset(token["query"]["features"])
            self._observation_index.add(token_id, self._observation_features[token_id])
            self._processed.add(token_id)
            committed += 1
            self.generation += 1
        return UpdateReport(committed, updates, skipped, skipped_nodes, boundary_hits)

    def freeze(self) -> None:
        if self._tokens or self._pending:
            raise ValueError("commit all pending events before freezing")
        self._frozen = True

    def fork_for_update(self) -> EdgeKappaKB:
        successor = copy.deepcopy(self)
        successor._frozen = False
        return successor

    def to_snapshot(self) -> dict:
        return {"schema": SCHEMA, "graph_version": self.graph_version,
            "normalization_version": self.normalization_version,
            "config": {"bucket_edges_ms": list(self.bucket_edges_ms), "k0": self.k0,
                "learning_rate": self.learning_rate, "learn_weights": self.learn_weights,
                "shared_child_weight": self.shared_child_weight,
                "max_optional_features": self.max_optional_features,
                "replay_seed": self.replay_seed},
            "generation": self.generation, "replay_cursor": self.replay_cursor,
            "frozen": self._frozen,
            "nodes": [{"id": key, "features": sorted(features),
                "parents": list(self.graph.parents[key]),
                "counts": list(self.nodes[key].counts),
                "observations": sorted(self.nodes[key].observations)}
                for key, features in self.graph.signatures.items()],
            "edges": [{"parent_id": parent, "child_id": child,
                "dropped_features": sorted(self.graph.signatures[child] - self.graph.signatures[parent]),
                **asdict(edge)} for (parent, child), edge in sorted(self.edges.items())],
            "tokens": copy.deepcopy(self._tokens),
            "pending": copy.deepcopy([list(item) for item in self._pending]),
            "processed_event_ids": sorted(self._processed),
            "completed_event_ids": sorted(self._completed),
            "completion_rows": copy.deepcopy(self._completion_rows),
            "observations": [{"event_id": key, "features": sorted(self._observation_features[key]),
                "bucket": bucket} for key, bucket in sorted(self._observation_buckets.items())]}

    def _capture_snapshot(self):
        """Return an internal read-only payload factory for this exact revision.

        The owner holds its lock here. Published token/outcome dictionaries are
        append-or-replace, never edited in place; mutable counts and coverage
        sets are copied only for changed nodes. Sorting and deep formatting can
        then run outside the lock, even while new observations are committed.
        Public to_snapshot retains its independently mutable-copy contract.
        """
        from .snapshot import NodeFrame, EdgeFrame, SnapshotPayload
        node_keys = self._dirty_snapshot_nodes if self._snapshot_ready else self.graph.signatures
        edge_keys = self._dirty_snapshot_edges if self._snapshot_ready else sorted(self.edges)
        # Preserve insertion order when several new nodes appeared since the
        # previous capture. Existing rows retain their chunk positions.
        if self._snapshot_ready:
            added = node_keys - self._snapshot_nodes.positions.keys()
            if added:
                node_keys = [key for key in node_keys if key not in added] + [
                    key for key in self._snapshot_new_nodes if key in added]
        for key in node_keys:
            node = self.nodes[key]
            self._snapshot_nodes[key] = NodeFrame(key, self.graph.signatures[key],
                self.graph.parents[key], tuple(node.counts), frozenset(node.observations))
        for parent, child in edge_keys:
            edge = self.edges[(parent, child)]
            self._snapshot_edges[(parent, child)] = EdgeFrame((parent, child), {
                'parent_id': parent, 'child_id': child,
                'dropped_features': sorted(self.graph.signatures[child] - self.graph.signatures[parent]),
                'log_kappa': edge.log_kappa, 'update_count': edge.update_count,
                'last_update_id': edge.last_update_id})
        self._dirty_snapshot_nodes.clear()
        self._snapshot_new_nodes.clear()
        self._dirty_snapshot_edges.clear()
        self._snapshot_ready = True
        node_chunks = self._snapshot_nodes.capture()
        edge_chunks = self._snapshot_edges.capture()
        tokens, pending = dict(self._tokens), tuple(self._pending)
        completed, processed = tuple(self._completed), tuple(self._processed)
        outcomes = dict(self._completion_rows)
        observations = tuple((key, self._observation_features[key], bucket)
                             for key, bucket in self._observation_buckets.items())
        metadata = {'schema': SCHEMA, 'graph_version': self.graph_version,
            'normalization_version': self.normalization_version,
            'config': {'bucket_edges_ms': list(self.bucket_edges_ms), 'k0': self.k0,
                'learning_rate': self.learning_rate, 'learn_weights': self.learn_weights,
                'shared_child_weight': self.shared_child_weight,
                'max_optional_features': self.max_optional_features, 'replay_seed': self.replay_seed},
            'generation': self.generation, 'replay_cursor': self.replay_cursor, 'frozen': self._frozen}
        def materialize():
            nodes = [frame for chunk in node_chunks for frame in chunk if frame is not None]
            edges = sorted((frame for chunk in edge_chunks for frame in chunk if frame is not None),
                           key=lambda frame: frame.key)
            return SnapshotPayload({**metadata, 'nodes': [frame.row() for frame in nodes],
                'edges': [frame.row() for frame in edges],
                'tokens': copy.deepcopy(tokens), 'pending': copy.deepcopy([list(item) for item in pending]),
                'processed_event_ids': sorted(processed), 'completed_event_ids': sorted(completed),
                'completion_rows': copy.deepcopy(outcomes),
                'observations': [{'event_id': key, 'features': sorted(features), 'bucket': bucket}
                                 for key, features, bucket in sorted(observations)]}, nodes, edges)
        return materialize

    @classmethod
    def from_snapshot(cls, obj: dict) -> EdgeKappaKB:
        if obj.get("schema") != SCHEMA or not isinstance(obj.get("graph_version"), int) or obj["graph_version"] < 1:
            raise ValueError("unsupported edge kappa snapshot version")
        config = dict(obj["config"])
        # Legacy capacity guards are retired. Keep the stored effective order,
        # graph, historical parent relations and learned weights unchanged.
        for key in ("max_covered_nodes", "max_graph_nodes"):
            if key in config:
                value = config.pop(key)
                if type(value) is not int or value < 1:
                    raise ValueError(f"invalid legacy {key}")
        kb = cls(**config, normalization_version=obj["normalization_version"])
        signatures = {frozenset(row["features"]) for row in obj["nodes"]}
        kb.graph = FeatureGraph(signatures, build_parents=False)
        # A growing online graph retains the stored historical parent relation.
        kb.graph.parents = {key: () for key in kb.graph.signatures}
        if {row["id"] for row in obj["nodes"]} != set(kb.graph.signatures):
            raise ValueError("invalid node IDs")
        kb.nodes = {row["id"]: _Node(list(row["counts"]), set(row["observations"]))
                    for row in obj["nodes"]}
        if any(len(node.counts) != kb.bucket_count or sum(node.counts) != len(node.observations)
               for node in kb.nodes.values()):
            raise ValueError("invalid node counts")
        stored_parents: dict[str, list[str]] = {key: [] for key in kb.graph.signatures}
        for row in obj["edges"]:
            key = (row["parent_id"], row["child_id"])
            if key[0] not in kb.nodes or key[1] not in kb.nodes or not (
                kb.graph.signatures[key[0]] < kb.graph.signatures[key[1]]) or row["dropped_features"] != sorted(
                kb.graph.signatures[key[1]] - kb.graph.signatures[key[0]]):
                raise ValueError("invalid edge")
            if not math.isfinite(row["log_kappa"]) or row["log_kappa"] > math.log(MAX_KAPPA):
                raise ValueError("invalid edge weight")
            stored_parents[key[1]].append(key[0])
            kb.edges[key] = _Edge(row["log_kappa"], row["update_count"], row["last_update_id"])
        kb.graph.parents = {key: tuple(sorted(parents)) for key, parents in stored_parents.items()}
        for row in obj["nodes"]:
            if "parents" in row and tuple(row["parents"]) != kb.graph.parents[row["id"]]:
                raise ValueError("snapshot edge/node parent mismatch")
        if len(obj["edges"]) != len(kb.edges):
            raise ValueError("duplicate edge")
        if obj["graph_version"] == 1 and kb.graph.parents != kb.graph.canonical_parents():
            raise ValueError("missing or incorrect edge")
        for (parent, child) in kb.edges:
            if not kb.nodes[child].observations <= kb.nodes[parent].observations:
                raise ValueError("inconsistent coverage observation sets")
            if any(c > p for p, c in zip(kb.nodes[parent].counts, kb.nodes[child].counts)):
                raise ValueError("inconsistent coverage bucket counts")
        kb.graph_version = obj["graph_version"]
        kb.generation = obj["generation"]
        kb.replay_cursor = obj["replay_cursor"]
        kb._frozen = obj["frozen"]
        kb._tokens = copy.deepcopy(obj["tokens"])
        kb._pending = [tuple(item) for item in obj["pending"]]
        heapq.heapify(kb._pending)
        kb._processed = set(obj["processed_event_ids"])
        kb._completed = set(obj["completed_event_ids"])
        kb._completion_rows = copy.deepcopy(obj["completion_rows"])
        kb._observation_buckets = {row["event_id"]: row["bucket"] for row in obj["observations"]}
        kb._observation_features = {row["event_id"]: frozenset(row["features"])
                                    for row in obj["observations"]}
        kb._observation_index = SupersetIndex(kb._observation_features.items())
        for event_id in kb._observation_buckets:
            duration = kb._completion_rows.get(event_id, {}).get("duration_ms")
            if duration is not None and (not math.isfinite(duration) or duration < 0
                    or bisect_right(kb.bucket_edges_ms, duration) != kb._observation_buckets[event_id]):
                raise ValueError("invalid observation duration")
        if len(kb._observation_buckets) != len(obj["observations"]) or any(
            not 0 <= bucket < kb.bucket_count for bucket in kb._observation_buckets.values()):
            raise ValueError("invalid observation index")
        expected_ids: dict[str, set[str]] = {key: set() for key in kb.nodes}
        expected_counts = {key: [0] * kb.bucket_count for key in kb.nodes}
        for event_id, features in kb._observation_features.items():
            exact = kb.graph.by_features.get(features)
            if exact is not None:
                kb.nodes[exact].own_observations.add(event_id)
            for key in kb.graph.subsets(features):
                expected_ids[key].add(event_id)
                expected_counts[key][kb._observation_buckets[event_id]] += 1
        for key, state in kb.nodes.items():
            if state.observations != expected_ids[key]:
                raise ValueError("inconsistent node observation coverage")
            if state.counts != expected_counts[key]:
                raise ValueError("inconsistent node bucket counts")
        return kb
