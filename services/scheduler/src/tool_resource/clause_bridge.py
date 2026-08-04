"""Bridge Stage-2 exec-image telemetry to static mvdan-clause observations.

The Stage-2 collector's ``(host_pid, exec_seq)`` object is a runtime *exec-image
occurrence*, NOT a shell clause. One static mvdan clause may own a same-PID exec
chain AND forked/execed descendants:

    env nice -n 0 workload ...
      mvdan:   one clause, headed by ``env``
      runtime: same-PID exec chain env -> nice -> workload (+ descendants)

This bridge parses the ORIGINAL command with :func:`parse_command_clauses`,
maps runtime exec images to static clauses, and folds each static clause's owned
exec images into a single :class:`~tool_resource.runtime_kb.ClauseObservation`
keyed by the mvdan clause identity (``bin``, ordered ``argv``).

Two correctness properties this module guarantees:

**Time-aligned aggregation, never scalar max.** A static clause that owns
concurrent execed descendants must not have its metrics computed as the max of
per-image scalar peaks — that under-counts and can flip a heavy/light label.
Each exec image exports compact time-aligned profiles:

- ``cpu_windows``: ``(absolute_500ms_window_index, cpu_ns)`` contributions;
- ``rss_bins``: ``(absolute_20ms_bin_index, mm_identity, rss_mb)`` samples.

The bridge merges ALL owned images before reducing: for CPU it sums owned
``cpu_ns`` within each common wall window, divides by the window's actual span,
quota-clips, then takes the max window; for RSS it deduplicates identical ``mm``
per common aligned bin, sums distinct live ``mm`` RSS, then takes the max bin.
If a profile is missing/incompatible for an owned image, the target is returned
``unavailable`` — never a scalar-max fallback. Per-image scalar peaks are kept
only as diagnostics.

**Full-proof, ambiguity-preserving mapping.** Only a runtime chain's initial
invocation may match a static clause. Its executable and complete argv must
align uniquely with the static word intents; later same-PID execs and forked
descendants are ownership transitions. Runtime order, argv prefixes, wrapper
subsequences, and bin-only identity never break ties. Any incomplete or
ambiguous proof withholds every observation from that tool call.
"""

from __future__ import annotations

import math
import re
from fnmatch import fnmatchcase
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from tool_resource.features import (
    parse_command_clauses,
    shell_bin_requires_exec_evidence,
)
from tool_resource.runtime_kb import ClauseObservation

# Kept in sync with the Stage-2 collector's windowing constants.
_WINDOW_NS = 500_000_000
_MIN_ELIGIBLE_SPAN_NS = 1_000_000_000  # resource_timeline: clause >= 1 s
_MIN_WINDOW_SPAN_NS = 100_000_000
_MAX_CAPTURED_ARGS = 16

_SHELL_BINS = frozenset({"sh", "dash", "bash", "ash", "zsh"})
_SHELL_LOOKUP_DIAGNOSTIC = re.compile(
    r"^(?:/[^:\n]+|(?:ba|da|a|z)?sh): "
    r"(?:(?:line )?\d+): "
    r"(?P<head>[A-Za-z0-9_./+@%-]+): "
    r"(?:(?:command )?not found)$"
)
# `source` is a bash-ism: the only no-exec builtin a real POSIX sh
# (dash/ash) can report "not found" for. Every other member is mandated or
# universally built in, so a "not found" diagnostic naming it can only be
# forged payload and must keep failing closed.
_DIALECT_DEPENDENT_BUILTINS = frozenset({"source"})


def _clause_requires_exec_evidence(clause: Mapping[str, Any]) -> bool:
    argv = clause.get("argv")
    executable_head = (
        str(argv[0])
        if isinstance(argv, Sequence) and not isinstance(argv, (str, bytes)) and argv
        else None
    )
    return shell_bin_requires_exec_evidence(
        str(clause.get("bin", "")),
        executable_head,
    )


@dataclass(frozen=True)
class ExecImageRecord:
    """One runtime exec-image occurrence produced by the Stage-2 collector.

    ``cpu_windows`` / ``rss_bins`` are the time-aligned profiles the bridge
    merges; ``None`` means the profile is unavailable (forcing that target to be
    reported unavailable rather than scalar-max'd). ``peak_cpu_cores`` /
    ``sampled_peak_rss_mb`` are per-image scalars kept ONLY as diagnostics.
    """

    host_pid: int
    exec_seq: int
    t_exec_ns: int
    t_end_ns: int
    bin: str
    argv: tuple[str, ...]
    terminal: bool
    cpu_windows: tuple[tuple[int, int], ...] | None  # (abs_window_idx, cpu_ns)
    rss_bins: tuple[tuple[int, int, float], ...] | None  # (abs_bin, mm, rss_mb)
    peak_cpu_cores: float | None = None  # diagnostic only
    peak_cpu_reason: str = "ok"
    sampled_peak_rss_mb: float | None = None  # diagnostic only
    sampled_rss_reason: str = "ok"
    disk_read_bytes_total: int | None = None
    disk_write_bytes_total: int | None = None
    disk_cancelled_write_bytes_total: int | None = None
    disk_io_reason: str = "missing_disk_io"
    cpu_ns_cumulative: int = 0
    exit_signal: int | None = None
    normal_exit_status: int | None = None
    has_causal_end: bool = True  # real exit / next same-pid exec; else fail closed
    argv_capture_flags: int = 0
    requested_executable_path: str | None = None
    requested_executable_path_truncated: bool = False
    exact_argc: int | None = None
    argv_capped: bool = False
    truncated_words: tuple[int, ...] = ()
    bprm_filename: str | None = None
    bprm_interp: str | None = None
    bprm_evidence_truncated: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FailedExecAttempt:
    """One execve/execveat syscall that returned an errno without a new image."""

    host_pid: int
    exec_seq: int
    ts_ns: int
    argv: tuple[str, ...]
    errno: int
    argv_capture_flags: int = 0


@dataclass(frozen=True)
class ShellCommandLookupFailure:
    """Strict shell command-not-found evidence.

    Historical offline replay required an exact source/replay agreement.  Live
    managed-wrapper execution has no separate source action: the launcher
    result is the source of truth.  ``evidence_mode`` distinguishes those
    protocols so live evidence is not made to impersonate a replay pair.
    """

    executable_head: str
    command: str
    source_tool_call_id: str
    replay_tool_call_id: str
    source_exit_code: int
    replay_exit_code: int
    source_diagnostic: str
    replay_diagnostic: str
    source_channel: str
    replay_channel: str
    parser: str
    exit_code_semantics: str
    evidence_mode: str = "source_replay"


