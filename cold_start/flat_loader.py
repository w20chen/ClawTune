"""Stream trace-v5 actions; never interpret prompts/results as training labels."""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from tool_resource.runtime_kb import ClauseObservation, CompletedCall


def valid(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def declared_sampling_interval_ms(resource: dict) -> float | None:
    seconds = resource.get("sample_interval_s")
    if valid(seconds) and seconds > 0:
        return float(seconds) * 1000.0
    milliseconds = resource.get("sampling_interval_ms")
    if valid(milliseconds) and milliseconds > 0:
        return float(milliseconds)
    return None


def _censored_call(data: dict) -> bool:
    """Inspect structured lifecycle metadata, never command/result text.

    Failure alone is not censoring: a completed nonzero exit still supplies a
    duration. Collector unavailability alone also does not prove truncation.
    """
    observation = data.get("resource_observation")
    timeline = data.get("resource_timeline")
    records = [data]
    if isinstance(observation, dict):
        records.append(observation)
    if isinstance(timeline, dict):
        records.append(timeline)
    for record in tuple(records):
        status = record.get("status")
        if isinstance(status, dict):
            records.append(status)
    for record in records:
        if any(record.get(flag) is True for flag in (
            "censored", "timed_out", "timeout", "cancelled", "canceled", "aborted",
            "interrupted", "protocol_timeout",
        )):
            return True
        for field in ("error_type", "outcome", "code", "reason", "unavailable_reason"):
            value = record.get(field)
            if isinstance(value, str) and any(marker in value.lower() for marker in (
                "timeout", "timed out", "cancel", "abort", "interrupt", "killed",
            )):
                return True
    return False


@dataclass
class LoadedTask:
    clauses: list[ClauseObservation] = field(default_factory=list)
    calls: list[CompletedCall] = field(default_factory=list)
    # Protocol target values aligned one-to-one with ``calls``. V5 resource
    # targets are populated only for one independently owned standalone clause.
    call_actuals: list[dict[str, float]] = field(default_factory=list)
    call_clauses: list[tuple[dict, ...]] = field(default_factory=list)
    sample_periods_ms: list[float] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)


