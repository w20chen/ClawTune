"""Runtime asymmetric two-layer tool resource knowledge bases.

``RuntimeToolResourceKB`` is retained for historical continuous-resource
diagnostics and snapshot compatibility. The current canonical API is the
latency-bucket path on ``ClauseResourceKB`` described below.

Public and repo layers intentionally use different key granularity because
they encode different environment assumptions:

- **Public layer** (frozen after fitting): heterogeneous repositories, so it
  holds only coarse cold-start knowledge — per-binary/head, per outer tool
  name, and global nodes. It never contains exact-command or command-prefix
  nodes; parameter semantics do not transfer across environments.
- **Repo layer** (accumulated causally online): same workspace, recurring
  command templates, so it may key by exact normalized command and ordered
  command prefixes before backing off to the local binary/head.

Backoff order for a query in repo R (hard repo-first, deepest non-empty node
wins — the evaluated baseline policy):

1. repo exact normalized command
2. repo ordered command prefix, deepest to shallowest (max depth 4, the
   frozen depth budget shared with the evaluated lattice)
3. repo binary/head
4. repo outer tool name for commandless, non-shell tools
5. public binary/head
6. public outer tool name (also the honest landing spot for compound
   commands with no single head, untokenizable commands, and non-shell
   tools)
7. public global

Binary/head identity is the generic basenamed command head from
``tool_time.command``. A compound shell call (``make && pytest``) has no
single honest head, so it feeds and matches no binary node — its full-call
label is never attributed to every contained binary. Shell builtins such as
``cd`` are represented by their parsed head; their label remains the
enclosing tool-call observation. No tool-specific option semantics are
implemented; argument order is preserved as-is.

Historical call-level target semantics:

- ``latency_ms``: observed call latency, skipped for censored calls;
- ``peak_cpu_cores``: eligible peak CPU cores;
- ``peak_memory_mb``: stored as an eligible residual relative to the
  deployment-legal ``ambient_before_mb`` anchor; a prediction adds the
  query's *current* ambient memory to the residual quantile. Queries
  without an ambient anchor get no memory prediction.

Causality: ``observe_completed_call`` buffers observations; an observation
enters repo state only when a later query's ``ts_start`` strictly exceeds
its ``ts_end`` (the evaluated prequential contract). Running, overlapping,
same-start, and future calls never leak into a prediction.

Known limitations (documented, unresolved by design in this phase): under
the hard repo-first baseline a repo node with a single sample overrides
public evidence; arbitration alternatives (shrinkage, calibration) remain
development candidates and are not implemented here.

--------------------------------------------------------------------------
Clause latency bucket predictor (``ClauseResourceKB``)
------------------------------------------------------

The current canonical stage predicts latency buckets with explicit boundaries.
The KB reuses mvdan clause identity, frozen public priors, and causal repo
refinement. Compound commands remain explicitly unavailable at the top level,
while each exec-producing clause exposes its own prediction or unavailable
reason. Partial evidence is distinguished from a fully evidenced but
uncomposed command, because sequential and pipeline timing require different
physical composition rules. Shell builtins that create no exec image remain in
the raw clause list but are excluded from the eBPF-evidence requirement.

``ClauseObservation`` is one *static mvdan clause* (identity = ``bin`` + ordered
``argv``), aggregated by ``tool_resource.clause_bridge`` from the Stage-2 eBPF
windowed sampler (`analysis/development/clause-telemetry-ebpf-stage2-*`). A
static clause may own a same-PID exec chain and descendants
(``env -> nice -> workload`` is ONE clause headed by ``env``); the bridge folds
all owned exec images into a single observation carrying latency plus canonical
Stage-2 CPU/RSS measurements for the later bucket stages. The current latency
API reads only ``latency_ms``. Backoff for clause identity is repo exact clause
-> repo argv prefixes -> repo bin -> public bin -> public global.
"""

from __future__ import annotations

import heapq
import math
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from tool_resource.features import (
    parse_command_clauses,
    shell_bin_requires_exec_evidence,
)
from tool_resource.metrics import ecdf_quantile
from tool_time.command import shell_command_heads, shell_command_prefix_tokens

TARGETS = ("latency_ms", "peak_cpu_cores", "peak_memory_mb")
_CONDITIONAL_P90_QUANTILE = 0.9
_MAX_PREFIX_DEPTH = 4  # frozen depth budget, same as the evaluated lattice
_SCHEMA = "runtime_tool_resource_kb_v1"

# (kind, key) — kind is what provenance exposes; key stays internal.
NodeKey = tuple[str, str]