@dataclass(frozen=True)
class SafetyGuardBlockEvidence:
    """Source/replay-agreed rejection before any shell process was started."""

    command: str
    source_command: str
    source_tool_call_id: str
    replay_tool_call_id: str
    source_result: str
    replay_result: str


@dataclass(frozen=True)
class MappingGap:
    kind: str  # unmatched_exec_image | unmatched_static_clause | ambiguous
    detail: str


@dataclass(frozen=True)
class BridgedClause:
    observation: ClauseObservation
    owned_pids: tuple[int, ...]
    owned_exec_images: tuple[tuple[int, int], ...]
    mapping_evidence: str
    disk_read_bytes_total: int | None
    disk_write_bytes_total: int | None
    disk_cancelled_write_bytes_total: int | None
    status: dict[str, Any]
    availability: dict[str, str]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class NoRuntimeExec:
    """A static clause resolved to explicit non-runtime evidence."""

    bin: str
    argv: tuple[str, ...]
    mapping_evidence: str
    attempts: tuple[FailedExecAttempt, ...] = ()
    command_lookup_failure: ShellCommandLookupFailure | None = None
    control_short_circuit: Mapping[str, Any] | None = None
    safety_guard_blocked: SafetyGuardBlockEvidence | None = None
    availability: dict[str, str] = field(
        default_factory=lambda: {
            "latency": "unknown:no_runtime_exec",
            "cpu": "unknown:no_runtime_exec",
            "memory": "unknown:no_runtime_exec",
            "disk_io": "unknown:no_runtime_exec",
            "status": "unknown:no_runtime_exec",
        }
    )


@dataclass(frozen=True)
class BridgeResult:
    bridged: list[BridgedClause]
    no_runtime_exec: list[NoRuntimeExec]
    coverage_gaps: list[MappingGap]
    unobserved_builtins: list[str]
    static_clause_count: int
    data_valid: bool
    invalid_reasons: list[MappingGap]
    transition_graph: list[dict[str, Any]] = field(default_factory=list)
    candidate_rejections: list[dict[str, Any]] = field(default_factory=list)
    static_clauses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def observations(self) -> list[ClauseObservation]:
        if not self.data_valid:
            return []
        return [
            bc.observation
            for bc in self.bridged
            if not all(
                reason == "unknown:protocol_timeout"
                for reason in bc.availability.values()
            )
        ]


# --------------------------------------------------------------------------
# Time-aligned aggregation
# --------------------------------------------------------------------------


_MIN_RSS_SAMPLES = 2  # fail closed on insufficient merged RSS coverage


def _merge_cpu(
    owned: Sequence[ExecImageRecord], t_exec: int, t_end: int, quota: float | None
) -> tuple[float | None, str]:
    if any(i.cpu_windows is None for i in owned):
        return None, "missing_cpu_profile"
    # Quota must be present, finite, and positive; clipping is meaningless
    # otherwise. No inf fallback.
    if quota is None or not math.isfinite(quota) or quota <= 0.0:
        return None, "missing_or_inconsistent_quota"
    if (t_end - t_exec) < _MIN_ELIGIBLE_SPAN_NS:
        return None, "clause_shorter_than_1s_ineligible_for_peak"
    merged: dict[int, int] = {}
    for img in owned:
        for widx, cpu_ns in img.cpu_windows or ():
            if not isinstance(widx, int) or not isinstance(cpu_ns, int) or cpu_ns < 0:
                return None, "invalid_cpu_profile"
            merged[widx] = merged.get(widx, 0) + cpu_ns
    if not merged:
        return None, "insufficient_cpu_samples"
    peak: float | None = None
    for widx, cpu_ns in merged.items():
        win_start = widx * _WINDOW_NS
        lo = max(win_start, t_exec)
        hi = min(win_start + _WINDOW_NS, t_end)
        span = hi - lo
        if span < _MIN_WINDOW_SPAN_NS:
            continue
        rate = min(cpu_ns / span, quota)
        peak = rate if peak is None else max(peak, rate)
    if peak is None:  # every merged window was too short to time -> no 0/ok
        return None, "no_eligible_merged_window"
    if not math.isfinite(peak):
        return None, "non_finite_cpu"
    return peak, "ok"


def _merge_rss(owned: Sequence[ExecImageRecord]) -> tuple[float | None, str]:
    """Max over time of the summed RSS of concurrently-LIVE distinct mm.

    Each mm's RSS is held between its samples over its observed lifetime
    ``[min_bin, max_bin]``; at each aligned bin only mm whose lifetime spans that
    bin contribute. This prevents summing mm whose lifetimes do not overlap
    (sequential peaks in adjacent bins are never added into one figure).
    """

    if any(i.rss_bins is None for i in owned):
        return None, "missing_rss_profile"
    # mm -> {bin: rss_mb}
    per_mm: dict[int, dict[int, float]] = {}
    n_samples = 0
    for img in owned:
        for bidx, mm, rss_mb in img.rss_bins or ():
            if (
                not isinstance(bidx, int)
                or not isinstance(mm, int)
                or not math.isfinite(rss_mb)
                or rss_mb < 0.0
            ):
                return None, "invalid_rss_profile"
            slot = per_mm.setdefault(mm, {})
            slot[bidx] = max(slot.get(bidx, 0.0), rss_mb)
            n_samples += 1
    if n_samples < _MIN_RSS_SAMPLES:
        return None, "insufficient_rss_samples"
    all_bins = sorted({b for slots in per_mm.values() for b in slots})
    totals: dict[int, float] = dict.fromkeys(all_bins, 0.0)
    for slots in per_mm.values():
        sbins = sorted(slots)
        lo, hi = sbins[0], sbins[-1]  # this mm's observed lifetime
        held, j = 0.0, 0
        for b in all_bins:
            if b < lo or b > hi:
                continue  # mm not alive at bin b -> not summed
            while j < len(sbins) and sbins[j] <= b:
                held = slots[sbins[j]]
                j += 1
            totals[b] += held
    peak = max(totals.values())
    if not math.isfinite(peak):
        return None, "non_finite_rss"
    return peak, "ok"


