"""Common tool-call adapter and restricted distribution composer.

Plain foreground commands, unconditional serial lists, and simple pipelines are
supported. Downstream dependency-only pipe consumers are excluded. Duration
and resource composition resample clause marginals with explicit approximations.
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


def summarize_weighted(target: str, edges: Sequence[float], backend: str,
                       values: Sequence[float], weights: Sequence[float], **kwargs: Any) -> LoadEstimate:
    """Exact summaries of weighted duration atoms; no resampling for one clause."""
    if len(values) != len(weights) or any(not math.isfinite(w) or w <= 0 for w in weights):
        raise ValueError("invalid empirical weights")
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("invalid empirical values")
    result = summarize(target, edges, backend, values, **kwargs)
    if not values:
        return result
    ordered = sorted(zip(values, weights))
    total = math.fsum(weights)
    bucket_weights: list[list[float]] = [[] for _ in range(len(edges) + 1)]
    for value, weight in ordered:
        bucket_weights[bisect_right(edges, value)].append(weight)
    # Use the same stable summation for each bucket and the total: ordinary
    # accumulation can make a bucket containing all atoms exceed probability 1.
    counts = [math.fsum(bucket) for bucket in bucket_weights]
    def quantile(probability):
        cumulative = 0.0
        correction = 0.0
        threshold = probability * total
        for index, (value, weight) in enumerate(ordered):
            # Compensated prefix sums keep this linear in the number of atoms.
            adjusted = weight - correction
            updated = cumulative + adjusted
            correction = (updated - cumulative) - adjusted
            cumulative = updated
            # Only allow rounding-sized differences, not a statistical tolerance
            # that would turn genuinely unequal masses into a midpoint median.
            at_boundary = math.isclose(cumulative, threshold, rel_tol=0.0,
                                       abs_tol=2 * math.ulp(threshold))
            if cumulative >= threshold or at_boundary:
                if probability == .5 and at_boundary and index + 1 < len(ordered):
                    return (value + ordered[index + 1][0]) / 2
                return value
        return ordered[-1][0]
    return result.model_copy(update=dict(avg=math.fsum(v*w for v, w in ordered) / total,
        p50=quantile(.5), p90=quantile(.9),
        buckets=LoadBuckets(edges=list(edges), probabilities=[count / total for count in counts])))


def plain_execution(command: str | None) -> tuple[tuple[dict[str, Any], ...], str | None]:
    from tool_resource.commands import parse_execution
    return parse_execution(command, parser=parse_command_clauses)


def _missing_evidence_reason(
    rows: Sequence[Mapping[str, Any] | None], default: str
) -> str | None:
    if rows and all(row and row.get("values") for row in rows):
        return None
    return next(
        (
            str(row["unavailable_reason"])
            for row in rows
            if row and row.get("unavailable_reason")
        ),
        default,
    )


def _composition_groups(
    clauses: Sequence[Mapping[str, Any]], retained_count: int
) -> list[list[int]] | None:
    if not clauses:
        return [[index] for index in range(retained_count)]
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
    return groups if retained_index == retained_count else None


def compose(backend: str, evidence: Sequence[Mapping[str, Mapping[str, Any]]],
            edges: Mapping[str, Sequence[float]], *, reason: str | None = None,
            clauses: Sequence[Mapping[str, Any]] = ()) -> CallLoadPrediction:
    targets = {}
    for target, boundaries in edges.items():
        why = reason
        rows = [row.get(target) for row in evidence]
        cpu_rows: list[Mapping[str, Any] | None] = []
        duration_rows: list[Mapping[str, Any] | None] = []
        if not why and len(rows) > 1 and target == "cpu_avg_cores":
            cpu_rows = [row.get("cpu_time_seconds") for row in evidence]
            duration_rows = [row.get("duration_ms") for row in evidence]
            why = _missing_evidence_reason(
                cpu_rows, "missing_clause_cpu_time_evidence"
            ) or _missing_evidence_reason(
                duration_rows, "missing_clause_duration_evidence"
            )
        elif not why:
            why = _missing_evidence_reason(rows, "missing_clause_evidence")
        if why:
            targets[target] = summarize(target, boundaries, backend, reason=why)
            continue
        value_rows = cpu_rows if cpu_rows else rows
        values = [row["values"] for row in value_rows if row is not None]
        cpu_values = [row["values"] for row in cpu_rows if row is not None]
        duration_values = [
            row["values"] for row in duration_rows if row is not None
        ]
        context_rows = [*cpu_rows, *duration_rows] if cpu_rows else rows
        contexts = [
            str(context)
            for row in context_rows
            if row is not None
            for context in row.get("context", ())
        ]
        assumptions = ["foreground_clause_lineage_covers_retained_workload", "shell_and_hook_overhead_not_modeled",
                       "listed_downstream_consumers_excluded"]
        assumptions += [
            assumption
            for clause in clauses
            if not is_pipeline_dependent_consumer(clause)
            for assumption in clause.get("prediction_assumptions", [])
        ]
        assumptions += [a for row in rows if row for a in row.get("assumptions", ())]
        sample_weights = None
        if len(values) == 1:
            samples = values[0]
            sample_weights = rows[0].get("weights")
            evidence_counts = [rows[0].get("evidence_count", len(values[0]))]
        else:
            # Fixed seed: repeatable query results. Generated samples are not
            # counted as historical evidence. Never sum medians or p90 values.
            rng = random.Random(0)
            groups = _composition_groups(clauses, len(values))
            if groups is None:
                targets[target] = summarize(
                    target, boundaries, backend, reason="clause_evidence_alignment_error"
                )
                continue
            samples = []
            for _ in range(2048):
                if target == "cpu_avg_cores":
                    cpu_draw = [rng.choice(value) for value in cpu_values]
                    duration_draw = [rng.choice(value) for value in duration_values]
                    total_duration_ms = sum(
                        max(duration_draw[i] for i in group) for group in groups
                    )
                    if total_duration_ms <= 0:
                        continue
                    value = sum(cpu_draw) / (total_duration_ms / 1000.0)
                else:
                    draw = [rng.choices(v, weights=row["weights"], k=1)[0]
                            if row.get("weights") is not None else rng.choice(v)
                            for v, row in zip(values, rows)]
                    if target == "duration_ms":
                        value = sum(max(draw[i] for i in group) for group in groups)
                    elif target == "cpu_time_seconds":
                        value = sum(draw)
                    elif target in {
                        "memory_total_peak_bytes",
                        "memory_extra_peak_bytes",
                    }:
                        value = max(draw)
                    else:  # CPU/RSS peaks lack alignment; pipeline sums are conservative.
                        value = max(sum(draw[i] for i in group) for group in groups)
                samples.append(value)
            if target == "cpu_avg_cores" and not samples:
                targets[target] = summarize(
                    target,
                    boundaries,
                    backend,
                    reason="nonpositive_composed_duration",
                )
                continue
            assumptions += ["independent_clause_marginals", "all_retained_stages_execute",
                            "no_surviving_background_work"]
            if target == "cpu_avg_cores":
                assumptions.append(
                    "cpu_time_sum_divided_by_composed_duration_approximation"
                )
                evidence_counts = [
                    min(len(cpu), len(duration))
                    for cpu, duration in zip(cpu_values, duration_values)
                ]
            else:
                evidence_counts = [row.get("evidence_count", len(v)) for v, row in zip(values, rows)]
            if target.startswith("memory_"):
                assumptions.append("call_environment_peak_approximated_by_clause_max")
            if any(len(group) > 1 for group in groups):
                if target in {"duration_ms", "cpu_avg_cores"}:
                    assumptions.append("pipeline_group_duration_is_stage_max")
                elif target in {"cpu_peak_cores", "sampled_peak_rss_bytes"}:
                    assumptions.append(
                        "parallel_peak_sum_is_conservative_not_calibrated"
                    )
        kwargs = dict(method="composed", evidence_counts=evidence_counts, context=contexts, assumptions=assumptions)
        targets[target] = (summarize_weighted(target, boundaries, backend, samples, sample_weights, **kwargs)
                           if sample_weights is not None else summarize(target, boundaries, backend, samples, **kwargs))
    quantile_method = ("weighted_midpoint_p50_inverse_cdf_p90" if backend == "edge_kappa" and len(evidence) == 1
                       else "median_p50_nearest_rank_p90")
    return CallLoadPrediction(targets=targets, quantile_method=quantile_method)


def predict_call_load(*, runtime: Any, trie: Any, lattice: Any, query: ToolCallQuery,
                      edges: Mapping[str, Sequence[float]],
                      parsed_clauses: Sequence[Mapping[str, Any]] | None = None,
                      edge_kappa: Any = None, edge_call_id: str | None = None,
                      ) -> tuple[CallLoadPrediction, LoadDiagnostics]:
    """Return ToolKB for existing consumers and each configured independent result.

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
    clause_backends = [("trie", trie), ("lattice", lattice)]
    if edge_kappa is not None:
        clause_backends.append(("edge_kappa", edge_kappa))
    for backend, kb in clause_backends:
        try:
            clauses = tuple(dict(c, memory_measurement=query.memory_measurement) for c in clauses)
            extra = {"call_id": edge_call_id} if backend == "edge_kappa" else {}
            evidence = kb.predict_load_samples(query.repo, clauses, query.ts_start, **extra) if not reason else ()
            retained = [c for c in clauses if not is_pipeline_dependent_consumer(c)]
            scoped = []
            for c, row in zip(retained, evidence):
                why = c.get("prediction_unavailable_reason")
                result = compose(backend, [row], edges, reason=why, clauses=[c])
                clause_targets = dict(result.targets)
                for target, definition in (("duration_ms", "clause_elapsed"), ("cpu_avg_cores", "owned_cpu_time_over_clause_elapsed")):
                    clause_targets[target] = clause_targets[target].model_copy(update={"metric_definition": definition})
                scoped.append(ClauseLoadPrediction(
                    quantile_method=result.quantile_method,
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