@dataclass(frozen=True)
class CompletedCall:
    """Deployment-legal record of one finished tool call."""

    repo: str
    tool_name: str
    command: str | None
    ts_start: float
    ts_end: float
    censored: bool = False
    peak_cpu_cores: float | None = None
    peak_cpu_cores_eligible: bool = False
    peak_memory_mb: float | None = None
    peak_memory_mb_eligible: bool = False
    ambient_before_mb: float | None = None

    def __post_init__(self) -> None:
        if not (math.isfinite(self.ts_start) and math.isfinite(self.ts_end)):
            raise ValueError("ts_start and ts_end must be finite")
        if self.ts_end < self.ts_start:
            raise ValueError(f"ts_end {self.ts_end} precedes ts_start {self.ts_start}")

    @classmethod
    def from_resource_sample(cls, sample: Any, repo: str) -> CompletedCall:
        """Adapt a ``ResourceCallSample``-shaped row (duck-typed)."""

        tool_args = sample.tool_args or {}
        command = tool_args.get("command")
        return cls(
            repo=repo,
            tool_name=sample.tool_name,
            command=command if isinstance(command, str) else None,
            ts_start=sample.tool_ts_start,
            ts_end=sample.tool_ts_end,
            censored=sample.censored,
            peak_cpu_cores=sample.peak_cpu_cores,
            peak_cpu_cores_eligible=sample.peak_cpu_cores_eligible,
            peak_memory_mb=sample.peak_memory_mb,
            peak_memory_mb_eligible=sample.peak_memory_mb_eligible,
            ambient_before_mb=sample.ambient_before_mb,
        )


@dataclass(frozen=True)
class ToolCallQuery:
    """Pre-call query: repo identity, outer tool call, and current context."""

    repo: str
    tool_name: str
    command: str | None
    ts_start: float
    ambient_before_mb: float | None = None


@dataclass(frozen=True)
class TargetPrediction:
    """Secondary conditional-p90 estimate plus provenance for one target."""

    target: str
    conditional_p90: float | None
    scope: str | None
    key_kind: str | None
    evidence_count: int
    fallback_path: tuple[str, ...]
    note: str | None = None


def _target_values(call: CompletedCall) -> dict[str, float]:
    """Eligible per-target values; an ineligible target is skipped alone."""

    values: dict[str, float] = {}
    if not call.censored:
        values["latency_ms"] = (call.ts_end - call.ts_start) * 1000.0
    if call.peak_cpu_cores_eligible and call.peak_cpu_cores is not None:
        values["peak_cpu_cores"] = float(call.peak_cpu_cores)
    if (
        call.peak_memory_mb_eligible
        and call.peak_memory_mb is not None
        and call.ambient_before_mb is not None
    ):
        residual = float(call.peak_memory_mb) - float(call.ambient_before_mb)
        if not math.isfinite(residual):
            raise ValueError("memory residual must be finite")
        values["peak_memory_mb"] = residual
    return values


def _single_head(command: str | None) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    heads = shell_command_heads(command)
    return heads[0] if len(heads) == 1 else None


def _repo_keys(tool_name: str, command: str | None) -> list[NodeKey]:
    """Repo-layer keys for shell commands or commandless outer tools."""

    if command is None:
        return [("tool_name", tool_name)] if tool_name else []
    if not isinstance(command, str) or not command.strip():
        return []
    tokens = shell_command_prefix_tokens(command)
    if not tokens:
        return []
    keys: list[NodeKey] = [("exact_command", " ".join(tokens))]
    depth = min(len(tokens), _MAX_PREFIX_DEPTH)
    for length in range(depth, 0, -1):
        keys.append((f"command_prefix_depth_{length}", " ".join(tokens[:length])))
    head = _single_head(command)
    if head is not None:
        keys.append(("binary_head", head))
    return keys


def _public_keys(tool_name: str, command: str | None) -> list[NodeKey]:
    """Public-layer node keys: binary head (if honest), tool name, global."""

    keys: list[NodeKey] = []
    head = _single_head(command)
    if head is not None:
        keys.append(("binary_head", head))
    keys.append(("tool_name", tool_name))
    keys.append(("global", ""))
    return keys