def read_task(path: Path, *, repo: str, task_id: str, rss_unit: str,
              trust_call_cgroup: bool = False) -> LoadedTask:
    if rss_unit not in {"MB", "MiB"}:
        raise ValueError("source RSS unit must be explicitly MB or MiB")
    rss_scale = 1_000_000 if rss_unit == "MB" else 1024**2
    result = LoadedTask()
    seen = set()
    with path.open("rb") as source:
        for line_number, line in enumerate(source, 1):
            # Most bytes are LLM prompts. Do not deserialize or retain them.
            if b'"trace_metadata"' not in line and b'"tool_exec"' not in line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"{path.name}:{line_number}: invalid JSON") from exc
            if row.get("type") == "trace_metadata":
                if row.get("instance_id") != task_id:
                    raise ValueError(f"{path.name}: metadata task identity mismatch")
                result.counts["metadata_records"] += 1
                continue
            if row.get("type") != "action" or row.get("action_type") != "tool_exec":
                continue
            if row.get("instance_id", task_id) != task_id:
                raise ValueError(f"{path.name}: action task identity mismatch")
            action_id = row.get("action_id")
            if not isinstance(action_id, str) or not action_id:
                raise ValueError(f"{path.name}:{line_number}: missing action identity")
            if action_id in seen:
                raise ValueError(f"{path.name}: duplicate tool action {action_id}")
            seen.add(action_id)
            data = row.get("data", {})
            timeline = data.get("resource_timeline") or {}
            sample_interval_ms = declared_sampling_interval_ms(timeline) if isinstance(timeline, dict) else None
            if sample_interval_ms is not None:
                # dt_s contains partial first/last samples and is not the
                # configured sampling frequency. Keep one declared value per
                # call so long-running calls do not dominate the audit.
                result.sample_periods_ms.append(sample_interval_ms)
            name = data.get("tool_name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{path.name}: missing tool name")
            result.counts["tool:" + name] += 1
            args = data.get("tool_args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            command = args.get("command") if name == "exec" and isinstance(args, dict) else None
            if not isinstance(command, str):
                command = None
            censored = _censored_call(data)
            if censored:
                result.counts["censored_calls"] += 1
            duration = data.get("duration_ms")
            call_actual_index = None
            if not valid(duration):
                result.counts["invalid_call_duration"] += 1
            else:
                summary = timeline.get("summary") or {}
                cpu = summary.get("cpu_core_s")
                eligible_cpu = (trust_call_cgroup and timeline.get("source") == "cgroup_cpu_proc_net"
                                and timeline.get("scope") == "openclaw_exec_tool_interval" and valid(cpu))
                if valid(cpu) and not eligible_cpu:
                    result.counts["withheld_call_cpu_unproven_ownership"] += 1
                # Static training labels have synthetic zero-based intervals;
                # call duration and paired CPU average stay numerically exact.
                call = CompletedCall(repo, name, command, 0., duration / 1000,
                    censored=censored,
                    cpu_time_seconds=float(cpu) if eligible_cpu else None, cpu_time_eligible=eligible_cpu,
                    outcome="ok" if data.get("success") is True else "error")
                result.calls.append(call)
                result.call_actuals.append({"duration_ms": float(duration)})
                result.call_clauses.append(())
                call_actual_index = len(result.call_actuals) - 1
            observation = data.get("resource_observation")
            if not isinstance(observation, dict):
                result.counts["no_clause_observation"] += 1
                continue
            if (observation.get("tool_call_id") != data.get("tool_call_id")
                    or (command is not None and observation.get("command") != command)):
                raise ValueError(f"{path.name}:{line_number}: resource/action identity mismatch")
            if censored:
                result.counts["withheld_censored_clause_observations"] += 1
                continue
            if (observation.get("eligible_for_kb") is not True
                    or observation.get("telemetry_quality") != "ok"
                    or observation.get("telemetry_status") != "ok"):
                result.counts["ineligible_resource_observation"] += 1
                continue
            from tool_resource.features import enrich_clause_structure
            enriched_clauses = enrich_clause_structure(command, observation.get("clauses", []))
            if call_actual_index is not None:
                result.call_clauses[call_actual_index] = tuple({
                    field: clause.get(field)
                    for field in ("bin", "argv", "in_loop", "in_pipe", "in_subst", "pipeline_position")
                } for clause in enriched_clauses)
            accepted_clauses = []
            for clause in enriched_clauses:
                availability = clause.get("availability") or {}
                if clause.get("eligible_for_kb") is not True or clause.get("telemetry_quality") != "ok":
                    result.counts["ineligible_clause"] += 1
                    continue
                argv = clause.get("argv")
                if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
                    raise ValueError(f"{path.name}: invalid clause argv")
                elapsed = clause.get("latency_ms")
                elapsed = float(elapsed) if availability.get("latency") == "ok" and valid(elapsed) else None
                cpu_ns = clause.get("cpu_ns_cumulative")
                cpu_ns = int(cpu_ns) if valid(cpu_ns) else None
                peak = clause.get("peak_cpu_cores")
                profile = clause.get("cpu_window_profile") or []
                windows = [p.get("cpu_cores") for p in profile
                           if valid(p.get("span_s")) and math.isclose(p["span_s"], .5, abs_tol=1e-6)
                           and valid(p.get("cpu_cores"))]
                if not (availability.get("cpu") == "ok" and valid(peak) and windows
                        and math.isclose(max(windows), peak, rel_tol=1e-5, abs_tol=1e-6)):
                    if valid(peak):
                        result.counts["withheld_clause_peak_unverified_500ms"] += 1
                    peak = None
                memory = clause.get("sampled_peak_rss_mb")
                # ClauseObservation's existing internal field is MiB. Normalize
                # the corpus's declared unit now, before either KB consumes it.
                memory = memory * rss_scale / 1024**2 if valid(memory) and availability.get("memory") == "ok" else None
                if elapsed is None and cpu_ns is None and peak is None and memory is None:
                    continue
                accepted = ClauseObservation(repo, str(clause.get("bin") or argv[0]), tuple(argv),
                    0., (elapsed or 0.) / 1000, latency_ms=elapsed, cpu_ns_cumulative=cpu_ns,
                    peak_cpu_cores=peak, sampled_peak_rss_mb=memory,
                    in_loop=clause.get("in_loop") is True, in_pipe=clause.get("in_pipe") is True,
                    in_subst=clause.get("in_subst") is True, pipeline_position=int(clause.get("pipeline_position", -1)))
                result.clauses.append(accepted)
                accepted_clauses.append(accepted)
            if (call_actual_index is not None and len(enriched_clauses) == 1
                    and len(accepted_clauses) == 1):
                clause = accepted_clauses[0]
                actuals = result.call_actuals[call_actual_index]
                if clause.cpu_ns_cumulative is not None:
                    actuals["cpu_time_seconds"] = clause.cpu_ns_cumulative / 1e9
                    if duration > 0:
                        actuals["cpu_avg_cores"] = actuals["cpu_time_seconds"] / (duration / 1000)
                if clause.peak_cpu_cores is not None:
                    actuals["cpu_peak_cores"] = clause.peak_cpu_cores
                if clause.sampled_peak_rss_mb is not None:
                    actuals["memory_peak_rss_bytes"] = clause.sampled_peak_rss_mb * 1024**2
    if result.counts["metadata_records"] != 1:
        raise ValueError(f"{path.name}: expected one trace metadata record")
    result.counts["calls"] = len(result.calls)
    result.counts["clauses"] = len(result.clauses)
    return result
