"""Runtime lifecycle and weighted-evidence adapter for the fourth KB.

The owner serializes this adapter with the other KBs. Only admitted clause
telemetry supplies training labels; tool-hook durations are not clause labels.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import shlex
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from edge_kappa_kb import EdgeKappaKB, TimeOutcome, TrainingEvent
from clawtune_sidecar.contracts.load_prediction import TARGET_UNITS
from clawtune_sidecar.identity import correlation_key
from tool_resource.commands import normalized_observation
from tool_resource.features import shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import ClauseObservation, is_pipeline_dependent_consumer
from tool_time.edge_kappa_adapter import shell_query


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def call_key(request: Any) -> str:
    return _digest(correlation_key(request))


def training_event(observation: ClauseObservation) -> TrainingEvent | None:
    row = normalized_observation(observation)
    if (row.latency_ms is None or not math.isfinite(row.latency_ms) or row.latency_ms < 0
            or not math.isfinite(row.ts_start) or not math.isfinite(row.ts_end)
            or row.ts_end < row.ts_start or not row.argv
            or is_pipeline_dependent_consumer(row) or row.in_loop or row.in_subst
            or not shell_bin_requires_exec_evidence(row.bin, row.argv[0])):
        return None
    query = shell_query(shlex.join(row.argv), repo=row.repo)
    identity = _digest([row.repo, row.argv, row.ts_start, row.ts_end, row.latency_ms])
    return TrainingEvent(query, TimeOutcome(identity, row.ts_start, row.ts_end,
        row.repo or "repository-neutral", identity, "0", duration_ms=row.latency_ms))


class EdgeKappaRuntime:
    def __init__(self, kb: EdgeKappaKB, *, frozen: bool = False, completed_calls: Iterable[str] = ()) -> None:
        self.kb = kb
        self.frozen = frozen
        self._completed_calls = set(completed_calls)
        self._observation_ids = set(kb.to_snapshot()["completed_event_ids"])
        self._calls: dict[str, dict[str, dict[str, Any]]] = {}
        self._feedback: dict[str, dict] = {}
        for event_id, token in kb.to_snapshot()["tokens"].items():
            if not token.get("weight_update_skipped") and token["call_id"] not in self._completed_calls:
                self._calls.setdefault(token["call_id"], {})[event_id] = token

    @classmethod
    def fit(cls, observations: Iterable[ClauseObservation], edges: Sequence[float], *,
            frozen: bool = False) -> EdgeKappaRuntime:
        events = {}
        for row in observations:
            event = training_event(row)
            if event is not None:
                events[event.outcome.event_id] = event
        kb = EdgeKappaKB.fit(events.values(), {"bucket_edges_ms": tuple(edges)})
        if frozen:
            kb.freeze()
        return cls(kb, frozen=frozen)

    @classmethod
    def from_snapshot(cls, obj: dict, *, frozen: bool = False) -> EdgeKappaRuntime:
        kb = EdgeKappaKB.from_snapshot(obj)
        if frozen:
            kb.freeze()
        elif obj["frozen"]:
            kb = kb.fork_for_update()
        result = cls(kb, frozen=frozen, completed_calls=obj.get("runtime", {}).get("completed_calls", ()))
        result._observation_ids.update(obj.get("runtime", {}).get("observation_ids", ()))
        result._feedback = copy.deepcopy(obj.get("runtime", {}).get("feedback", {}))
        for call_id, tokens in obj.get("runtime", {}).get("calls", {}).items():
            if call_id in result._calls:
                result._calls[call_id].update(copy.deepcopy(tokens))
        return result

    def to_snapshot(self) -> dict:
        obj = self.kb.to_snapshot()
        obj["runtime"] = {"completed_calls": sorted(self._completed_calls),
                          "observation_ids": sorted(self._observation_ids), "calls": copy.deepcopy(self._calls),
                          "feedback": copy.deepcopy(self._feedback)}
        return obj

    def predict_load_samples(self, repo: str, clauses: Sequence[Mapping[str, Any]], ts_start: float,
                             *, call_id: str | None = None) -> list[dict]:
        output = []
        for index, clause in enumerate(clauses):
            if is_pipeline_dependent_consumer(clause):
                continue
            row = {target: {"values": (), "unavailable_reason": "edge_kappa_time_only"}
                   for target in TARGET_UNITS}
            reason = clause.get("prediction_unavailable_reason")
            if reason:
                row["duration_ms"] = {"values": (), "unavailable_reason": reason}
                output.append(row)
                continue
            query = shell_query(shlex.join(clause["argv"]), repo=repo)
            event_id = None
            if not self.frozen and call_id is not None and call_id not in self._completed_calls:
                event_id = f"{call_id}:{clause.get('clause_index', index)}"
                existing = self._calls.get(call_id, {}).get(event_id)
                if existing and "evidence" in existing:
                    if frozenset(existing["query"]["features"]) != query.features:
                        raise ValueError("call clause identity reused with different features")
                    output.append(copy.deepcopy(existing["evidence"]))
                    continue
                start = existing["start_time"] if existing else ts_start
                self.kb.begin(query, event_id, start, task_id=call_id, call_id=call_id,
                              clause_id=str(clause.get("clause_index", index)))
                self._calls.setdefault(call_id, {})[event_id] = {
                    "start_time": start, "query": {"features": sorted(query.features),
                    "core_features": sorted(query.core_features), "tool": query.tool,
                    "normalization_version": query.normalization_version},
                    "clause_id": str(clause.get("clause_index", index))}
            distribution = self.kb.predict_duration(query)
            row["duration_ms"] = {
                "values": distribution.values_ms, "weights": distribution.weights,
                "evidence_count": distribution.evidence_count,
                "unavailable_reason": distribution.unavailable_reason,
                "context": ["edge_kappa:" + distribution.bucket_prediction.node_id],
                "assumptions": ["weighted_empirical_durations",
                                "numerical_bucket_smoothing_has_no_duration_atom"],
            }
            if event_id is not None:
                self._calls[call_id][event_id]["evidence"] = copy.deepcopy(row)
            output.append(row)
        return output

    def complete_call(self, call_id: str, observations: Iterable[ClauseObservation], *, commit_time: float) -> None:
        """Unique feature matches learn from pre-action tokens; others count only.

        Ambiguous repeated commands must never borrow another clause's gradient.
        Unexecuted branches and missing telemetry release their unused tokens.
        """
        if self.frozen or call_id in self._completed_calls:
            return
        tokens = self._calls.get(call_id, {})
        events = [event for row in observations if (event := training_event(row)) is not None]
        if not tokens and not events:
            return
        frequencies = Counter(event.query.features for event in events)
        delivered = set()
        for event in events:
            if event.outcome.event_id in self._observation_ids:
                continue
            matches = [(key, token) for key, token in tokens.items()
                       if frozenset(token["query"]["features"]) == event.query.features]
            match = matches[0] if len(matches) == 1 and frequencies[event.query.features] == 1 else None
            if match and event.outcome.end_time >= match[1]["start_time"]:
                event_id, token = match
                outcome = TimeOutcome(event_id, token["start_time"], event.outcome.end_time,
                    call_id, call_id, token["clause_id"], duration_ms=event.outcome.duration_ms)
                self.kb.complete(event_id, outcome)
            else:
                event_id = f"{call_id}:observed:{event.outcome.event_id}"
                outcome = TimeOutcome(event_id, event.outcome.start_time, event.outcome.end_time,
                    call_id, call_id, "observed", duration_ms=event.outcome.duration_ms)
                self.kb.complete(event_id, outcome, query=event.query)
            delivered.add(event_id)
            self._feedback[event.outcome.event_id] = self.kb.completion_feedback(event_id)
            self._observation_ids.add(event.outcome.event_id)
        self.kb.commit_before(commit_time)
        # A just-ended sample can remain pending until the next strictly later
        # query. Only tokens without a delivered completion may be discarded.
        for event_id in tokens:
            if event_id not in delivered:
                self.kb.discard_prediction(event_id)
        self._calls.pop(call_id, None)
        self._completed_calls.add(call_id)

    def merge_historical(self, observations: Iterable[ClauseObservation]) -> None:
        """Existing weights survive restart; newly imported history is count-only."""
        known = self._observation_ids
        for row in observations:
            event = training_event(row)
            if event is not None and event.outcome.event_id not in known:
                self.kb.complete(event.outcome.event_id, event.outcome, query=event.query)
                known.add(event.outcome.event_id)
        self.kb.commit_before(float("inf"))

    def merge_feedback(self, source: EdgeKappaRuntime) -> None:
        """Replay one task's durable feedback before importing its raw history.

        Callers order tasks by stable task ID; within a task, completion time
        and event ID order the delayed gradients. Inherited feedback is deduped.
        """
        if not source._feedback and any(
                edge.update_count > (self.kb.edges[key].update_count if key in self.kb.edges else 0)
                for key, edge in source.kb.edges.items()):
            raise ValueError("task learned edge weights without replayable feedback; rerun the task")
        for identity, feedback in sorted(source._feedback.items(), key=lambda item: (
                item[1]["outcome"]["end_time"], item[1]["outcome"]["event_id"])):
            if identity in self._observation_ids:
                continue
            normalized = self.kb.replay_feedback(feedback, source.kb)
            self._observation_ids.add(identity)
            self._feedback[identity] = copy.deepcopy(normalized)
        self._completed_calls.update(source._completed_calls)