def _merge_disk_io(
    owned: Sequence[ExecImageRecord],
) -> tuple[tuple[int, int, int] | None, str]:
    fields = (
        "disk_read_bytes_total",
        "disk_write_bytes_total",
        "disk_cancelled_write_bytes_total",
    )
    if any(getattr(image, field) is None for image in owned for field in fields):
        reasons = sorted(
            {
                image.disk_io_reason
                for image in owned
                if any(getattr(image, field) is None for field in fields)
            }
        )
        return None, "owned_image_unavailable:" + ",".join(reasons)
    values = tuple(
        sum(int(getattr(image, field)) for image in owned) for field in fields
    )
    if any(value < 0 for value in values):
        return None, "invalid_negative_disk_io"
    return values, "ok"


# --------------------------------------------------------------------------
# Evidence-prioritized staged matching
# --------------------------------------------------------------------------


def parse_shell_lookup_diagnostic(line: str) -> str | None:
    """Return the exact missing executable head from one anchored shell line."""

    match = _SHELL_LOOKUP_DIAGNOSTIC.fullmatch(line)
    return match.group("head") if match is not None else None


def _lookup_exit_semantics(
    static: Sequence[Mapping[str, Any]],
    executable_head: str,
    exit_code: int,
) -> str | None:
    if exit_code == 127:
        return "direct_command_not_found_127"
    if exit_code != 0:
        return None
    candidates = [
        index
        for index, clause in enumerate(static)
        if clause.get("argv") and clause["argv"][0] == executable_head
    ]
    if len(candidates) != 1:
        return None
    index = candidates[0]
    clause = static[index]
    if index + 1 >= len(static) or not clause.get("in_pipe"):
        return None
    next_clause = static[index + 1]
    if (
        not next_clause.get("in_pipe")
        or int(next_clause.get("pipeline_position", -1))
        != int(clause.get("pipeline_position", -1)) + 1
    ):
        return None
    return "nonfinal_pipeline_masked_0"


def shell_lookup_exit_semantics(
    command: str,
    executable_head: str,
    exit_code: int,
) -> str | None:
    """Classify the only accepted source/replay exit-code semantics."""

    parsed = parse_command_clauses(command)
    if parsed["parse_failed"]:
        return None
    return _lookup_exit_semantics(parsed["clauses"], executable_head, exit_code)


def _valid_lookup_failure(
    evidence: ShellCommandLookupFailure,
    command: str,
    static: Sequence[Mapping[str, Any]],
) -> bool:
    source_head = parse_shell_lookup_diagnostic(evidence.source_diagnostic)
    replay_head = parse_shell_lookup_diagnostic(evidence.replay_diagnostic)
    expected_semantics = _lookup_exit_semantics(
        static,
        evidence.executable_head,
        evidence.replay_exit_code,
    )
    common = (
        bool(evidence.replay_tool_call_id)
        and evidence.command == command
        and replay_head == evidence.executable_head
        and evidence.replay_channel in {"raw_stderr", "tool_result"}
        and evidence.parser == "anchored_shell_command_not_found_v1"
        and expected_semantics is not None
        and evidence.exit_code_semantics == expected_semantics
    )
    if evidence.evidence_mode == "live_execution":
        return (
            common
            and not evidence.source_tool_call_id
            and not evidence.source_diagnostic
            and evidence.source_channel == "unavailable"
        )
    return (
        common
        and evidence.evidence_mode == "source_replay"
        and bool(evidence.source_tool_call_id)
        and evidence.source_exit_code == evidence.replay_exit_code
        and source_head == replay_head
        and evidence.source_channel == "source_tool_result"
    )


@dataclass(frozen=True)
class _ControlState:
    status: int
    controller_clause_index: int
    controller_pid: int | None
    controller_exec_seq: int | None
    controller_evidence: str = "mapped_exec_image"
    edge_path: tuple[int, ...] = ()


def _resolve_control_short_circuits(
    static: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    assigned: Mapping[int, int],
    evidence: Mapping[int, str],
    chains: Mapping[int, Sequence[ExecImageRecord]],
    lookup_assigned: Mapping[int, ShellCommandLookupFailure],
    excluded: set[int],
) -> tuple[dict[int, Mapping[str, Any]], list[MappingGap]]:
    """Resolve parser-proven short-circuits from exact controller evidence."""

    leaf_states: dict[int, _ControlState] = {}
    for clause_index, pid in assigned.items():
        chain = chains[pid]
        terminal = chain[-1]
        if (
            evidence.get(clause_index) != "interchangeable_identical"
            and terminal.terminal
            and terminal.has_causal_end
            and terminal.exit_signal is None
            and terminal.normal_exit_status is not None
        ):
            leaf_states[clause_index] = _ControlState(
                status=terminal.normal_exit_status,
                controller_clause_index=clause_index,
                controller_pid=pid,
                controller_exec_seq=terminal.exec_seq,
            )
    for clause_index, failure in lookup_assigned.items():
        if failure.exit_code_semantics == "direct_command_not_found_127":
            leaf_states[clause_index] = _ControlState(
                status=127,
                controller_clause_index=clause_index,
                controller_pid=None,
                controller_exec_seq=None,
                controller_evidence="shell_command_lookup_failure_exact_head",
            )

    edge_states: dict[int, _ControlState] = {}
    resolved: dict[int, Mapping[str, Any]] = {}
    gaps: list[MappingGap] = []

    def operand_state(operand: Mapping[str, Any]) -> _ControlState | None:
        if operand["kind"] == "clause":
            if (
                operand["negated"]
                or operand["contains_pipeline"]
                or operand["contains_subshell"]
            ):
                return None
            return leaf_states.get(int(operand["index"]))
        if operand["kind"] == "edge":
            return edge_states.get(int(operand["index"]))
        return None

    for edge in edges:
        edge_id = int(edge["id"])
        lhs = operand_state(edge["lhs"])
        if lhs is None:
            rhs = operand_state(edge["rhs"])
            if rhs is not None:
                edge_states[edge_id] = replace(
                    rhs,
                    edge_path=(*rhs.edge_path, edge_id),
                )
            continue
        short_circuited = (edge["operator"] == "&&" and lhs.status != 0) or (
            edge["operator"] == "||" and lhs.status == 0
        )
        if not short_circuited:
            rhs = operand_state(edge["rhs"])
            if rhs is not None:
                edge_states[edge_id] = replace(
                    rhs,
                    edge_path=(*rhs.edge_path, edge_id),
                )
            continue

        rhs_indices = [int(index) for index in edge["rhs"]["clause_indices"]]
        if any(index in assigned or index in excluded for index in rhs_indices):
            gaps.append(
                MappingGap(
                    "control_flow_contradiction",
                    f"control edge {edge_id} {edge['operator']} short-circuits "
                    "an RHS clause with runtime or conflicting evidence",
                )
            )
        else:
            executable_rhs_indices = [
                index
                for index in rhs_indices
                if _clause_requires_exec_evidence(static[index])
            ]
            for index in executable_rhs_indices:
                if index in resolved:
                    continue
                resolved[index] = {
                    "parser": "mvdan.cc/sh/v3",
                    "control_edge_id": edge_id,
                    "control_edge_path": [*lhs.edge_path, edge_id],
                    "operator": edge["operator"],
                    "controller_clause_index": lhs.controller_clause_index,
                    "controller_bin": static[lhs.controller_clause_index]["bin"],
                    "controller_pid": lhs.controller_pid,
                    "controller_exec_seq": lhs.controller_exec_seq,
                    "controller_mapping_evidence": lhs.controller_evidence,
                    "controller_normal_exit_status": lhs.status,
                    "controlled_clause_index": index,
                    "controlled_rhs_clause_indices": rhs_indices,
                    "controlled_rhs_executable_clause_indices": (
                        executable_rhs_indices
                    ),
                    "controlled_rhs_subtree": dict(edge["rhs"]),
                    "source_replay_fidelity": ("exact_tool_result_and_exit_code"),
                }
        edge_states[edge_id] = _ControlState(
            status=lhs.status,
            controller_clause_index=lhs.controller_clause_index,
            controller_pid=lhs.controller_pid,
            controller_exec_seq=lhs.controller_exec_seq,
            controller_evidence=lhs.controller_evidence,
            edge_path=(*lhs.edge_path, edge_id),
        )
    return resolved, gaps