class RuntimeToolResourceKB:
    """Frozen public layer plus causally accumulated per-repo nodes.

    Construct via :meth:`fit_public` or :meth:`from_json_obj`; the public
    layer is immutable afterwards and online observations touch only repo
    and pending state.
    """

    def __init__(self) -> None:
        self._public: dict[str, dict[NodeKey, tuple[float, ...]]] = {
            target: {} for target in TARGETS
        }
        self._repo: dict[str, dict[str, dict[NodeKey, list[float]]]] = {}
        self._pending: list[tuple[float, int, CompletedCall]] = []
        self._pending_seq = 0
        # Queries must be monotonic: absorbing pending calls is irreversible,
        # so a backdated query would see repo state from its future.
        self._last_query_ts: float | None = None

    @classmethod
    def fit_public(cls, calls: Iterable[CompletedCall]) -> RuntimeToolResourceKB:
        """Fit the frozen public layer from historical completed calls."""

        accumulator: dict[str, dict[NodeKey, list[float]]] = {
            target: {} for target in TARGETS
        }
        for call in calls:
            keys = _public_keys(call.tool_name, call.command)
            for target, value in _target_values(call).items():
                for key in keys:
                    accumulator[target].setdefault(key, []).append(value)
        missing = [
            target for target in TARGETS if not accumulator[target].get(("global", ""))
        ]
        if missing:
            raise ValueError(f"fit corpus has no eligible labels for {missing}")
        kb = cls()
        kb._public = {
            target: {key: tuple(values) for key, values in nodes.items()}
            for target, nodes in accumulator.items()
        }
        return kb

    def observe_completed_call(self, call: CompletedCall) -> None:
        """Buffer a finished call; it becomes visible only once causally prior."""

        heapq.heappush(self._pending, (call.ts_end, self._pending_seq, call))
        self._pending_seq += 1

    def query(self, query: ToolCallQuery) -> dict[str, TargetPrediction]:
        """Predict secondary conditional p90 before ``query.ts_start``."""

        if self._last_query_ts is not None and query.ts_start < self._last_query_ts:
            raise ValueError(
                f"backdated query at ts_start {query.ts_start} after a query at "
                f"{self._last_query_ts}: repo state already absorbed observations "
                "completed before the later time"
            )
        self._last_query_ts = query.ts_start
        self._absorb_completed(query.ts_start)
        return {target: self._predict_target(query, target) for target in TARGETS}

    def _absorb_completed(self, ts_start: float) -> None:
        # Strictly-completed contract: ts_end < ts_start. Same-start,
        # overlapping, running, and future observations stay pending.
        while self._pending and self._pending[0][0] < ts_start:
            _, _, call = heapq.heappop(self._pending)
            repo_targets = self._repo.setdefault(
                call.repo, {target: {} for target in TARGETS}
            )
            keys = _repo_keys(call.tool_name, call.command)
            for target, value in _target_values(call).items():
                for key in keys:
                    repo_targets[target].setdefault(key, []).append(value)

    def _levels(
        self, repo: str, target: str, tool_name: str, command: str | None
    ) -> Iterator[tuple[str, NodeKey, Sequence[float]]]:
        repo_nodes = self._repo.get(repo, {}).get(target, {})
        for key in _repo_keys(tool_name, command):
            yield "repo", key, repo_nodes.get(key, ())
        public_nodes = self._public[target]
        for key in _public_keys(tool_name, command):
            yield "public", key, public_nodes.get(key, ())

    def _select(
        self, repo: str, target: str, tool_name: str, command: str | None
    ) -> tuple[Sequence[float], str, str, tuple[str, ...]]:
        """Baseline arbitration: first (deepest) non-empty node wins outright.

        This is the single selection point; alternative arbitration policies
        (shrinkage, calibration) would replace this method, not the storage.
        """

        path: list[str] = []
        for scope, (kind, _), values in self._levels(repo, target, tool_name, command):
            path.append(f"{scope}:{kind}")
            if values:
                return values, scope, kind, tuple(path)
        raise ValueError(f"no public global node for target {target!r}")

    def _predict_target(self, query: ToolCallQuery, target: str) -> TargetPrediction:
        if target == "peak_memory_mb" and query.ambient_before_mb is None:
            return TargetPrediction(
                target=target,
                conditional_p90=None,
                scope=None,
                key_kind=None,
                evidence_count=0,
                fallback_path=(),
                note="memory prediction requires ambient_before_mb anchor",
            )
        values, scope, kind, path = self._select(
            query.repo, target, query.tool_name, query.command
        )
        conditional_p90 = ecdf_quantile(values, _CONDITIONAL_P90_QUANTILE)
        note = None
        if target == "peak_memory_mb":
            conditional_p90 += float(query.ambient_before_mb)
            note = "residual quantile plus query ambient_before_mb"
        return TargetPrediction(
            target=target,
            conditional_p90=conditional_p90,
            scope=scope,
            key_kind=kind,
            evidence_count=len(values),
            fallback_path=path,
            note=note,
        )

    def to_json_obj(self) -> dict[str, Any]:
        """JSON-serializable snapshot of public, repo, and pending state."""

        return {
            "schema": _SCHEMA,
            "quantile": _CONDITIONAL_P90_QUANTILE,
            "max_prefix_depth": _MAX_PREFIX_DEPTH,
            "public": {
                target: _nodes_to_json(nodes) for target, nodes in self._public.items()
            },
            "repo": {
                repo: {
                    target: _nodes_to_json(nodes) for target, nodes in targets.items()
                }
                for repo, targets in self._repo.items()
            },
            "pending": [asdict(call) for _, _, call in sorted(self._pending)],
            "last_query_ts": self._last_query_ts,
        }

    @classmethod
    def from_json_obj(cls, obj: Mapping[str, Any]) -> RuntimeToolResourceKB:
        """Restore a snapshot produced by :meth:`to_json_obj`."""

        if obj.get("schema") != _SCHEMA:
            raise ValueError(f"unsupported schema {obj.get('schema')!r}")
        if obj.get("quantile") != _CONDITIONAL_P90_QUANTILE:
            raise ValueError("snapshot quantile differs from module quantile")
        if obj.get("max_prefix_depth") != _MAX_PREFIX_DEPTH:
            raise ValueError("snapshot prefix depth differs from module depth")
        kb = cls()
        kb._public = {
            target: {
                key: tuple(values)
                for key, values in _nodes_from_json(obj["public"][target])
            }
            for target in TARGETS
        }
        kb._repo = {
            repo: {
                target: {
                    key: list(values)
                    for key, values in _nodes_from_json(targets.get(target, []))
                }
                for target in TARGETS
            }
            for repo, targets in obj.get("repo", {}).items()
        }
        for row in obj.get("pending", []):
            kb.observe_completed_call(CompletedCall(**row))
        last_query_ts = obj.get("last_query_ts")
        kb._last_query_ts = None if last_query_ts is None else float(last_query_ts)
        return kb


