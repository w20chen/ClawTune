"""Shared validation helpers for tool-time evaluation datasets."""

from __future__ import annotations

import math
from typing import Any, Iterable


def required_text(row: dict[str, Any], field: str, *, source: str) -> str:
    value = row.get(field)
    if value is None:
        raise ValueError(f"{source}: missing required field {field!r}")
    if not isinstance(value, str):
        raise ValueError(f"{source}: field {field!r} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{source}: empty required field {field!r}")
    return text


def required_nonnegative_float(row: dict[str, Any], field: str, *, source: str) -> float:
    value = row.get(field)
    if value is None:
        raise ValueError(f"{source}: missing required field {field!r}")
    if not isinstance(value, int | float):
        raise ValueError(f"{source}: field {field!r} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{source}: field {field!r} must be finite")
    if number < 0.0:
        raise ValueError(f"{source}: field {field!r} must be non-negative")
    return number


def normalized_positive_floats(values: Iterable[float], *, label: str) -> list[float]:
    result = sorted({float(value) for value in values})
    if not result:
        raise ValueError(f"at least one {label} is required")
    for value in result:
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{label}s must be finite and positive, got {value}")
    return result


def row_order_key(row: dict[str, Any]) -> tuple[str, float, str]:
    source_trace = required_text(
        row,
        "source_trace",
        source=f"row {row.get('sample_id', '<unknown>')}",
    )
    tool_ts_start = required_nonnegative_float(
        row,
        "tool_ts_start",
        source=f"row {row.get('sample_id', '<unknown>')}",
    )
    sample_id = required_text(row, "sample_id", source=f"row {source_trace}")
    return (source_trace, tool_ts_start, sample_id)