def _pathname_match(value: str, pattern: str) -> bool:
    """Shell `*` and `?` do not cross `/` without a recursive-glob option."""

    if value == pattern:
        return True
    if "**" in pattern:
        return False
    value_parts = value.split("/")
    pattern_parts = pattern.split("/")
    return len(value_parts) == len(pattern_parts) and all(
        (not value_part.startswith(".") or pattern_part.startswith("."))
        and fnmatchcase(value_part, pattern_part)
        for value_part, pattern_part in zip(value_parts, pattern_parts, strict=True)
    )


def _alignment_evidence(
    clause: Mapping[str, Any],
    initial: ExecImageRecord,
) -> tuple[str | None, str]:
    """Prove one full static-word to initial-runtime-argv alignment."""

    if _incomplete_capture(initial):
        return None, "runtime_argv_incomplete"
    runtime = tuple(initial.argv)
    static = tuple(str(word) for word in clause["argv"])
    if not runtime or not static:
        return None, "empty_argv"
    if runtime[0] != static[0]:
        return None, "executable_head_mismatch"
    if "/" in static[0] and initial.requested_executable_path != static[0]:
        return None, "requested_executable_path_mismatch"

    intents = clause.get("word_intents")
    if not isinstance(intents, list) or len(intents) != len(static):
        return (
            ("initial_invocation_exact", "ok")
            if runtime == static
            else (None, "word_intent_unavailable")
        )
    for intent in intents:
        components = intent.get("components")
        if not isinstance(components, list):
            return None, "word_intent_unavailable"
        kinds = {component.get("kind") for component in components}
        if kinds - {"literal", "pathname_expansion"}:
            return None, "unsupported_dynamic_expansion"
        if any(
            component.get("kind") == "pathname_expansion"
            and (component.get("quoted") or component.get("escaped"))
            for component in components
        ):
            return None, "invalid_quoted_pathname_expansion"

    alignments: list[tuple[tuple[int, int], ...]] = []

    def align(
        static_index: int,
        runtime_index: int,
        spans: tuple[tuple[int, int], ...],
    ) -> None:
        if len(alignments) > 1:
            return
        if static_index == len(intents):
            if runtime_index == len(runtime):
                alignments.append(spans)
            return
        intent = intents[static_index]
        components = intent["components"]
        is_glob = any(
            component["kind"] == "pathname_expansion"
            for component in components
        )
        if not is_glob:
            if (
                runtime_index < len(runtime)
                and runtime[runtime_index] == intent["cooked"]
            ):
                align(
                    static_index + 1,
                    runtime_index + 1,
                    (*spans, (runtime_index, runtime_index + 1)),
                )
            return
        pattern = str(intent["cooked"])
        for end in range(runtime_index + 1, len(runtime) + 1):
            if all(_pathname_match(word, pattern) for word in runtime[runtime_index:end]):
                align(
                    static_index + 1,
                    end,
                    (*spans, (runtime_index, end)),
                )

    align(0, 0, ())
    if len(alignments) != 1:
        return (
            None,
            "ambiguous_expansion_alignment"
            if alignments
            else "no_full_argv_alignment",
        )
    expanded = any(end - start != 1 for start, end in alignments[0])
    return (
        "initial_invocation_unique_expansion"
        if expanded or runtime != static
        else "initial_invocation_exact",
        "ok",
    )


def _incomplete_capture(image: ExecImageRecord) -> bool:
    return bool(
        image.argv_capture_flags
        or image.argv_capped
        or image.truncated_words
        or image.requested_executable_path_truncated
        or image.bprm_evidence_truncated
    )


def _components(
    statics: Sequence[int],
    chains: Sequence[int],
    tier: Mapping[tuple[int, int], object],
) -> list[tuple[list[int], list[int]]]:
    """Connected components of the static<->chain candidate bipartite graph."""

    adj: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for si in statics:
        adj.setdefault(("s", si), [])
    for pid in chains:
        adj.setdefault(("c", pid), [])
    for si, pid in tier:
        if si in statics and pid in chains:
            adj[("s", si)].append(("c", pid))
            adj[("c", pid)].append(("s", si))
    seen: set[tuple[str, int]] = set()
    comps: list[tuple[list[int], list[int]]] = []
    for node in adj:
        if node in seen:
            continue
        stack = [node]
        seen.add(node)
        cs: list[int] = []
        cp: list[int] = []
        while stack:
            kind, ident = stack.pop()
            (cs if kind == "s" else cp).append(ident)
            for nb in adj[(kind, ident)]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if cs and cp:  # ignore isolated nodes (no candidates)
            comps.append((cs, cp))
    return comps