def _nodes_to_json(
    nodes: Mapping[NodeKey, Sequence[float]],
) -> list[list[Any]]:
    return [[kind, key, list(values)] for (kind, key), values in nodes.items()]


def _nodes_from_json(
    rows: Iterable[Sequence[Any]],
) -> Iterator[tuple[NodeKey, list[float]]]:
    for kind, key, values in rows:
        yield (str(kind), str(key)), [float(value) for value in values]


# ==========================================================================
# Clause latency bucket predictor
# ==========================================================================

_CLAUSE_SCHEMA = "runtime_clause_resource_kb_v4"
_CLAUSE_MAX_DEPTH = 4  # frozen ordered argv-prefix depth budget
_DELIM = "\x00"  # argv tokens may contain spaces; NUL cannot collide

# Aggregated Stage-2 clause-observation value sources. Each is a per-clause
# MEASURED metric (see ``tool_resource.clause_bridge``), not an eBPF exit field:
#   latency_ms          -- clause wall interval;
#   peak_cpu_cores       -- windowed peak CPU cores (never cpu_ns/wall_ns);
#   sampled_peak_rss_mb  -- max aligned distinct-mm RSS (never lifetime hiwater).
_LATENCY_MS = "latency_ms"
_PEAK_CPU_CORES = "peak_cpu_cores"
_SAMPLED_PEAK_RSS_MB = "sampled_peak_rss_mb"
_CLAUSE_SOURCES = (_LATENCY_MS, _PEAK_CPU_CORES, _SAMPLED_PEAK_RSS_MB)


@dataclass(frozen=True)
class ClauseObservation:
    """One completed *static mvdan clause*, aggregated from Stage-2 telemetry.

    Identity is the mvdan clause (``bin``, ordered ``argv``) — NOT a runtime
    exec-image occurrence. A single static clause may own an exec chain
    (``env -> nice -> workload``) and descendants; the bridge
    (``tool_resource.clause_bridge``) aggregates all owned exec images into one
    observation. The three fields are per-clause MEASURED metrics, each
    ``None`` when its target-specific coverage was insufficient:

    - ``latency_ms``      -- clause wall interval;
    - ``peak_cpu_cores``  -- windowed peak CPU cores over the owned lineage;
    - ``sampled_peak_rss_mb`` -- max aligned distinct-mm RSS over the lineage.

    ``cpu_ns_cumulative`` is preserved as a separate raw field.
    ``ts_start``/``ts_end`` are wall-clock seconds for the causal
    contract.
    """

    repo: str
    bin: str
    argv: tuple[str, ...]
    ts_start: float
    ts_end: float
    latency_ms: float | None = None
    peak_cpu_cores: float | None = None
    sampled_peak_rss_mb: float | None = None
    cpu_ns_cumulative: int | None = None  # raw, separate; never a flag source
    in_loop: bool = False
    in_pipe: bool = False
    in_subst: bool = False
    pipeline_position: int = -1

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("clause argv must be non-empty")
        if not (math.isfinite(self.ts_start) and math.isfinite(self.ts_end)):
            raise ValueError("ts_start and ts_end must be finite")
        if self.ts_end < self.ts_start:
            raise ValueError(f"ts_end {self.ts_end} precedes ts_start {self.ts_start}")


@dataclass(frozen=True)
class LatencyBuckets:
    """Explicit positive boundaries for right-open latency buckets."""

    edges_ms: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.edges_ms:
            raise ValueError("at least one latency bucket edge is required")
        previous = 0.0
        for edge in self.edges_ms:
            if not math.isfinite(edge) or edge <= previous:
                raise ValueError(
                    "latency bucket edges must be finite, positive, and "
                    "strictly increasing"
                )
            previous = edge

    @property
    def bucket_count(self) -> int:
        return len(self.edges_ms) + 1

    def bucket_id(self, latency_ms: float) -> int:
        """Return i for [b_i, b_{i+1}); the final bucket extends to +inf."""

        if not math.isfinite(latency_ms) or latency_ms < 0.0:
            raise ValueError("latency_ms must be finite and non-negative")
        return bisect_right(self.edges_ms, latency_ms)


@dataclass(frozen=True)
class ClauseLatencyBucketPrediction:
    """Empirical latency-bucket prediction for one clause."""

    bucket_id: int
    probability_by_bucket: tuple[float, ...]
    scope: str
    key_kind: str
    evidence_count: int
    fallback_path: tuple[str, ...]


@dataclass(frozen=True)
class ClauseLatencyBucketOutcome:
    """Prediction or explicit unavailability for one exec-producing clause."""

    clause_index: int
    bin: str
    argv: tuple[str, ...]
    prediction: ClauseLatencyBucketPrediction | None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.clause_index < 0:
            raise ValueError("clause_index must be non-negative")
        if not self.bin or not self.argv:
            raise ValueError("clause outcome requires non-empty bin and argv")
        if (self.prediction is None) == (self.unavailable_reason is None):
            raise ValueError(
                "clause outcome requires exactly one of prediction or "
                "unavailable_reason"
            )
        if self.unavailable_reason == "":
            raise ValueError("unavailable_reason must be non-empty")


