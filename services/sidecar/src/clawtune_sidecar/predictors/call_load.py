"""Common tool-call adapter and restricted distribution composer.

Plain foreground commands, unconditional serial lists, and simple pipelines are
supported. Downstream dependency-only pipe consumers are excluded. Duration
composition resamples clause marginals; multi-clause resources stay unknown.
"""
from __future__ import annotations

import math
import random
import re
import statistics
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from clawtune_sidecar.contracts.load_prediction import (
    CallLoadPrediction, LoadBuckets, LoadDiagnostics, LoadEstimate, TARGET_DEFINITIONS, TARGET_UNITS,
)
from tool_resource.features import parse_command_clauses, shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import ToolCallQuery, is_pipeline_dependent_consumer


def summarize(target: str, edges: Sequence[float], backend: str,
              samples: Sequence[float] = (), *, method: str = "direct",
              evidence_counts: Sequence[int] = (), context: Sequence[str] = (),
              assumptions: Sequence[str] = (), reason: str = "no_compatible_evidence") -> LoadEstimate:
    values = sorted(float(v) for v in samples if isinstance(v, (int, float))
                    and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    common = dict(unit=TARGET_UNITS[target], metric_definition=TARGET_DEFINITIONS[target],
                  backend=backend, context=list(context), assumptions=list(assumptions))
    if not values:
        return LoadEstimate(status="unavailable", method="unavailable", unavailable_reason=reason,
                            buckets=LoadBuckets(edges=list(edges)), **common)
    counts = [0] * (len(edges) + 1)
    for value in values:
        counts[bisect_right(edges, value)] += 1
    return LoadEstimate(status="available", method=method,
                        avg=statistics.mean(values), p50=statistics.median(values),
                        p90=values[math.ceil(0.9 * len(values)) - 1],
                        buckets=LoadBuckets(edges=list(edges), probabilities=[n / len(values) for n in counts]),
                        sample_count=len(values), evidence_counts=list(evidence_counts or [len(values)]), **common)


def plain_execution(command: str | None) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Whitelist literal simple clauses joined by pipes, semicolons, or newlines.

    Inspect gaps as well as clause spans: AST clause flags alone miss &&, &,
    subshells, assignments, redirects and conditionals. Quoted metacharacters
    are conservatively rejected too; direct call history remains usable.
    """
    if not command:
        return (), "not_a_shell_command"
    parsed = parse_command_clauses(command)
    clauses = tuple(parsed["clauses"])
    if parsed["parse_failed"]:
        return clauses, "parse_failed"
    if not clauses or parsed.get("control_edges"):
        return clauses, "unsupported_execution_structure"
    position = 0
    for index, clause in enumerate(clauses):
        argv = clause["argv"]
        if (not argv or not shell_bin_requires_exec_evidence(clause["bin"], argv[0])
                or any(clause.get(flag) for flag in ("in_loop", "in_subst"))):
            return clauses, "unsupported_execution_structure"
        start, end = clause["span"]
        gap = command[position:start]
        previous = clauses[index - 1] if index else None
        pipe_connected = bool(
            previous
            and previous.get("in_pipe")
            and clause.get("in_pipe")
            and int(clause.get("pipeline_position", -1))
            == int(previous.get("pipeline_position", -1)) + 1
        )
        gap_pattern = r"[ \t\r]*\|&?[ \t\r]*" if pipe_connected else r"[ \t\r]*[;\n][;\s]*"
        if (index == 0 and gap.strip()) or (index and not re.fullmatch(gap_pattern, gap)):
            return clauses, "unsupported_execution_structure"
        text = command[start:end]
        # No expansions, redirection, operators, assignments, braces or comments.
        text_without_stderr_merge = re.sub(r"\s+2>&1(?=\s|$)", "", text)
        if re.search(r"[|&<>$`(){}=!#*?\[\]~]", text_without_stderr_merge):
            return clauses, "unsupported_execution_structure"
        position = end
    if not re.fullmatch(r"[;\s]*", command[position:]):
        return clauses, "unsupported_execution_structure"
    return clauses, None


def compose(backend: str, evidence: Sequence[Mapping[str, Mapping[str, Any]]],
            edges: Mapping[str, Sequence[float]], *, reason: str | None = None,
            clauses: Sequence[Mapping[str, Any]] = ()) -> CallLoadPrediction:
    targets = {}
    for target, boundaries in edges.items():
        why = reason
        rows = [row.get(target) for row in evidence]
        if not why and (not rows or any(not row or not row.get("values") for row in rows)):
            why = next((row["unavailable_reason"] for row in rows if row and row.get("unavailable_reason")),
                       "missing_clause_evidence")
        if not why and len(rows) > 1 and target != "duration_ms":
            why = "requires_joint_execution_ownership_and_timeline"
        if why:
            targets[target] = summarize(target, boundaries, backend, reason=why)
            continue
        values = [row["values"] for row in rows]
        contexts = [str(c) for row in rows for c in row.get("context", ())]
        assumptions = ["foreground_clause_lineage_covers_call_workload", "shell_and_hook_overhead_not_modeled"]
        if len(values) == 1:
            samples = values[0]
        else:
            # Fixed seed: repeatable query results. Generated samples are not
            # counted as historical evidence. Never sum medians or p90 values.
            rng = random.Random(0)
            if clauses:
                groups: list[list[int]] = []
                retained_index = 0
                for clause_index, clause in enumerate(clauses):
                    previous = clauses[clause_index - 1] if clause_index else None
                    connected = bool(
                        previous
                        and previous.get("in_pipe")
                        and clause.get("in_pipe")
                        and int(clause.get("pipeline_position", -1))
                        == int(previous.get("pipeline_position", -1)) + 1
                    )
                    if not connected:
                        groups.append([])
                    if not is_pipeline_dependent_consumer(clause):
                        groups[-1].append(retained_index)
                        retained_index += 1
                groups = [group for group in groups if group]
            else:
                groups = [[index] for index in range(len(values))]
            if sum(len(group) for group in groups) != len(values):
                targets[target] = summarize(
                    target, boundaries, backend, reason="clause_evidence_alignment_error"
                )
                continue
            samples = [
                sum(max(rng.choice(values[index]) for index in group) for group in groups)
                for _ in range(2048)
            ]
            assumptions += ["independent_clause_durations", "serial_groups_sum"]
            if any(len(group) > 1 for group in groups):
                assumptions.append("pipeline_group_duration_is_stage_max")
        targets[target] = summarize(target, boundaries, backend, samples, method="composed",
                                    evidence_counts=[len(v) for v in values], context=contexts,
                                    assumptions=assumptions)
    return CallLoadPrediction(targets=targets)


def predict_call_load(*, runtime: Any, trie: Any, lattice: Any, query: ToolCallQuery,
                      edges: Mapping[str, Sequence[float]]) -> tuple[CallLoadPrediction, LoadDiagnostics]:
    """All three backends expose full call results; selection is per target.

    Prefer compatible observed call evidence, then trie baseline, then lattice.
    Selection is deterministic, not a claim of measured superiority.
    """
    backends = {}
    try:
        direct = runtime.predict_load_samples(query)
        backends["runtime"] = CallLoadPrediction(targets={
            target: summarize(target, boundaries, "runtime", direct.get(target, {}).get("values", ()),
                              context=direct.get(target, {}).get("context", ()))
            for target, boundaries in edges.items()
        })
    except Exception as exc:
        backends["runtime"] = compose("runtime", (), edges, reason=f"backend_error:{type(exc).__name__}")
    try:
        clauses, reason = plain_execution(query.command) if query.tool_name == "exec" else ((), "not_a_shell_command")
    except Exception as exc:
        clauses, reason = (), f"parse_error:{type(exc).__name__}"
    for backend, kb in (("trie", trie), ("lattice", lattice)):
        try:
            evidence = kb.predict_load_samples(query.repo, clauses, query.ts_start) if not reason else ()
            backends[backend] = compose(
                backend, evidence, edges, reason=reason, clauses=clauses
            )
        except Exception as exc:
            backends[backend] = compose(backend, (), edges, reason=f"backend_error:{type(exc).__name__}")
    selected = {}
    for target in edges:
        candidates = [backends[name].targets[target] for name in ("runtime", "trie", "lattice")]
        selected[target] = next((value for value in candidates if value.status == "available"),
                                candidates[0].model_copy(update={"unavailable_reason": ";".join(
                                    f"{value.backend}:{value.unavailable_reason}" for value in candidates)}))
    return CallLoadPrediction(targets=selected), LoadDiagnostics(backends=backends)
