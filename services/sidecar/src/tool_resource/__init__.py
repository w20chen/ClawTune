"""Local command resource prediction and observation SDK.

Public SDK symbols are loaded lazily.  Importing an independent submodule such
as :mod:`tool_resource.telemetry` must not eagerly import NumPy-backed predictor
modules before an eBPF preflight can report its own result.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tool_resource.runtime_kb import (  # noqa: F401
        ClauseObservation,
        ClauseResourceKB,
        CommandLatencyBucketPrediction,
        LatencyBuckets,
    )
    from tool_resource.sdk import (  # noqa: F401
        ColdStartReport,
        CommandObservationToken,
        CommandResult,
        CommandRun,
        DockerCommandObserver,
        DockerExecutionContext,
        ToolResourceSDK,
    )

_EXPORTS = {
    "ClauseObservation": ("tool_resource.runtime_kb", "ClauseObservation"),
    "ClauseResourceKB": ("tool_resource.runtime_kb", "ClauseResourceKB"),
    "CommandLatencyBucketPrediction": (
        "tool_resource.runtime_kb",
        "CommandLatencyBucketPrediction",
    ),
    "LatencyBuckets": ("tool_resource.runtime_kb", "LatencyBuckets"),
    "ColdStartReport": ("tool_resource.sdk", "ColdStartReport"),
    "CommandObservationToken": ("tool_resource.sdk", "CommandObservationToken"),
    "CommandResult": ("tool_resource.sdk", "CommandResult"),
    "CommandRun": ("tool_resource.sdk", "CommandRun"),
    "DockerCommandObserver": ("tool_resource.sdk", "DockerCommandObserver"),
    "DockerExecutionContext": ("tool_resource.sdk", "DockerExecutionContext"),
    "ToolResourceSDK": ("tool_resource.sdk", "ToolResourceSDK"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve a public SDK symbol on first access and cache the result."""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
