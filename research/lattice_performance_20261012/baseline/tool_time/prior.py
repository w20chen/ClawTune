"""Empirical tool-duration priors and their optimal single trigger."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Iterable

import numpy as np

from tool_time._validation import (
    required_nonnegative_float,
    required_text,
)


@dataclass(frozen=True)
class LatencyPrior:
    """Per-group, per-tool, and global latency samples from the profile split."""

    values_by_tool: dict[str, list[float]]  # each sorted ascending
    global_values: list[float]  # sorted ascending
    source_traces: frozenset[str]
    task_ids: frozenset[str]
    values_by_task: dict[str, list[float]]  # task_id -> sorted values
    values_by_task_by_tool: dict[str, dict[str, list[float]]]
    values_by_group: dict[str, list[float]] | None = None  # each sorted ascending
    values_by_task_by_group: dict[str, dict[str, list[float]]] | None = None


@dataclass(frozen=True)
class LatencyPriorNode:
    """One eligible level in the global -> tool -> prefix prior hierarchy."""

    values: list[float]
    values_by_task: dict[str, list[float]]
    source: str
    group_key: str | None



def build_latency_prior(
    rows: Iterable[dict[str, Any]],
    *,
    row_group_keys: Callable[[dict[str, Any]], tuple[str, ...]] | None = None,
) -> LatencyPrior:
    """Aggregate profile-split rows into prefix-node/tool/global latency samples."""

    values_by_tool: dict[str, list[float]] = {}
    values_by_task: dict[str, list[float]] = {}
    values_by_task_by_tool: dict[str, dict[str, list[float]]] = {}
    values_by_group: dict[str, list[float]] | None = (
        {} if row_group_keys is not None else None
    )
    values_by_task_by_group: dict[str, dict[str, list[float]]] | None = (
        {} if row_group_keys is not None else None
    )
    global_values: list[float] = []
    source_traces: set[str] = set()
    task_ids: set[str] = set()
    task_id_by_source: dict[str, str] = {}
    for index, row in enumerate(rows):
        source = f"profile row {index}"
        tool_name = required_text(row, "tool_name", source=source)
        latency_ms = required_nonnegative_float(row, "latency_ms", source=source)
        source_trace = required_text(row, "source_trace", source=source)
        task_id = _row_task_id(row, source=source)
        _record_source_task(
            task_id_by_source,
            source_trace=source_trace,
            task_id=task_id,
            source=source,
        )
        source_traces.add(source_trace)
        task_ids.add(task_id)
        values_by_tool.setdefault(tool_name, []).append(latency_ms)
        values_by_task.setdefault(task_id, []).append(latency_ms)
        values_by_task_by_tool.setdefault(tool_name, {}).setdefault(
            task_id,
            [],
        ).append(latency_ms)
        global_values.append(latency_ms)
        if values_by_group is not None:
            for group_key in row_group_keys(row):
                values_by_group.setdefault(group_key, []).append(latency_ms)
                assert values_by_task_by_group is not None
                values_by_task_by_group.setdefault(group_key, {}).setdefault(
                    task_id,
                    [],
                ).append(latency_ms)
    if not global_values:
        raise ValueError("empty latency prior: no profile rows supplied")
    for values in values_by_tool.values():
        values.sort()
    for values in values_by_task.values():
        values.sort()
    for task_values in values_by_task_by_tool.values():
        for values in task_values.values():
            values.sort()
    if values_by_group is not None:
        for values in values_by_group.values():
            values.sort()
        assert values_by_task_by_group is not None
        for task_values in values_by_task_by_group.values():
            for values in task_values.values():
                values.sort()
    global_values.sort()
    return LatencyPrior(
        values_by_tool=values_by_tool,
        global_values=global_values,
        source_traces=frozenset(source_traces),
        task_ids=frozenset(task_ids),
        values_by_task=values_by_task,
        values_by_task_by_tool=values_by_task_by_tool,
        values_by_group=values_by_group,
        values_by_task_by_group=values_by_task_by_group,
    )


def hazard_recheck_ms(
    values: list[float],
    *,
    threshold_ms: float,
    kv_cost_ms: float,
    restore_cost_ms: float = 0.0,
) -> float:
    """Expected-cost-optimal swap re-check time for a t0-declined call.

    The policy may start the swap at elapsed ``k`` while the call is still
    running. Under the node's sample distribution, a candidate ``k`` earns,
    per sample latency ``L``:

    * 0 when ``L <= k`` (the re-check never fires),
    * ``min(kv, L - k) - max(0, kv - (L - k))`` when ``L > threshold``
      (hiding on a truly long call minus its residual stall),
    * ``-max(0, kv - (L - k)) - restore`` when ``k < L <= threshold`` (a
      swap on a short call stalls, and its swap-back lands on the critical
      path — the deadline policy never fires on short calls, so the restore
      charge is differential).

    The expected benefit is piecewise linear in ``k`` between breakpoints at
    sample-derived points (``L`` and ``L - kv``; the restore step also
    changes only at ``L``), so maximizing over those candidates plus
    ``{0, threshold}`` is exact - no grid and no tuning parameter. Ties
    resolve to the latest ``k``, so thin or ambiguous nodes degrade to the
    plain ``k = threshold`` re-check, which never fires on short calls.
    Empty ``values`` return ``threshold_ms``.
    """

    if not values:
        return threshold_ms
    if not math.isfinite(restore_cost_ms) or restore_cost_ms < 0.0:
        raise ValueError(
            f"restore_cost_ms must be finite and non-negative, got {restore_cost_ms}"
        )
    samples = np.asarray(values, dtype=float)
    candidates = {0.0, threshold_ms}
    for value in values:
        if 0.0 < value < threshold_ms:
            candidates.add(float(value))
        edge = value - kv_cost_ms
        if 0.0 < edge < threshold_ms:
            candidates.add(float(edge))
    is_long = samples > threshold_ms
    best_k = threshold_ms
    best_benefit = -math.inf
    ordered_candidates = sorted(candidates)
    # Keep temporary broadcast arrays near 64K elements (a few MiB total).
    batch_size = max(1, 65_536 // samples.size)
    for start in range(0, len(ordered_candidates), batch_size):
        batch_candidates = ordered_candidates[start : start + batch_size]
        candidate_column = np.asarray(batch_candidates, dtype=float)[:, None]
        window = samples - candidate_column
        fires = samples > candidate_column
        hidden_on_long = np.where(
            fires & is_long, np.minimum(kv_cost_ms, window), 0.0
        )
        exposed = np.where(fires, np.maximum(0.0, kv_cost_ms - window), 0.0)
        restore = np.where(fires & ~is_long, restore_cost_ms, 0.0)
        benefits = np.mean(hidden_on_long - exposed - restore, axis=1)
        # Preserve the original ascending scan and latest-tie winner exactly.
        for k, benefit in zip(batch_candidates, benefits, strict=True):
            if benefit >= best_benefit:
                best_benefit = float(benefit)
                best_k = k
    return best_k


def _row_task_id(row: dict[str, Any], *, source: str) -> str:
    """Return the logical task identity, with legacy trace-level fallback."""

    if "task_id" in row:
        return required_text(row, "task_id", source=source)
    return required_text(row, "source_trace", source=source)


def _record_source_task(
    task_id_by_source: dict[str, str],
    *,
    source_trace: str,
    task_id: str,
    source: str,
) -> None:
    existing = task_id_by_source.get(source_trace)
    if existing is not None and existing != task_id:
        raise ValueError(
            f"{source}: source_trace {source_trace!r} maps to conflicting "
            f"task_id values {existing!r} and {task_id!r}"
        )
    task_id_by_source[source_trace] = task_id


def latency_prior_hierarchy(
    prior: LatencyPrior,
    tool_name: str,
    group_keys: tuple[str, ...],
    *,
    min_tool_history: int,
    min_profile_tasks: int,
) -> tuple[LatencyPriorNode, ...]:
    """Return eligible prior nodes ordered from global to most specific."""

    nodes = [
        LatencyPriorNode(
            values=prior.global_values,
            values_by_task=prior.values_by_task,
            source="prior_global",
            group_key=None,
        )
    ]
    tool_values = prior.values_by_tool.get(tool_name, [])
    tool_values_by_task = prior.values_by_task_by_tool.get(tool_name, {})
    if (
        len(tool_values) >= min_tool_history
        and len(tool_values_by_task) >= min_profile_tasks
    ):
        nodes.append(
            LatencyPriorNode(
                values=tool_values,
                values_by_task=tool_values_by_task,
                source="prior_tool",
                group_key=None,
            )
        )
    if prior.values_by_group is not None and prior.values_by_task_by_group is not None:
        for group_key in group_keys:
            group_values = prior.values_by_group.get(group_key, [])
            group_values_by_task = prior.values_by_task_by_group.get(group_key, {})
            if (
                len(group_values) >= min_tool_history
                and len(group_values_by_task) >= min_profile_tasks
            ):
                nodes.append(
                    LatencyPriorNode(
                        values=group_values,
                        values_by_task=group_values_by_task,
                        source="prior_group",
                        group_key=group_key,
                    )
                )
    return tuple(nodes)


def validate_profile_eval_disjoint(
    eval_rows: list[dict[str, Any]],
    *,
    prior: LatencyPrior,
) -> dict[str, str]:
    """Validate eval identity and return its sample-to-task mapping."""

    if not eval_rows:
        raise ValueError("no latency rows supplied")
    task_id_by_sample_id: dict[str, str] = {}
    eval_task_id_by_source: dict[str, str] = {}
    for index, row in enumerate(eval_rows):
        source = f"eval row {index}"
        sample_id = required_text(row, "sample_id", source=source)
        source_trace = required_text(row, "source_trace", source=source)
        task_id = _row_task_id(row, source=source)
        _record_source_task(
            eval_task_id_by_source,
            source_trace=source_trace,
            task_id=task_id,
            source=source,
        )
        existing_task_id = task_id_by_sample_id.get(sample_id)
        if existing_task_id is not None and existing_task_id != task_id:
            raise ValueError(
                f"duplicate sample_id {sample_id!r} with conflicting task_id values"
            )
        task_id_by_sample_id[sample_id] = task_id

    overlap = sorted(set(eval_task_id_by_source) & prior.source_traces)
    if overlap:
        raise ValueError(
            "profile and eval rows must come from disjoint traces; "
            f"shared source_trace values: {overlap}"
        )
    task_overlap = sorted(set(eval_task_id_by_source.values()) & prior.task_ids)
    if task_overlap:
        raise ValueError(
            "profile and eval rows must come from disjoint logical tasks; "
            f"shared task_id values: {task_overlap}"
        )
    return task_id_by_sample_id


__all__ = [
    "LatencyPrior",
    "LatencyPriorNode",
    "build_latency_prior",
    "hazard_recheck_ms",
    "latency_prior_hierarchy",
    "validate_profile_eval_disjoint",
]