@dataclass(frozen=True)
class CommandLatencyBucketPrediction:
    """Command result with honest per-clause compound evidence.

    ``composed`` is True when the command-level ``prediction`` is a derived
    total composed from per-clause median latencies (serial units sum,
    pipeline units take the max, trailing pipe viewers are dropped). The raw
    per-clause outcomes remain in ``clause_predictions`` so consumers can
    always see the underlying evidence instead of only the derived value.
    """

    repo: str
    command: str
    parse_failed: bool
    clause_bins: tuple[str, ...]
    prediction: ClauseLatencyBucketPrediction | None
    unavailable_reason: str | None = None
    clause_predictions: tuple[ClauseLatencyBucketOutcome, ...] = ()
    composed: bool = False
    composed_total_ms: float | None = None
    composition: tuple[dict[str, Any], ...] = ()


def _unavailable_clause_outcome(
    clause_index: int,
    clause: Mapping[str, Any],
    reason: str,
) -> ClauseLatencyBucketOutcome:
    return ClauseLatencyBucketOutcome(
        clause_index=clause_index,
        bin=str(clause["bin"]),
        argv=tuple(clause["argv"]),
        prediction=None,
        unavailable_reason=reason,
    )


def _clause_unavailable_reason(exc: Exception) -> str:
    if isinstance(exc, ValueError) and "no public global clause latency node" in str(
        exc
    ):
        return "no_clause_latency_evidence"
    return f"clause_prediction_error:{type(exc).__name__}"


def _clause_value(obs: ClauseObservation, source: str) -> float | None:
    if source == _LATENCY_MS:
        return obs.latency_ms
    if source == _PEAK_CPU_CORES:
        return obs.peak_cpu_cores
    if source == _SAMPLED_PEAK_RSS_MB:
        return obs.sampled_peak_rss_mb
    raise ValueError(f"unknown clause value source {source!r}")


def _clause_tokens(bin_: str, argv: Sequence[str]) -> tuple[str, ...]:
    # Identity token stream: bin head then the argv tail (argv[0] may be a full
    # path; bin is its basename, already normalized by mvdan).
    return (bin_, *argv[1:])


def _clause_repo_keys(bin_: str, argv: Sequence[str]) -> list[NodeKey]:
    """Repo backoff keys, most-specific first, for clause identity (bin, argv).

    Order: exact clause -> shorter bin-qualified argv prefixes -> bin. Every
    prefix key is nested under ``bin`` (its first token is ``bin``), so ``bin``
    is the LAST, most-general node queried — a more-specific prefix always wins
    before the bare bin node.
    """

    tokens = _clause_tokens(bin_, argv)
    keys: list[NodeKey] = [("exact_clause", _DELIM.join(tokens))]
    depth = min(len(tokens), _CLAUSE_MAX_DEPTH)
    # depth-1 prefix equals the bin node's content, so stop prefixes at 2.
    for length in range(depth, 1, -1):
        keys.append((f"argv_prefix_depth_{length}", _DELIM.join(tokens[:length])))
    keys.append(("bin", bin_))
    return keys


def _clause_public_keys(bin_: str) -> list[NodeKey]:
    """Public clause keys: coarse bin prior then global."""

    return [("bin", bin_), ("global", "")]


# Trailing pipeline stages that only consume/present the upstream result
# (tail, head, wc, cat, ...). In a pipeline every stage runs concurrently and
# the trailing consumer's wall clock tracks the producer — the producer
# usually closes the pipe or is SIGPIPE'd first — so a viewer's own prediction
# adds no independent time. Composition drops trailing viewers instead of
# double-counting them. Heavy transformers (grep/sed/awk/sort/...) are
# intentionally NOT listed: they can be the pipeline bottleneck, so dropping
# them would under-estimate the total.
_PIPE_VIEWER_BINS = frozenset(
    {
        "cat", "comm", "column", "cut", "fold", "head", "hexdump", "less",
        "more", "nl", "od", "paste", "rev", "tac", "tail", "tee", "tr",
        "ts", "uniq", "wc", "xxd",
    }
)


def _clause_in_pipe(clause: Mapping[str, Any]) -> bool:
    """Whether a parsed clause participates in a pipeline (``|``)."""
    return bool(clause.get("in_pipe", False))


def _clause_pipeline_position(clause: Mapping[str, Any]) -> int:
    """Zero-based position of the clause inside its pipeline (``-1`` when none)."""
    return int(clause.get("pipeline_position", -1))


