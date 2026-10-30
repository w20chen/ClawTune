"""Common tool-call adapter and restricted distribution composer.

Plain foreground commands, unconditional serial lists, and simple pipelines are
supported. Downstream dependency-only pipe consumers are excluded. Duration
and resource composition resample clause marginals with explicit approximations.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from clawtune_sidecar.contracts.load_prediction import (
    ClauseLoadPrediction, CallLoadPrediction, LoadBuckets, LoadDiagnostics, LoadEstimate, TARGET_DEFINITIONS, TARGET_UNITS,
)
from tool_resource.features import parse_command_clauses, shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import ToolCallQuery, is_pipeline_dependent_consumer


_PARTIAL_MEMORY_TARGETS = {
    "sampled_peak_rss_bytes",
    "memory_total_peak_bytes",
    "memory_extra_peak_bytes",
}


@dataclass
class CompositionCache:
    """Numerical results scoped to one call; explanations remain independent."""

    draws: dict = field(default_factory=dict)
    summaries: dict = field(default_factory=dict)
    # Retain owners so tuple identities cannot be recycled during this request.
    sample_summaries: dict = field(default_factory=dict)


def _sample_summary(samples, edges, cache):
    immutable = cache is not None and type(samples) is tuple
    boundaries = tuple(edges)
    if immutable:
        identity_key = (id(samples), boundaries)
        cached = cache.sample_summaries.get(identity_key)
        if cached is not None:
            return cached[1], cached[2]
        normalized = []
        for value in samples:
            # Numeric subclasses may override conversion using mutable state.
            kind = type(value)
            if kind is float or kind is int:
                if math.isfinite(value) and value >= 0:
                    normalized.append(float(value))
            else:
                immutable = False
                if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and value >= 0):
                    normalized.append(float(value))
        values = tuple(normalized)
    else:
        values = tuple(float(v) for v in samples if isinstance(v, (int, float))
                       and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    count = len(values)
    numeric = None
    if count:
        key = (values, boundaries)
        numeric = cache.summaries.get(key) if cache is not None else None
        if numeric is None:
            ordered = sorted(values)
            counts = [0] * (len(edges) + 1)
            for value in ordered:
                counts[bisect_right(edges, value)] += 1
            middle = count // 2
            p50 = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
            numeric = (statistics.mean(ordered), p50,
                       ordered[math.ceil(0.9 * count) - 1], tuple(n / count for n in counts))
            if cache is not None:
                cache.summaries[key] = numeric
    if immutable:
        cache.sample_summaries[identity_key] = (samples, numeric, count)
    return numeric, count


def summarize(target: str, edges: Sequence[float], backend: str,
              samples: Sequence[float] = (), *, method: str = "direct",
              evidence_counts: Sequence[int] = (), context: Sequence[str] = (),
              assumptions: Sequence[str] = (), reason: str = "no_compatible_evidence",
              cache: CompositionCache | None = None) -> LoadEstimate:
    numeric, count = _sample_summary(samples, edges, cache)
    common = dict(unit=TARGET_UNITS[target], metric_definition=TARGET_DEFINITIONS[target],
                  backend=backend, context=list(context), assumptions=list(assumptions))
    if numeric is None:
        return LoadEstimate(status="unavailable", method="unavailable", unavailable_reason=reason,
                            buckets=LoadBuckets(edges=list(edges)), **common)
    return LoadEstimate(status="available", method=method,
                        avg=numeric[0], p50=numeric[1], p90=numeric[2],
                        buckets=LoadBuckets(edges=list(edges), probabilities=list(numeric[3])),
                        sample_count=count, evidence_counts=list(evidence_counts or [count]), **common)


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


def _draw_samples(target, values, groups, cpu_values=(), duration_values=(), *, cache=None):
    if cache is not None:
        key = (target, tuple(tuple(v) for v in values), tuple(tuple(g) for g in groups),
               tuple(tuple(v) for v in cpu_values), tuple(tuple(v) for v in duration_values))
        if key in cache.draws:
            return cache.draws[key]
    rng = random.Random(0)
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
            draw = [rng.choice(v) for v in values]
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
    samples = tuple(samples)
    if cache is not None:
        cache.draws[key] = samples
    return samples


def duration_p50(values, clauses):
    """Oracle objective with the exact same draws as compose, without reporting work."""
    if len(values) == 1:
        return statistics.median(values[0])
    groups = _composition_groups(clauses, len(values))
    if groups is None:
        return None
    samples = _draw_samples("duration_ms", values, groups)
    valid = [float(v) for v in samples if isinstance(v, (int, float))
             and not isinstance(v, bool) and math.isfinite(v) and v >= 0]
    return statistics.median(valid) if valid else None


def compose(backend: str, evidence: Sequence[Mapping[str, Mapping[str, Any]]],
            edges: Mapping[str, Sequence[float]], *, reason: str | None = None,
            clauses: Sequence[Mapping[str, Any]] = (),
            cache: CompositionCache | None = None) -> CallLoadPrediction:
    targets = {}
    for target, boundaries in edges.items():
        why = reason
        rows = [row.get(target) for row in evidence]
        partial_structure_reason = None
        if (
            why and target in _PARTIAL_MEMORY_TARGETS
            and any(row and row.get("values") for row in rows)
        ):
            partial_structure_reason = str(why)
            why = None
        target_clauses = clauses
        available_indices = None
        partial_clause_count = 0
        original_clause_count = len(rows)
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
        elif not why and target in _PARTIAL_MEMORY_TARGETS:
            available = [
                index for index, row in enumerate(rows)
                if row and row.get("values")
            ]
            if available:
                available_indices = available
                partial_clause_count = len(rows) - len(available)
                rows = [rows[index] for index in available]
                retained_clauses = [
                    clause for clause in clauses
                    if not is_pipeline_dependent_consumer(clause)
                ]
                if len(retained_clauses) == original_clause_count:
                    target_clauses = [retained_clauses[index] for index in available]
                elif partial_clause_count:
                    # Evidence is still usable for a max/peak estimate even
                    # when a caller did not provide clause structure.
                    target_clauses = ()
            else:
                why = _missing_evidence_reason(rows, "missing_clause_evidence")
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
            for clause in target_clauses
            if not is_pipeline_dependent_consumer(clause)
            for assumption in clause.get("prediction_assumptions", [])
        ]
        assumptions += [a for row in rows if row for a in row.get("assumptions", ())]
        if partial_clause_count:
            assumptions += [
                "partial_clause_evidence",
                f"covered_retained_clauses:{len(rows)}/{original_clause_count}",
            ]
        if partial_structure_reason:
            assumptions += [
                "partial_dynamic_structure_evidence",
                f"ignored_structure_reason:{partial_structure_reason}",
            ]
        if len(values) == 1:
            samples = values[0]
            evidence_counts = [rows[0].get("evidence_count", len(values[0]))]
        else:
            # Fixed seed: repeatable query results. Generated samples are not
            # counted as historical evidence. Never sum medians or p90 values.
            # Preserve the original pipeline connectivity before selecting
            # target-specific evidence. Removing an intermediate consumer or
            # an unmeasured stage must not turn parallel stages into serial ones.
            groups = _composition_groups(clauses, original_clause_count)
            if groups is not None and available_indices is not None:
                remap = {old: new for new, old in enumerate(available_indices)}
                groups = [[remap[index] for index in group if index in remap]
                          for group in groups]
                groups = [group for group in groups if group]
            if groups is None:
                targets[target] = summarize(
                    target, boundaries, backend, reason="clause_evidence_alignment_error"
                )
                continue
            samples = _draw_samples(target, values, groups, cpu_values, duration_values, cache=cache)
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
        targets[target] = summarize(target, boundaries, backend, samples, cache=cache, **kwargs)
    quantile_method = "median_p50_nearest_rank_p90"
    return CallLoadPrediction(targets=targets, quantile_method=quantile_method)


@dataclass(frozen=True)
class PreparedCall:
    clauses: tuple[dict[str, Any], ...]
    reason: str | None


def prepare_call(query: ToolCallQuery, *, parsed_clauses=None, parsed_execution=None, feature_call_id=None,
                 feature_service=None, command_context=None) -> PreparedCall:
    """Normalize and bind command features once for all prediction backends."""
    if parsed_execution is not None:
        clauses, reason = parsed_execution
    elif parsed_clauses is not None:
        from tool_resource.commands import unwrap_argv
        normalized = []
        for index, clause in enumerate(parsed_clauses):
            argv, env, assumptions, why = unwrap_argv(clause.get("argv", []))
            if not argv or not shell_bin_requires_exec_evidence(argv[0].rsplit("/", 1)[-1], argv[0]):
                continue
            normalized.append(dict(clause, syntax_argv=clause.get("syntax_argv", clause["argv"]), argv=argv, bin=argv[0].rsplit("/", 1)[-1],
                clause_index=clause.get("clause_index", index), env=env,
                prediction_assumptions=assumptions, prediction_unavailable_reason=why))
        clauses, reason = tuple(normalized), None
    else:
        try:
            clauses, reason = plain_execution(query.command) if query.tool_name in {"exec", "terminal_exec"} else ((), "not_a_shell_command")
        except Exception as exc:
            clauses, reason = (), f"parse_error:{type(exc).__name__}"
    if not reason:
        from command_parser import ParseContext, default_service
        clauses = (feature_service or default_service()).prepare(
            clauses, command_context or ParseContext(repo=query.repo), feature_call_id if feature_service else None,
        )
    clauses = tuple(dict(c, memory_measurement=query.memory_measurement) for c in clauses)
    return PreparedCall(clauses, reason)


def predict_clause_load(backend: str, kb: Any, query: ToolCallQuery,
                        edges: Mapping[str, Sequence[float]], prepared: PreparedCall, *,
                        query_cache=None, composition_cache=None) -> CallLoadPrediction:
    """Evaluate one clause backend without rerunning unrelated predictors."""
    clauses, reason = prepared.clauses, prepared.reason
    try:
        options = {"query_cache": query_cache} if backend == "lattice" and query_cache is not None else {}
        evidence = kb.predict_load_samples(query.repo, clauses, query.ts_start, **options) if not reason else ()
        retained = [c for c in clauses if not is_pipeline_dependent_consumer(c)]
        scoped = []
        for c, row in zip(retained, evidence):
            why = c.get("prediction_unavailable_reason")
            result = compose(backend, [row], edges, reason=why, clauses=[c], cache=composition_cache)
            clause_targets = dict(result.targets)
            for target, definition in (("duration_ms", "clause_elapsed"), ("cpu_avg_cores", "owned_cpu_time_over_clause_elapsed")):
                clause_targets[target] = clause_targets[target].model_copy(update={"metric_definition": definition})
            scoped.append(ClauseLoadPrediction(
                quantile_method=result.quantile_method,
                clause_index=c.get("clause_index", len(scoped)), argv=list(c["argv"]),
                cwd=c.get("cwd"), env_names=sorted(c.get("env", {})), targets=clause_targets, memory_measurement=query.memory_measurement))
        combined_reason = reason or next((c.get("prediction_unavailable_reason") for c in retained
                                          if c.get("prediction_unavailable_reason")), None)
        prediction = compose(
            backend, evidence, edges, reason=combined_reason, clauses=clauses, cache=composition_cache
        ).model_copy(update={"clause_predictions": scoped})
        # These are predictions, not observed hook labels. Make the
        # approximation explicit instead of silently changing definitions.
        targets = dict(prediction.targets)
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
        prediction = prediction.model_copy(update={"targets": targets})
    except Exception as exc:
        prediction = compose(backend, (), edges, reason=f"backend_error:{type(exc).__name__}")
    return prediction.model_copy(update={"memory_measurement": query.memory_measurement})


def predict_call_load(*, runtime: Any, trie: Any, lattice: Any, query: ToolCallQuery,
                      edges: Mapping[str, Sequence[float]],
                      parsed_clauses: Sequence[Mapping[str, Any]] | None = None,
                      parsed_execution=None,
                      feature_call_id: str | None = None,
                      feature_service: Any = None, command_context: Any = None,
                      prepared: PreparedCall | None = None, query_cache=None, composition_cache=None,
                      ) -> tuple[CallLoadPrediction, LoadDiagnostics]:
    """Return ToolKB for existing consumers and each configured independent result.

    The historical name "runtime" is retained in backend metadata and snapshots.
    No target or clause is filled using another model's evidence.
    """
    composition_cache = composition_cache if composition_cache is not None else CompositionCache()
    backends = {}
    try:
        direct = runtime.predict_load_samples(query)
        backends["runtime"] = CallLoadPrediction(targets={
            target: summarize(target, boundaries, "runtime", direct.get(target, {}).get("values", ()),
                              context=direct.get(target, {}).get("context", ()), cache=composition_cache)
            for target, boundaries in edges.items()
        })
    except Exception as exc:
        backends["runtime"] = compose("runtime", (), edges, reason=f"backend_error:{type(exc).__name__}")
    prepared = prepared if prepared is not None else prepare_call(
        query, parsed_clauses=parsed_clauses, parsed_execution=parsed_execution, feature_call_id=feature_call_id,
        feature_service=feature_service, command_context=command_context,
    )
    for backend, kb in (("trie", trie), ("lattice", lattice)):
        backends[backend] = predict_clause_load(backend, kb, query, edges, prepared,
            query_cache=query_cache, composition_cache=composition_cache)
    backends = {name: value.model_copy(update={"memory_measurement": query.memory_measurement}) for name, value in backends.items()}
    return backends["runtime"], LoadDiagnostics(backends=backends)
