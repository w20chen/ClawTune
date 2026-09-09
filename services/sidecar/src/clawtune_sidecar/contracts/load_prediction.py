"""Typed mirror of contracts/tool-decision.schema.json's callLoad definitions."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LoadTarget = Literal["duration_ms", "cpu_time_seconds", "cpu_avg_cores", "cpu_peak_cores", "memory_peak_rss_bytes"]
TARGET_UNITS = {"duration_ms": "ms", "cpu_time_seconds": "core_seconds", "cpu_avg_cores": "cores",
                "cpu_peak_cores": "cores", "memory_peak_rss_bytes": "bytes"}
TARGET_DEFINITIONS = {
    "duration_ms": "tool_hook_elapsed",
    "cpu_time_seconds": "owned_workload_cpu_time",
    "cpu_avg_cores": "owned_cpu_time_over_tool_hook_elapsed",
    "cpu_peak_cores": "owned_cpu_fixed_500ms_window_peak",
    "memory_peak_rss_bytes": "sampled_distinct_mm_rss",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LoadBuckets(StrictModel):
    edges: list[float]
    interval: Literal["left_closed_right_open"] = "left_closed_right_open"
    probabilities: list[float] | None = None

    @model_validator(mode="after")
    def valid_buckets(self) -> "LoadBuckets":
        from clawtune_sidecar.prediction_config import validate_edges
        validate_edges(self.edges)
        if self.probabilities is not None:
            if (len(self.probabilities) != len(self.edges) + 1
                    or any(p < 0 or p > 1 for p in self.probabilities)
                    or not math.isclose(sum(self.probabilities), 1.0, abs_tol=1e-9)):
                raise ValueError("bucket probabilities must have edge_count+1 entries and sum to one")
        return self


class LoadEstimate(StrictModel):
    status: Literal["available", "unavailable"]
    unit: Literal["ms", "core_seconds", "cores", "bytes"]
    metric_definition: str
    avg: float | None = Field(default=None, ge=0)
    p50: float | None = Field(default=None, ge=0)
    p90: float | None = Field(default=None, ge=0)
    buckets: LoadBuckets
    backend: Literal["runtime", "trie", "lattice"]
    method: Literal["direct", "composed", "unavailable"]
    evidence_counts: list[int] = Field(default_factory=list)
    sample_count: int = Field(default=0, ge=0)
    context: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    calibration: Literal["unvalidated"] = "unvalidated"
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def valid_estimate(self) -> "LoadEstimate":
        values = (self.avg, self.p50, self.p90, self.buckets.probabilities)
        if self.status == "available":
            if (any(v is None for v in values) or self.unavailable_reason is not None
                    or self.method == "unavailable" or self.sample_count <= 0
                    or not self.evidence_counts or any(n <= 0 for n in self.evidence_counts)):
                raise ValueError("available estimate requires all statistics and evidence")
            if self.p50 > self.p90:
                raise ValueError("p50 must not exceed p90")
        elif (any(v is not None for v in values) or not self.unavailable_reason
              or self.method != "unavailable" or self.sample_count != 0):
            raise ValueError("unavailable estimate must not carry fabricated statistics")
        return self


class CallLoadPrediction(StrictModel):
    schema_version: Literal["call_load.v1"] = "call_load.v1"
    scope: Literal["tool_call"] = "tool_call"
    lifecycle: Literal["tool_hook_interval"] = "tool_hook_interval"
    cpu_peak_window_ms: Literal[500] = 500
    quantile_method: Literal["median_p50_nearest_rank_p90"] = "median_p50_nearest_rank_p90"
    targets: dict[LoadTarget, LoadEstimate]

    @model_validator(mode="after")
    def complete_targets(self) -> "CallLoadPrediction":
        if set(self.targets) != set(TARGET_UNITS):
            raise ValueError("call prediction requires all five targets")
        for target, estimate in self.targets.items():
            if estimate.unit != TARGET_UNITS[target] or estimate.metric_definition != TARGET_DEFINITIONS[target]:
                raise ValueError("target unit or measurement definition mismatch")
        return self


class LoadDiagnostics(StrictModel):
    backends: dict[Literal["runtime", "trie", "lattice"], CallLoadPrediction]