def _static_exchange_identity(clause: Mapping[str, Any]) -> tuple[object, ...]:
    intents = clause.get("word_intents")
    word_identity = (
        tuple(
            (
                intent.get("cooked"),
                intent.get("source"),
                bool(intent.get("quoted")),
                bool(intent.get("escaped")),
                tuple(
                    (
                        component.get("kind"),
                        component.get("source"),
                        bool(component.get("quoted")),
                        bool(component.get("escaped")),
                    )
                    for component in intent.get("components", ())
                ),
            )
            for intent in intents
        )
        if isinstance(intents, list)
        else ()
    )
    return (
        tuple(clause["argv"]),
        str(clause.get("original", "")),
        bool(clause.get("in_loop")),
        bool(clause.get("in_pipe")),
        bool(clause.get("in_subst")),
        int(clause.get("pipeline_position", -1)),
        tuple(clause.get("structural_context", ())),
        word_identity,
    )


def _exchange_context_supported(clause: Mapping[str, Any]) -> bool:
    contexts = clause.get("structural_context")
    if not isinstance(contexts, list):
        return False
    for context in contexts:
        parts = str(context).split(":")
        if len(parts) != 3 or parts[0] != "binary":
            return False
        operator, side = parts[1:]
        if operator in {"&&", "||"}:
            continue
        if operator in {"|", "|&"} and side == "rhs":
            continue
        return False
    return True


