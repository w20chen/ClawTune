"""Minimal container stats helpers required by tool_resource.labels."""

from __future__ import annotations


def _parse_memory_mb(mem_usage: str) -> float | None:
    if not mem_usage:
        return None
    left = mem_usage.split("/")[0].strip()
    units = (
        ("GiB", 1024.0),
        ("MiB", 1.0),
        ("KiB", 1.0 / 1024.0),
        ("GB", 1000.0),
        ("MB", 1.0),
        ("KB", 1.0 / 1000.0),
        ("kB", 1.0 / 1000.0),
        ("B", 1.0 / (1024.0 * 1024.0)),
    )
    for suffix, multiplier in units:
        if left.endswith(suffix):
            try:
                return float(left[: -len(suffix)]) * multiplier
            except ValueError:
                return None
    return None