def _compose_compound_latency_ms(
    clauses: Sequence[tuple[int, Mapping[str, Any]]],
    medians: Mapping[int, float],
) -> tuple[float, tuple[dict[str, Any], ...]] | None:
    """Compose a compound command's total latency from per-clause medians.

    ``clauses`` are ``(clause_index, clause)`` pairs for the exec-producing
    clauses in source order; ``medians`` maps each clause index to its median
    latency. Serial connections (``;``, ``&&``, ``||``, newline) run one after
    another and sum; a pipeline (``|``) runs its stages concurrently and
    contributes its slowest stage. Trailing pipeline stages whose bin is a
    result-viewing consumer (``_PIPE_VIEWER_BINS``) are dropped first: they
    run concurrently with the producer and their wall clock tracks it, so
    their own prediction adds no independent time.

    Returns ``(total_ms, units)`` where each unit is ``{"kind": "single" |
    "pipeline", "bins": [...], "time_ms": float,
    "dropped_viewer_bins": [...]}``. Returns ``None`` when no executable
    clause carries a median latency.
    """
    groups: list[list[int]] = []
    for position, (index, clause) in enumerate(clauses):
        if position > 0:
            _, previous_clause = clauses[position - 1]
            connected = (
                groups
                and _clause_in_pipe(clause)
                and _clause_in_pipe(previous_clause)
                and _clause_pipeline_position(clause)
                == _clause_pipeline_position(previous_clause) + 1
            )
        else:
            connected = False
        if connected:
            groups[-1].append(index)
        else:
            groups.append([index])

    clause_by_index = {index: clause for index, clause in clauses}
    units: list[dict[str, Any]] = []
    total_ms = 0.0
    for group in groups:
        if len(group) == 1:
            index = group[0]
            if index not in medians:
                return None
            units.append(
                {
                    "kind": "single",
                    "bins": [str(clause_by_index[index]["bin"])],
                    "time_ms": medians[index],
                    "dropped_viewer_bins": [],
                }
            )
            total_ms += medians[index]
            continue
        pipeline = list(group)
        dropped_viewer_bins: list[str] = []
        while (
            len(pipeline) > 1
            and str(clause_by_index[pipeline[-1]]["bin"]) in _PIPE_VIEWER_BINS
        ):
            dropped_viewer_bins.append(str(clause_by_index[pipeline[-1]]["bin"]))
            pipeline.pop()
        stage_times = [medians[index] for index in pipeline if index in medians]
        if not stage_times:
            return None
        unit_ms = max(stage_times)
        units.append(
            {
                "kind": "pipeline",
                "bins": [str(clause_by_index[index]["bin"]) for index in pipeline],
                "time_ms": unit_ms,
                "dropped_viewer_bins": dropped_viewer_bins,
            }
        )
        total_ms += unit_ms
    if not units:
        return None
    return total_ms, tuple(units)