def _control_consumer_identity(
    clause_index: int,
    statics: Mapping[int, Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> tuple[object, ...]:
    edge_by_id = {int(edge["id"]): edge for edge in edges}
    referenced = {
        int(operand["index"])
        for edge in edges
        for operand in (edge["lhs"], edge["rhs"])
        if operand["kind"] == "edge"
    }

    def operand_identity(operand: Mapping[str, Any]) -> tuple[object, ...]:
        if operand["kind"] == "edge":
            value = edge_identity(edge_by_id[int(operand["index"])])
        elif operand["kind"] == "clause":
            index = int(operand["index"])
            value = (
                ("target",)
                if index == clause_index
                else ("clause", _static_exchange_identity(statics[index]))
            )
        else:
            value = tuple(
                ("target",)
                if int(index) == clause_index
                else ("clause", _static_exchange_identity(statics[int(index)]))
                for index in operand["clause_indices"]
            )
        return (
            str(operand["kind"]),
            bool(operand["negated"]),
            bool(operand["contains_pipeline"]),
            bool(operand["contains_subshell"]),
            value,
        )

    def edge_identity(edge: Mapping[str, Any]) -> tuple[object, ...]:
        return (
            str(edge["operator"]),
            operand_identity(edge["lhs"]),
            operand_identity(edge["rhs"]),
        )

    return tuple(
        edge_identity(edge)
        for edge in edges
        if int(edge["id"]) not in referenced
        and clause_index
        in {
            int(index)
            for operand in (edge["lhs"], edge["rhs"])
            for index in operand["clause_indices"]
        }
    )


def _assign(
    statics: Mapping[int, Mapping[str, Any]],
    chains: Mapping[int, list[ExecImageRecord]],
    control_edges: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[int, int],
    dict[int, str],
    set[int],
    list[dict[str, Any]],
]:
    """Return (static_idx -> chain_pid, evidence, ambiguous static indices)."""

    candidates: dict[tuple[int, int], str] = {}
    rejections: list[dict[str, Any]] = []
    for si, clause in statics.items():
        for pid, imgs in chains.items():
            label, reason = _alignment_evidence(clause, imgs[0])
            if label is not None:
                candidates[(si, pid)] = label
            else:
                rejections.append(
                    {
                        "static_clause_index": si,
                        "runtime_root_pid": pid,
                        "reason": reason,
                    }
                )

    assigned: dict[int, int] = {}
    used: set[int] = set()
    evidence: dict[int, str] = {}
    changed = True
    while changed:
        changed = False
        for si in statics:
            if si in assigned:
                continue
            opts = [
                pid
                for pid in chains
                if pid not in used and (si, pid) in candidates
            ]
            if len(opts) != 1:
                continue
            pid = opts[0]
            claimants = [
                sj
                for sj in statics
                if sj not in assigned and (sj, pid) in candidates
            ]
            if len(claimants) == 1:
                assigned[si] = pid
                used.add(pid)
                evidence[si] = candidates[(si, pid)]
                changed = True

    ambiguous: set[int] = set()
    rem_statics = [
        si
        for si in statics
        if si not in assigned
        and any(pid not in used and (si, pid) in candidates for pid in chains)
    ]
    rem_chains = [pid for pid in chains if pid not in used]
    for cs, cp in _components(rem_statics, rem_chains, candidates):
        identities = {
            (
                _static_exchange_identity(statics[si]),
                _control_consumer_identity(si, statics, control_edges),
            )
            for si in cs
        }
        if (
            len(identities) == 1
            and len(cs) == len(cp)
            and all(_exchange_context_supported(statics[si]) for si in cs)
        ):
            # Pairing is serialization only: semantic exchangeability, not PID
            # or runtime order, is the proof that every pairing is equivalent.
            for si, pid in zip(sorted(cs), sorted(cp), strict=False):
                assigned[si] = pid
                used.add(pid)
                evidence[si] = "interchangeable_identical"
        else:
            ambiguous.update(cs)
    return assigned, evidence, ambiguous, rejections


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def bridge_command(
    repo: str,
    command: str,
    exec_images: Sequence[ExecImageRecord],
    *,
    failed_exec_attempts: Sequence[FailedExecAttempt] = (),
    command_lookup_failure: ShellCommandLookupFailure | None = None,
    safety_guard_blocked: SafetyGuardBlockEvidence | None = None,
    allow_control_short_circuit: bool = False,
    entry_pid: int,
    fork_parent: Mapping[int, int],
    epoch_offset: float = 0.0,
    loss_count: int = 0,
    attribution_gap_count: int = 0,
    protocol_timeout: bool = False,
) -> BridgeResult:
    """Map exec images to static mvdan clauses and aggregate per clause.

    Fails closed: a ``parse_failed`` command or a run with ``loss_count > 0``
    (any collector loss makes the event stream untrustworthy) yields NO usable
    KB observations — the clauses become coverage gaps instead.
    """

    parsed = parse_command_clauses(command)
    static = parsed["clauses"]

    if parsed["parse_failed"] or loss_count > 0:
        reason = "parse_failed" if parsed["parse_failed"] else "nonzero_loss"
        return BridgeResult(
            bridged=[],
            no_runtime_exec=[],
            coverage_gaps=[
                MappingGap(reason, f"{reason}: run withheld from KB ({command!r})")
            ],
            unobserved_builtins=[],
            static_clause_count=len(static),
            data_valid=False,
            invalid_reasons=[
                MappingGap(reason, f"{reason}: run withheld from KB ({command!r})")
            ],
            static_clauses=[dict(clause) for clause in static],
        )

    chains: dict[int, list[ExecImageRecord]] = {}
    for img in exec_images:
        chains.setdefault(img.host_pid, []).append(img)
    for chain in chains.values():
        chain.sort(key=lambda r: r.exec_seq)

    children: dict[int, list[int]] = {}
    for child, parent in fork_parent.items():
        children.setdefault(parent, []).append(child)

    statics = {si: c for si, c in enumerate(static)}
    static_identities = {
        si: (str(c["bin"]), tuple(str(word) for word in c["argv"]))
        for si, c in statics.items()
    }

    def is_shell(pid: int) -> bool:
        if pid not in chains or not all(img.bin in _SHELL_BINS for img in chains[pid]):
            return False
        # Ignore orchestration shells, but preserve an explicitly requested
        # shell clause (for example ``bash installer.sh``). Bin-only evidence is
        # intentionally insufficient here: the outer ``sh -c <command>`` often
        # shares the same bin and must remain structural.
        return not any(
            _alignment_evidence(clause, chains[pid][0])[0] is not None
            for clause in statics.values()
        )

    def nearest_nonstructural_ancestor(pid: int) -> int | None:
        cur = fork_parent.get(pid)
        while cur is not None and cur != entry_pid:
            if cur in chains and not is_shell(cur):
                return cur
            cur = fork_parent.get(cur)
        return None

    top_level = {
        pid: chains[pid]
        for pid in chains
        if not is_shell(pid) and nearest_nonstructural_ancestor(pid) is None
    }
    candidate_chains = {
        pid: chain for pid, chain in chains.items() if not is_shell(pid)
    }
    assigned, evidence, ambiguous, candidate_rejections = _assign(
        statics, candidate_chains, parsed["control_edges"]
    )
    mapped_roots = set(assigned.values())
    failed_by_identity: dict[tuple[str, tuple[str, ...]], list[FailedExecAttempt]] = {}
    for attempt in failed_exec_attempts:
        normalized = tuple(attempt.argv)
        if normalized and attempt.argv_capture_flags == 0:
            failed_by_identity.setdefault(
                (normalized[0].rsplit("/", 1)[-1], normalized), []
            ).append(attempt)
    failed_static_candidates: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    for si, identity in static_identities.items():
        if (
            si not in assigned
            and si not in ambiguous
            and _clause_requires_exec_evidence(statics[si])
        ):
            failed_static_candidates.setdefault(identity, []).append(si)
    failed_assigned = {
        indices[0]: tuple(failed_by_identity[identity])
        for identity, indices in failed_static_candidates.items()
        if len(indices) == 1 and identity in failed_by_identity
    }
    lookup_assigned: dict[int, ShellCommandLookupFailure] = {}
    if command_lookup_failure is not None and _valid_lookup_failure(
        command_lookup_failure,
        command,
        static,
    ):
        lookup_candidates = [
            si
            for si, clause in enumerate(static)
            if clause.get("argv")
            if si not in assigned
            and si not in ambiguous
            and si not in failed_assigned
            and clause["argv"][0] == command_lookup_failure.executable_head
            and (
                _clause_requires_exec_evidence(clause)
                or str(clause["bin"]) in _DIALECT_DEPENDENT_BUILTINS
            )
        ]
        if len(lookup_candidates) == 1:
            lookup_assigned[lookup_candidates[0]] = command_lookup_failure
    guard_assigned: dict[int, SafetyGuardBlockEvidence] = {}
    if (
        safety_guard_blocked is not None
        and not exec_images
        and safety_guard_blocked.command == command
        and safety_guard_blocked.source_command == command
        and bool(safety_guard_blocked.source_tool_call_id)
        and bool(safety_guard_blocked.replay_tool_call_id)
        and safety_guard_blocked.source_result == safety_guard_blocked.replay_result
        and safety_guard_blocked.replay_result.startswith(
            "Error: Command blocked by safety guard ("
        )
    ):
        guard_assigned = {
            si: safety_guard_blocked
            for si, clause in enumerate(static)
            if _clause_requires_exec_evidence(clause)
        }
    control_assigned, control_gaps = (
        _resolve_control_short_circuits(
            static,
            parsed["control_edges"],
            assigned,
            evidence,
            chains,
            lookup_assigned,
            {
                *ambiguous,
                *failed_assigned,
                *lookup_assigned,
                *guard_assigned,
            },
        )
        if allow_control_short_circuit
        else ({}, [])
    )

    bridged: list[BridgedClause] = []
    no_runtime_exec: list[NoRuntimeExec] = []
    gaps: list[MappingGap] = list(control_gaps)
    unobserved: list[str] = []
    owned_all: set[int] = set()

    for si, clause in enumerate(static):
        cbin = str(clause["bin"])
        if si in assigned:
            owned_pids = _owned_pids(assigned[si], children, mapped_roots)
            owned_images = [img for pid in owned_pids for img in chains.get(pid, [])]
            owned_all.update(owned_pids)  # consumed either way (obs or fail-closed)
            if any(not img.has_causal_end for img in owned_images):
                gaps.append(
                    MappingGap(
                        "no_causal_end",
                        f"clause {si} bin={cbin!r}: an owned exec image has no "
                        "real exit/causal end; withheld from KB",
                    )
                )
            else:
                bridged.append(
                    _aggregate(
                        repo,
                        clause,
                        owned_pids,
                        owned_images,
                        evidence[si],
                        epoch_offset,
                        protocol_timeout_terminated=(
                            protocol_timeout
                            and any(
                                image.exit_signal is not None for image in owned_images
                            )
                        ),
                    )
                )
        elif si in ambiguous:
            gaps.append(
                MappingGap(
                    "ambiguous",
                    f"clause {si} bin={cbin!r} argv={list(clause['argv'])} "
                    "has multiple equally-valid runtime chains",
                )
            )
        elif si in failed_assigned:
            no_runtime_exec.append(
                NoRuntimeExec(
                    bin=cbin,
                    argv=tuple(clause["argv"]),
                    mapping_evidence="failed_exec_exact",
                    attempts=failed_assigned[si],
                )
            )
        elif si in lookup_assigned:
            no_runtime_exec.append(
                NoRuntimeExec(
                    bin=cbin,
                    argv=tuple(clause["argv"]),
                    mapping_evidence="shell_command_lookup_failure_exact_head",
                    command_lookup_failure=lookup_assigned[si],
                )
            )
        elif si in guard_assigned:
            no_runtime_exec.append(
                NoRuntimeExec(
                    bin=cbin,
                    argv=tuple(clause["argv"]),
                    mapping_evidence="safety_guard_blocked_before_runtime",
                    safety_guard_blocked=guard_assigned[si],
                )
            )
        elif si in control_assigned:
            no_runtime_exec.append(
                NoRuntimeExec(
                    bin=cbin,
                    argv=tuple(clause["argv"]),
                    mapping_evidence="shell_control_short_circuit",
                    control_short_circuit=control_assigned[si],
                )
            )
        elif not _clause_requires_exec_evidence(clause):
            unobserved.append(cbin)
        else:
            gaps.append(
                MappingGap(
                    "unmatched_static_clause",
                    f"clause {si} bin={cbin!r} argv={list(clause['argv'])}",
                )
            )

    used_pids = set(assigned.values())
    # A chain is "unmatched_exec_image" only if it had NO candidate static clause;
    # a chain that matched but lost to ambiguity is already covered by the
    # ambiguous static-clause gap and must not be double-reported.
    chains_with_candidate = {
        pid
        for pid, imgs in candidate_chains.items()
        if any(
            _alignment_evidence(clause, imgs[0])[0] is not None
            for clause in statics.values()
        )
    }
    for pid in top_level:
        if (
            pid not in used_pids
            and pid not in owned_all
            and pid not in chains_with_candidate
        ):
            gaps.append(
                MappingGap(
                    "unmatched_exec_image",
                    f"first-level pid={pid} bin={chains[pid][0].bin!r} "
                    "matched no static clause",
                )
            )

    mapping_anchors = {
        (pid, chains[pid][0].exec_seq)
        for pid in assigned.values()
    }
    owned_exec_images = {
        image
        for clause in bridged
        for image in clause.owned_exec_images
    }
    ownership_only_images = owned_exec_images - mapping_anchors
    incomplete_capture_gaps = [
        MappingGap(
            "runtime_argv_incomplete",
            f"pid={image.host_pid} exec_seq={image.exec_seq} has capped or "
            "truncated argv",
        )
        for image in exec_images
        if _incomplete_capture(image)
        and (image.host_pid, image.exec_seq) not in ownership_only_images
    ]
    gaps.extend(incomplete_capture_gaps)
    transition_graph = [
        {
            "kind": "same_pid_exec",
            "from": [chain[index - 1].host_pid, chain[index - 1].exec_seq],
            "to": [image.host_pid, image.exec_seq],
        }
        for chain in chains.values()
        for index, image in enumerate(chain)
        if index
    ]
    transition_graph.extend(
        {
            "kind": "interpreter",
            "exec_image": [image.host_pid, image.exec_seq],
            "from": image.bprm_filename,
            "to": image.bprm_interp,
        }
        for image in exec_images
        if image.bprm_filename
        and image.bprm_interp
        and image.bprm_filename != image.bprm_interp
    )
    transition_graph.extend(
        {
            "kind": "fork",
            "parent_pid": parent,
            "child_pid": child,
        }
        for child, parent in fork_parent.items()
    )
    attribution_reasons = [
        MappingGap(
            "attribution_gap",
            f"runtime attribution has {attribution_gap_count} relevant gap(s)",
        )
    ] if attribution_gap_count else []
    return BridgeResult(
        bridged=bridged,
        no_runtime_exec=no_runtime_exec,
        coverage_gaps=gaps,
        unobserved_builtins=unobserved,
        static_clause_count=len(static),
        data_valid=not gaps and not attribution_reasons,
        invalid_reasons=[*gaps, *attribution_reasons],
        transition_graph=transition_graph,
        candidate_rejections=candidate_rejections,
        static_clauses=[dict(clause) for clause in static],
    )


def _owned_pids(
    root_pid: int, children: Mapping[int, Sequence[int]], mapped_roots: set[int]
) -> tuple[int, ...]:
    owned = [root_pid]
    frontier = [root_pid]
    while frontier:
        nxt: list[int] = []
        for pid in frontier:
            for child in children.get(pid, ()):
                if child in mapped_roots and child != root_pid:
                    continue  # child starts its own static clause
                if child not in owned:
                    owned.append(child)
                    nxt.append(child)
        frontier = nxt
    return tuple(owned)


def _aggregate(
    repo: str,
    clause: Mapping[str, Any],
    owned_pids: tuple[int, ...],
    owned_images: Sequence[ExecImageRecord],
    evidence: str,
    epoch_offset: float,
    *,
    protocol_timeout_terminated: bool = False,
) -> BridgedClause:
    t_exec = min(img.t_exec_ns for img in owned_images)
    t_end = max(img.t_end_ns for img in owned_images)
    # Quota must be present and consistent across owned images (a single run's
    # cgroup quota). Missing (<=0) or conflicting values -> CPU unavailable.
    quotas = {
        float(img.provenance["quota_cores"])
        for img in owned_images
        if isinstance(img.provenance.get("quota_cores"), (int, float))
        and math.isfinite(img.provenance["quota_cores"])
        and img.provenance["quota_cores"] > 0.0
    }
    quota = quotas.pop() if len(quotas) == 1 else None
    peak_cpu, cpu_reason = _merge_cpu(owned_images, t_exec, t_end, quota)
    peak_rss, rss_reason = _merge_rss(owned_images)
    disk_io, disk_io_reason = _merge_disk_io(owned_images)
    status = _clause_status(
        owned_pids,
        owned_images,
        protocol_timeout_terminated=protocol_timeout_terminated,
    )
    exit_signals = [i.exit_signal for i in owned_images if i.exit_signal]

    obs = ClauseObservation(
        repo=repo,
        bin=str(clause["bin"]),
        argv=tuple(clause["argv"]),
        ts_start=epoch_offset + t_exec / 1e9,
        ts_end=epoch_offset + t_end / 1e9,
        latency_ms=(None if protocol_timeout_terminated else (t_end - t_exec) / 1e6),
        peak_cpu_cores=peak_cpu,
        sampled_peak_rss_mb=peak_rss,
        cpu_ns_cumulative=sum(i.cpu_ns_cumulative for i in owned_images),
        in_loop=bool(clause.get("in_loop", False)),
        in_pipe=bool(clause.get("in_pipe", False)),
        in_subst=bool(clause.get("in_subst", False)),
        pipeline_position=int(clause.get("pipeline_position", -1)),
    )
    availability = (
        dict.fromkeys(
            ("latency", "cpu", "memory", "disk_io", "status"),
            "unknown:protocol_timeout",
        )
        if protocol_timeout_terminated
        else {
            "latency": "ok",
            "cpu": "ok" if peak_cpu is not None else f"unknown:{cpu_reason}",
            "memory": "ok" if peak_rss is not None else f"unknown:{rss_reason}",
            "disk_io": ("ok" if disk_io is not None else f"unknown:{disk_io_reason}"),
            "status": (
                "ok"
                if status["state"] in {"exited", "signaled"}
                else f"unknown:{status['reason'] or 'missing_terminal_status'}"
            ),
        }
    )
    provenance = {
        "mapping_evidence": evidence,
        "owned_exec_image_count": len(owned_images),
        "boundary_coverage": {
            "has_exec": True,
            "has_exit": any(i.terminal for i in owned_images),
        },
        "exit_signal": exit_signals[0] if exit_signals else None,
        "exit_signals": exit_signals,
        "merged_cpu_reason": cpu_reason,
        "merged_rss_reason": rss_reason,
        "merged_disk_io_reason": disk_io_reason,
        "disk_io_reduction": "sum_disjoint_owned_exec_image_totals",
        "per_image_diagnostics": [
            {
                "host_pid": i.host_pid,
                "exec_seq": i.exec_seq,
                "bin": i.bin,
                "requested_executable_path": i.requested_executable_path,
                "argv": list(i.argv),
                "argc": i.exact_argc,
                "argv_capped": i.argv_capped,
                "truncated_words": list(i.truncated_words),
                "bprm_filename": i.bprm_filename,
                "bprm_interp": i.bprm_interp,
                "normal_exit_status": i.normal_exit_status,
                "exit_signal": i.exit_signal,
                "scalar_peak_cpu_cores": i.peak_cpu_cores,
                "scalar_sampled_peak_rss_mb": i.sampled_peak_rss_mb,
                "has_cpu_profile": i.cpu_windows is not None,
                "has_rss_profile": i.rss_bins is not None,
                "rss_provenance": i.provenance.get("rss"),
                "disk_io_reason": i.disk_io_reason,
                "disk_read_bytes_total": i.disk_read_bytes_total,
                "disk_write_bytes_total": i.disk_write_bytes_total,
                "disk_cancelled_write_bytes_total": (
                    i.disk_cancelled_write_bytes_total
                ),
                "disk_io_provenance": i.provenance.get("disk_io"),
                "sample_attribution": i.provenance.get("sample_attribution"),
                "identity_only_sample_count": i.provenance.get(
                    "identity_only_sample_count", 0
                ),
                "identity_only_samples": i.provenance.get("identity_only_samples", []),
            }
            for i in owned_images
        ],
    }
    return BridgedClause(
        observation=obs,
        owned_pids=owned_pids,
        owned_exec_images=tuple((i.host_pid, i.exec_seq) for i in owned_images),
        mapping_evidence=evidence,
        disk_read_bytes_total=disk_io[0] if disk_io is not None else None,
        disk_write_bytes_total=disk_io[1] if disk_io is not None else None,
        disk_cancelled_write_bytes_total=(disk_io[2] if disk_io is not None else None),
        status=status,
        availability=availability,
        provenance=provenance,
    )


def _clause_status(
    owned_pids: Sequence[int],
    owned_images: Sequence[ExecImageRecord],
    *,
    protocol_timeout_terminated: bool = False,
) -> dict[str, Any]:
    """Return the shell-visible status of the mapped clause's root exec chain."""

    base = {
        "state": "unavailable",
        "exit_code": None,
        "signal": None,
        "succeeded": None,
        "reason": None,
        "source": "root_exec_chain_terminal",
    }
    if protocol_timeout_terminated:
        return {**base, "reason": "protocol_timeout"}
    if not owned_pids:
        return {**base, "reason": "missing_owned_root"}
    root_images = sorted(
        (image for image in owned_images if image.host_pid == owned_pids[0]),
        key=lambda image: (image.t_exec_ns, image.exec_seq),
    )
    if not root_images:
        return {**base, "reason": "missing_root_exec_chain"}
    terminal = root_images[-1]
    if not terminal.terminal or not terminal.has_causal_end:
        return {**base, "reason": "missing_causal_terminal"}
    if terminal.exit_signal is not None:
        return {
            **base,
            "state": "signaled",
            "signal": terminal.exit_signal,
            "succeeded": False,
        }
    if terminal.normal_exit_status is not None:
        return {
            **base,
            "state": "exited",
            "exit_code": terminal.normal_exit_status,
            "succeeded": terminal.normal_exit_status == 0,
        }
    return {**base, "reason": "missing_terminal_status"}


__all__ = [
    "BridgeResult",
    "BridgedClause",
    "ExecImageRecord",
    "FailedExecAttempt",
    "MappingGap",
    "NoRuntimeExec",
    "ShellCommandLookupFailure",
    "bridge_command",
    "parse_shell_lookup_diagnostic",
    "shell_lookup_exit_semantics",
]
