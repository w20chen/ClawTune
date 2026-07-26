"""Resource-value adapter for the shared empirical latency prior."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from tool_time.prior import (
    LatencyPrior,
    build_latency_prior,
    latency_prior_hierarchy,
    validate_profile_eval_disjoint,
)


def adapt_resource_rows(
    rows: Iterable[Mapping[str, Any]], value_field: str
) -> list[dict[str, Any]]:
    """Copy resource rows with ``value_field`` in the prior's numeric slot."""

    adapted: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        copied["latency_ms"] = copied[value_field]
        adapted.append(copied)
    return adapted


def build_resource_prior(
    rows: Iterable[Mapping[str, Any]],
    *,
    value_field: str,
    row_group_keys: Callable[[dict[str, Any]], tuple[str, ...]] | None = None,
) -> LatencyPrior:
    """Build the shared empirical prior over one resource field."""

    return build_latency_prior(
        adapt_resource_rows(rows, value_field), row_group_keys=row_group_keys
    )


resource_prior_hierarchy = latency_prior_hierarchy
validate_resource_profile_eval_disjoint = validate_profile_eval_disjoint


__all__ = [
    "adapt_resource_rows",
    "build_resource_prior",
    "resource_prior_hierarchy",
    "validate_resource_profile_eval_disjoint",
]
