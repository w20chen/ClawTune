"""Common tool-call adapter and restricted distribution composer.

Plain foreground commands, unconditional serial lists, and simple pipelines are
supported. Downstream dependency-only pipe consumers are excluded. Duration
composition resamples clause marginals; memory composition requires joint evidence.
"""
from __future__ import annotations

import math
import random
import statistics
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from clawtune_sidecar.contracts.load_prediction import (
    ClauseLoadPrediction, CallLoadPrediction, LoadBuckets, LoadDiagnostics, LoadEstimate, TARGET_DEFINITIONS, TARGET_UNITS,
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
    from tool_resource.commands import parse_execution
    return parse_execution(command, parser=parse_command_clauses)


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
        if not why and len(rows) > 1 and target.startswith("memory_"):
            why = "requires_environment_baseline_and_joint_memory_timeline"
        if not why and len(rows) > 1 and target == "cpu_avg_cores":
            why = "requires_paired_cpu_time_and_duration_samples"
        if why:
            targets[target] = summarize(target, boundaries, backend, reason=why)
            continue
        values = [row["values"] for row in rows]
        contexts = [str(c) for row in rows for c in row.get("context", ())]
        assumptions = ["foreground_clause_lineage_covers_retained_workload", "shell_and_hook_overhead_not_modeled",
                       "listed_downstream_consumers_excluded"]
        assumptions += [a for c in clauses for a in c.get("prediction_assumptions", [])]
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
            samples = []
            for _ in range(2048):
                draw = [rng.choice(v) for v in values]
                if target == "duration_ms":
                    value = sum(max(draw[i] for i in group) for group in groups)
                elif target == "cpu_time_seconds":
                    value = sum(draw)
                else:  # CPU peak: aligned peaks are unknown; stage sum is conservative.
                    value = max(sum(draw[i] for i in group) for group in groups)
                samples.append(value)
            assumptions += ["independent_clause_marginals", "all_retained_stages_execute",
                            "no_surviving_background_work"]
            if any(len(group) > 1 for group in groups):
                assumptions.append("pipeline_group_duration_is_stage_max" if target == "duration_ms"
                                   else "parallel_peak_sum_is_conservative_not_calibrated")
        targets[target] = summarize(target, boundaries, backend, samples, method="composed",
                                    evidence_counts=[len(v) for v in values], context=contexts,
                                    assumptions=assumptions)
    return CallLoadPrediction(targets=targets)


def predict_call_load(*, runtime: Any, trie: Any, lattice: Any, query: ToolCallQuery,
                      edges: Mapping[str, Sequence[float]],
                      parsed_clauses: Sequence[Mapping[str, Any]] | None = None,
                      ) -> tuple[CallLoadPrediction, LoadDiagnostics]:
    """Return ToolKB for existing consumers and all three independent results.

    The historical name "runtime" is retained in backend metadata and snapshots.
    No target or clause is filled using another model's evidence.
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
    if parsed_clauses is not None:
        from tool_resource.commands import unwrap_argv
        normalized = []
        for index, clause in enumerate(parsed_clauses):
            argv, env, assumptions, why = unwrap_argv(clause.get("argv", []))
            if not argv or not shell_bin_requires_exec_evidence(argv[0].rsplit("/", 1)[-1], argv[0]):
                continue
            normalized.append(dict(clause, argv=argv, bin=argv[0].rsplit("/", 1)[-1],
                clause_index=clause.get("clause_index", index), env=env,
                prediction_assumptions=assumptions, prediction_unavailable_reason=why))
        clauses, reason = tuple(normalized), None
    else:
        try:
            clauses, reason = plain_execution(query.command) if query.tool_name in {"exec", "terminal_exec"} else ((), "not_a_shell_command")
        except Exception as exc:
            clauses, reason = (), f"parse_error:{type(exc).__name__}"
    for backend, kb in (("trie", trie), ("lattice", lattice)):
        try:
            clauses = tuple(dict(c, memory_measurement=query.memory_measurement) for c in clauses)
            evidence = kb.predict_load_samples(query.repo, clauses, query.ts_start) if not reason else ()
            retained = [c for c in clauses if not is_pipeline_dependent_consumer(c)]
            scoped = []
            for c, row in zip(retained, evidence):
                why = c.get("prediction_unavailable_reason")
                result = compose(backend, [row], edges, reason=why, clauses=[c])
                clause_targets = dict(result.targets)
                for target, definition in (("duration_ms", "clause_elapsed"), ("cpu_avg_cores", "owned_cpu_time_over_clause_elapsed")):
                    clause_targets[target] = clause_targets[target].model_copy(update={"metric_definition": definition})
                scoped.append(ClauseLoadPrediction(
                    clause_index=c.get("clause_index", len(scoped)), argv=list(c["argv"]),
                    cwd=c.get("cwd"), env_names=sorted(c.get("env", {})), targets=clause_targets, memory_measurement=query.memory_measurement))
            combined_reason = reason or next((c.get("prediction_unavailable_reason") for c in retained
                                              if c.get("prediction_unavailable_reason")), None)
            backends[backend] = compose(
                backend, evidence, edges, reason=combined_reason, clauses=clauses
            ).model_copy(update={"clause_predictions": scoped})
            # These are predictions, not observed hook labels. Make the
            # approximation explicit instead of silently changing definitions.
            targets = dict(backends[backend].targets)
            for target, assumptions in (
                ("duration_ms", ["tool_hook_overhead_assumed_zero"]),
                ("cpu_avg_cores", ["tool_hook_overhead_assumed_zero"]),
                ("memory_total_peak_bytes", ["no_environment_memory_peak_outside_clause"]),
                ("memory_extra_peak_bytes", ["clause_baseline_assumed_equal_to_tool_baseline",
                                             "no_environment_memory_peak_outside_clause"]),
            ):
                if targets[target].status == "available":
                    targets[target] = targets[target].model_copy(update={
                        "assumptions": [*targets[target].assumptions, *assumptions],
                    })
            backends[backend] = backends[backend].model_copy(update={"targets": targets})
        except Exception as exc:
            backends[backend] = compose(backend, (), edges, reason=f"backend_error:{type(exc).__name__}")
    backends = {name: value.model_copy(update={"memory_measurement": query.memory_measurement}) for name, value in backends.items()}
    return backends["runtime"], LoadDiagnostics(backends=backends)
