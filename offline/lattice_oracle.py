"""Hindsight duration selector, intentionally available only to offline evaluation."""
from __future__ import annotations

from itertools import product
import math

from clawtune_sidecar.predictors.call_load import duration_p50


ORACLE_DESCRIPTION = {
    "uses_test_labels": True,
    "target": "duration_ms",
    "objective": "min_absolute_call_p50_error",
    "candidate_policy": "execution_identity_subsets_exact_own_preference",
}


class LatticeOracle:
    """Choose a globally optimal candidate tuple under the existing call composer.

    All combinations are examined: no greedy clause choice, search budget,
    interpolation, or synthesized samples. Equal errors retain the first tuple
    in deterministic feature order. Only duration is evaluated; other targets
    explicitly remain unavailable.
    """

    def __init__(self, kb, actual_duration_ms, edges):
        if not getattr(kb, "_frozen", False):
            raise ValueError("offline oracle requires a frozen Lattice KB")
        self.kb = kb
        self.actual = actual_duration_ms
        self.edges = edges

    def predict_load_samples(self, repo, clauses, ts_start, *, query_cache=None):
        from tool_resource.runtime_kb import is_pipeline_dependent_consumer

        retained = [clause for clause in clauses if not is_pipeline_dependent_consumer(clause)]

        def unavailable(reason):
            return tuple({target: {"values": (), "unavailable_reason":
                          reason if target == "duration_ms" else "oracle_duration_only"}
                          for target in self.edges} for _ in retained)

        if (not isinstance(self.actual, (int, float)) or isinstance(self.actual, bool)
                or not math.isfinite(self.actual) or self.actual < 0):
            return unavailable("oracle_requires_actual_duration")
        options = {"query_cache": query_cache} if query_cache is not None else {}
        candidates = self.kb.duration_candidate_samples(repo, clauses, ts_start, **options)
        if not candidates or any(not choices for choices in candidates):
            return unavailable("missing_clause_evidence")
        # Different feature nodes can have exactly the same ordered samples.
        # The composer then produces identical draws for either node. Keep the
        # first (feature-order) representative without changing oracle ties.
        distinct = []
        for choices in candidates:
            seen = set()
            unique = []
            for row in choices:
                values = tuple(row["values"])
                if values not in seen:
                    seen.add(values)
                    unique.append(row)
            distinct.append(tuple(unique))
        candidates = tuple(distinct)
        if any(clause.get("prediction_unavailable_reason") for clause in retained):
            return unavailable("unsupported_execution_structure")

        def evidence(choice):
            return tuple({
                **{target: {"values": (), "unavailable_reason": "oracle_duration_only"}
                   for target in self.edges if target != "duration_ms"},
                "duration_ms": {**row, "assumptions": ["oracle_uses_actual_call_duration"]},
            } for row in choice)

        best, best_error = None, math.inf
        for choice in product(*candidates):
            p50 = duration_p50([row["values"] for row in choice], clauses)
            if p50 is None:
                continue
            error = abs(p50 - self.actual)
            if error < best_error:
                best, best_error = choice, error
            if error == 0:
                break
        return evidence(best) if best is not None else unavailable("missing_clause_evidence")