class ClauseResourceKB:
    """Causal clause history with a current-stage latency-bucket API.

    Public bin priors are frozen after construction; repo clause/prefix nodes
    accumulate causally (strict ``ts_end < query ts_start``) under the same
    monotonic-query guard as :class:`RuntimeToolResourceKB`.
    """

    def __init__(self) -> None:
        self._public: dict[str, dict[NodeKey, tuple[float, ...]]] = {
            source: {} for source in _CLAUSE_SOURCES
        }
        self._repo: dict[str, dict[str, dict[NodeKey, list[float]]]] = {}
        self._pending: list[tuple[float, int, ClauseObservation]] = []
        self._pending_seq = 0
        self._last_query_ts: float | None = None

    @classmethod
    def fit_public(
        cls,
        observations: Iterable[ClauseObservation],
    ) -> ClauseResourceKB:
        """Fit frozen public bin/global priors from historical clauses."""

        acc: dict[str, dict[NodeKey, list[float]]] = {
            source: {} for source in _CLAUSE_SOURCES
        }
        for obs in observations:
            keys = _clause_public_keys(obs.bin)
            for source in _CLAUSE_SOURCES:
                value = _clause_value(obs, source)
                if value is None:
                    continue
                for key in keys:
                    acc[source].setdefault(key, []).append(value)
        if not acc[_LATENCY_MS].get(("global", "")):
            raise ValueError("fit corpus has no clause latency (wall_ns) evidence")
        kb = cls()
        kb._public = {
            source: {key: tuple(values) for key, values in nodes.items()}
            for source, nodes in acc.items()
        }
        return kb

    def observe_completed_clause(self, obs: ClauseObservation) -> None:
        """Buffer a completed clause; visible only once strictly causally prior."""

        heapq.heappush(self._pending, (obs.ts_end, self._pending_seq, obs))
        self._pending_seq += 1

    def _absorb_completed(self, ts_start: float) -> None:
        while self._pending and self._pending[0][0] < ts_start:
            _, _, obs = heapq.heappop(self._pending)
            repo_sources = self._repo.setdefault(
                obs.repo, {source: {} for source in _CLAUSE_SOURCES}
            )
            keys = _clause_repo_keys(obs.bin, obs.argv)
            for source in _CLAUSE_SOURCES:
                value = _clause_value(obs, source)
                if value is None:
                    continue
                for key in keys:
                    repo_sources[source].setdefault(key, []).append(value)

    def _select(
        self, repo: str, source: str, bin_: str, argv: Sequence[str]
    ) -> tuple[Sequence[float], str, str, tuple[str, ...]] | None:
        repo_nodes = self._repo.get(repo, {}).get(source, {})
        public_nodes = self._public[source]
        path: list[str] = []
        for key in _clause_repo_keys(bin_, argv):
            path.append(f"repo:{key[0]}")
            values = repo_nodes.get(key)
            if values:
                return values, "repo", key[0], tuple(path)
        for key in _clause_public_keys(bin_):
            path.append(f"public:{key[0]}")
            values = public_nodes.get(key)
            if values:
                return values, "public", key[0], tuple(path)
        return None

    def _clause_median_latency_ms(
        self, repo: str, bin_: str, argv: Sequence[str]
    ) -> float | None:
        """Median clause wall latency from the same evidence as the bucket."""
        selected = self._select(repo, _LATENCY_MS, bin_, argv)
        if selected is None:
            return None
        values, _, _, _ = selected
        ordered = sorted(values)
        if not ordered:
            return None
        midpoint = len(ordered) // 2
        if len(ordered) % 2 == 1:
            return ordered[midpoint]
        return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0

    def _compose_command_prediction(
        self,
        repo: str,
        executable: Sequence[tuple[int, Mapping[str, Any]]],
        outcomes: Sequence[ClauseLatencyBucketOutcome],
        buckets: LatencyBuckets,
    ) -> tuple[ClauseLatencyBucketPrediction, float, tuple[dict[str, Any], ...]] | None:
        """Compose a command-level latency bucket from per-clause medians.

        Every exec-producing clause must already have a bucket prediction; the
        composed total is a derived estimate (serial units sum, pipeline units
        take the max, trailing pipe viewers are dropped) rather than a direct
        observation. Returns ``(prediction, composed_total_ms, composition)``
        or ``None`` when any clause lacks median latency evidence.
        """
        medians: dict[int, float] = {}
        for item in outcomes:
            if item.prediction is None:
                return None
            median = self._clause_median_latency_ms(repo, item.bin, item.argv)
            if median is None:
                return None
            medians[item.clause_index] = median
        composed = _compose_compound_latency_ms(executable, medians)
        if composed is None:
            return None
        total_ms, units = composed
        bucket_id = buckets.bucket_id(total_ms)
        probabilities = [0.0] * buckets.bucket_count
        probabilities[bucket_id] = 1.0
        prediction = ClauseLatencyBucketPrediction(
            bucket_id=bucket_id,
            probability_by_bucket=tuple(probabilities),
            scope="composed",
            key_kind="compound_composed",
            evidence_count=len(medians),
            fallback_path=("composed",) + tuple(unit["kind"] for unit in units),
        )
        return prediction, total_ms, units

    def predict_clause_latency_bucket(
        self,
        repo: str,
        bin_: str,
        argv: Sequence[str],
        buckets: LatencyBuckets,
    ) -> ClauseLatencyBucketPrediction:
        """Predict the modal empirical latency bucket for one clause."""

        selected = self._select(repo, _LATENCY_MS, bin_, argv)
        if selected is None:
            raise ValueError("no public global clause latency node")
        values, scope, kind, path = selected
        counts = [0] * buckets.bucket_count
        for value in values:
            counts[buckets.bucket_id(value)] += 1
        predicted = max(range(buckets.bucket_count), key=lambda i: (counts[i], -i))
        return ClauseLatencyBucketPrediction(
            bucket_id=predicted,
            probability_by_bucket=tuple(count / len(values) for count in counts),
            scope=scope,
            key_kind=kind,
            evidence_count=len(values),
            fallback_path=path,
        )

    def predict_command_latency_bucket_from_clauses(
        self,
        repo: str,
        clauses: Sequence[Mapping[str, Any]],
        ts_start: float,
        buckets: LatencyBuckets,
        *,
        command: str = "",
        parse_failed: bool = False,
        shell_command: bool = True,
    ) -> CommandLatencyBucketPrediction:
        """Predict exec-producing clauses without composing compound latency.

        ``clause_bins`` preserves every parsed clause, including shell builtins
        such as ``cd`` which run inside the shell and create no eBPF exec image.
        ``clause_predictions`` contains only clauses that should create an exec
        image, retaining their raw clause indexes for transparent correlation.
        """

        self._advance(ts_start)
        effective = list(clauses)
        clause_bins = tuple(str(c["bin"]) for c in effective)
        executable = [
            (index, clause)
            for index, clause in enumerate(effective)
            if not shell_command
            or shell_bin_requires_exec_evidence(
                str(clause["bin"]),
                str(clause["argv"][0]) if clause.get("argv") else None,
            )
        ]
        clause_predictions: tuple[ClauseLatencyBucketOutcome, ...] = ()
        reason: str | None = None
        prediction: ClauseLatencyBucketPrediction | None = None
        composed = False
        composed_total_ms: float | None = None
        composition: tuple[dict[str, Any], ...] = ()
        if parse_failed:
            reason = "parse_failed"
            clause_predictions = tuple(
                _unavailable_clause_outcome(index, clause, reason)
                for index, clause in executable
            )
        elif len(effective) == 0:
            reason = "empty_command"
        elif len(effective) == 1 and not executable:
            reason = "no_executable_clauses"
        else:
            clause_predictions = tuple(
                self._predict_clause_outcome(repo, index, clause, buckets)
                for index, clause in executable
            )
            if len(effective) == 1:
                prediction = clause_predictions[0].prediction
                reason = clause_predictions[0].unavailable_reason
            elif not clause_predictions:
                # Multi-clause command with no measurable exec-producing
                # clause (e.g. ``cd /workspace && export MODE=test``).
                reason = "no_executable_clauses"
            elif any(item.prediction is None for item in clause_predictions):
                reason = "compound_clause_evidence_incomplete"
            else:
                composed_result = self._compose_command_prediction(
                    repo,
                    executable,
                    clause_predictions,
                    buckets,
                )
                if composed_result is None:
                    reason = "compound_command_uncomposed"
                else:
                    prediction, composed_total_ms, composition = composed_result
                    composed = True
                    reason = None
        return CommandLatencyBucketPrediction(
            repo=repo,
            command=command,
            parse_failed=parse_failed,
            clause_bins=clause_bins,
            prediction=prediction,
            unavailable_reason=reason,
            clause_predictions=clause_predictions,
            composed=composed,
            composed_total_ms=composed_total_ms,
            composition=composition,
        )

    def _predict_clause_outcome(
        self,
        repo: str,
        clause_index: int,
        clause: Mapping[str, Any],
        buckets: LatencyBuckets,
    ) -> ClauseLatencyBucketOutcome:
        bin_ = str(clause["bin"])
        argv = tuple(clause["argv"])
        try:
            prediction = self.predict_clause_latency_bucket(
                repo,
                bin_,
                argv,
                buckets,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ClauseLatencyBucketOutcome(
                clause_index=clause_index,
                bin=bin_,
                argv=argv,
                prediction=None,
                unavailable_reason=_clause_unavailable_reason(exc),
            )
        return ClauseLatencyBucketOutcome(
            clause_index=clause_index,
            bin=bin_,
            argv=argv,
            prediction=prediction,
        )

    def predict_command_latency_bucket(
        self,
        repo: str,
        command: str,
        ts_start: float,
        buckets: LatencyBuckets,
    ) -> CommandLatencyBucketPrediction:
        """Parse a command and predict its bucket when composition is unnecessary.

        Enforces the monotonic-query guard and releases causally-prior repo
        clauses before predicting.
        """

        parsed = parse_command_clauses(command)
        return self.predict_command_latency_bucket_from_clauses(
            repo,
            parsed["clauses"],
            ts_start,
            buckets,
            command=command,
            parse_failed=bool(parsed["parse_failed"]),
        )

    def _advance(self, ts_start: float) -> None:
        if not math.isfinite(ts_start):
            raise ValueError("query ts_start must be finite")
        if self._last_query_ts is not None and ts_start < self._last_query_ts:
            raise ValueError(
                f"backdated query at ts_start {ts_start} after a query at "
                f"{self._last_query_ts}: repo clause state already absorbed "
                "observations completed before the later time"
            )
        self._last_query_ts = ts_start
        self._absorb_completed(ts_start)

    def to_json_obj(self) -> dict[str, Any]:
        """JSON-serializable snapshot of public, repo, and pending state."""

        return {
            "schema": _CLAUSE_SCHEMA,
            "max_prefix_depth": _CLAUSE_MAX_DEPTH,
            "public": {
                source: _nodes_to_json(nodes) for source, nodes in self._public.items()
            },
            "repo": {
                repo: {
                    source: _nodes_to_json(nodes) for source, nodes in sources.items()
                }
                for repo, sources in self._repo.items()
            },
            "pending": [asdict(obs) for _, _, obs in sorted(self._pending)],
            "last_query_ts": self._last_query_ts,
        }

    @classmethod
    def from_json_obj(cls, obj: Mapping[str, Any]) -> ClauseResourceKB:
        """Restore a snapshot produced by :meth:`to_json_obj`."""

        if obj.get("schema") != _CLAUSE_SCHEMA:
            raise ValueError(f"unsupported clause schema {obj.get('schema')!r}")
        if obj.get("max_prefix_depth") != _CLAUSE_MAX_DEPTH:
            raise ValueError("snapshot prefix depth differs from module depth")
        kb = cls()
        kb._public = {
            source: {
                key: tuple(values)
                for key, values in _nodes_from_json(obj["public"].get(source, []))
            }
            for source in _CLAUSE_SOURCES
        }
        kb._repo = {
            repo: {
                source: {
                    key: list(values)
                    for key, values in _nodes_from_json(sources.get(source, []))
                }
                for source in _CLAUSE_SOURCES
            }
            for repo, sources in obj.get("repo", {}).items()
        }
        for row in obj.get("pending", []):
            kb.observe_completed_clause(
                ClauseObservation(**{**row, "argv": tuple(row["argv"])})
            )
        last_query_ts = obj.get("last_query_ts")
        kb._last_query_ts = None if last_query_ts is None else float(last_query_ts)
        return kb


__all__ = [
    "TARGETS",
    "ClauseLatencyBucketOutcome",
    "ClauseLatencyBucketPrediction",
    "ClauseObservation",
    "ClauseResourceKB",
    "CommandLatencyBucketPrediction",
    "CompletedCall",
    "LatencyBuckets",
    "RuntimeToolResourceKB",
    "TargetPrediction",
    "ToolCallQuery",
]
