"""Causal tabular features for per-call latency, CPU, and memory prediction."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
import heapq
import math
import random
import re
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from tool_resource.labels import ResourceCallSample
from tool_resource.metrics import ecdf_quantile
from tool_resource.mvdan_client import (
    PARSER_NAME,
    PARSER_VERSION,
    MvdanClientError,
    get_client as get_mvdan_client,
)
from tool_time.command import make_row_command_prefix_keys, shell_command_segments
from tool_time.prior import LatencyPrior, latency_prior_hierarchy


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REPO_SUFFIX_RE = re.compile(r"-\d+$")
_WRAPPER_BINS = frozenset(
    {"bash", "env", "nice", "nohup", "sh", "sudo", "timeout", "xargs"}
)
# Static shell clauses headed by these builtins execute inside the already
# running shell. They cannot, and therefore must not, be made contingent on a
# new eBPF exec-image observation. Keep the raw clause in parser output so
# callers can preserve command structure and clause indexes.
NOEXEC_SHELL_BUILTINS = frozenset(
    {
        "cd",
        "export",
        "unset",
        "set",
        "true",
        "false",
        ":",
        "alias",
        "umask",
        "shift",
        "local",
        "read",
        "echo",
        "printf",
        "test",
        "[",
        "wait",
        "eval",
        "source",
        ".",
        "pwd",
        "exit",
        "return",
        "break",
        "continue",
        "trap",
    }
)
_GIT_READ = frozenset(
    {"blame", "branch", "describe", "diff", "grep", "log", "show", "status"}
)
_GIT_WRITE = frozenset(
    {
        "add",
        "am",
        "apply",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "rebase",
        "reset",
        "restore",
        "revert",
        "stash",
        "switch",
        "tag",
    }
)
_GIT_NETWORK = frozenset({"clone", "fetch", "pull", "push", "remote", "submodule"})
_PREFIX_KEYS = make_row_command_prefix_keys("command", max_depth=4)
_EWMA_ALPHA = 0.3
_N_FOLDS = 5
_FOLD_SEED = 0
_PARSER_PROVENANCE = {"name": PARSER_NAME, "version": PARSER_VERSION}

_GROUP_A_FEATURES = (
    "clause_count",
    "pipeline_depth",
    "has_loop",
    "has_pipeline",
    "has_wrapper",
    "has_substitution",
    "parse_failed",
    "heavy_bin_count",
    "light_bin_count",
    "heavy_bin_mean_cpu_s_sum",
    "heavy_bin_mean_cpu_s_max",
    "heavy_bin_mean_mem_kb_sum",
    "heavy_bin_mean_mem_kb_max",
    "pytest_target_count",
    "pytest_has_x",
    "pytest_has_k",
    "pip_install",
    "git_class_read",
    "git_class_write",
    "git_class_network",
    "git_class_other",
    "apt_install",
)
_GROUP_B_FEATURES = (
    "exact_command_recurrence_count",
    "same_command_last_latency_ms",
    "same_command_last_peak_memory_mb",
    "same_command_last_peak_cpu_cores",
    "same_prefix_last_latency_ms",
    "same_prefix_last_peak_memory_mb",
    "same_prefix_last_peak_cpu_cores",
)
_GROUP_C_FEATURES = (
    "call_index",
    "elapsed_task_s",
    "prior_duration_ewma_ms",
    "gap_since_previous_call_gt_5s",
    "previous_call_duration_ms",
)
_GROUP_D_FEATURES = ("prior_latency_p50_ms", "prior_latency_p90_ms")
TARGET_NAMES = ("latency_ms", "peak_cpu_cores", "peak_memory_mb")


@dataclass(frozen=True)
class TabularDataset:
    """Aligned numeric features, targets, masks, identities, and repo folds."""

    metadata: dict[str, Any]
    features: dict[str, np.ndarray]
    targets: dict[str, np.ndarray]
    eligibility_masks: dict[str, np.ndarray]
    sample_ids: np.ndarray
    task_ids: np.ndarray
    fold_assignment: np.ndarray
    repo_folds: dict[str, int]
    feature_names: tuple[str, ...]


@dataclass
class _History:
    latency_ms: float | None = None
    peak_memory_mb: float | None = None
    peak_cpu_cores: float | None = None

    def update(self, sample: ResourceCallSample, latency_ms: float) -> None:
        self.latency_ms = latency_ms
        if sample.peak_memory_mb_eligible and sample.peak_memory_mb is not None:
            self.peak_memory_mb = sample.peak_memory_mb
        if sample.peak_cpu_cores_eligible and sample.peak_cpu_cores is not None:
            self.peak_cpu_cores = sample.peak_cpu_cores


def repo_of(task_id: str) -> str:
    """Strip the trailing instance number from a task ID."""

    return _REPO_SUFFIX_RE.sub("", task_id)


def shell_bin_requires_exec_evidence(bin_: str) -> bool:
    """Whether a static shell clause head should create a new exec image."""

    return bin_ not in NOEXEC_SHELL_BUILTINS


def assign_repo_folds(
    repos: Iterable[str],
    n_folds: int,
    seed: int,
) -> dict[str, int]:
    """Assign each unique repository to one deterministic fold."""

    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    unique = sorted(set(repos))
    random.Random(seed).shuffle(unique)
    return {repo: index % n_folds for index, repo in enumerate(unique)}


def parse_command_clauses(command: str) -> dict[str, Any]:
    """Return Bash clauses and causal context, falling back on malformed input.

    Mvdan byte offsets are converted to Python code-point indices. Leading
    assignments are excluded from argv, while executable wrappers remain the
    head. Statements without an executable head emit no clause.
    """

    response = get_mvdan_client().parse(command)
    if not response.get("ok"):
        return {
            "clauses": _fallback_clauses(command),
            "control_edges": [],
            "parse_failed": True,
        }
    raw_clauses = response.get("clauses")
    if not isinstance(raw_clauses, list):
        raise MvdanClientError("mvdan adapter response has no clause list")
    byte_to_character = _byte_to_character_offsets(command)
    clauses = [
        _clause_from_adapter(command, raw_clause, byte_to_character)
        for raw_clause in raw_clauses
    ]
    raw_control_edges = response.get("control_edges")
    if not isinstance(raw_control_edges, list):
        raise MvdanClientError("mvdan adapter response has no control-edge list")
    control_edges = [
        _control_edge_from_adapter(
            raw_edge,
            edge_id,
            len(clauses),
            byte_to_character,
        )
        for edge_id, raw_edge in enumerate(raw_control_edges)
    ]
    return {
        "clauses": clauses,
        "control_edges": control_edges,
        "parse_failed": False,
    }


def build_tabular_dataset(
    samples_by_task: Mapping[str, Sequence[ResourceCallSample]],
    prior: LatencyPrior,
    cost_table: Mapping[str, Mapping[str, float]] | None,
    taus: Sequence[float],
) -> TabularDataset:
    """Build start-time features and aligned targets in deterministic task order."""

    if not samples_by_task:
        raise ValueError("samples_by_task must be non-empty")
    normalized_taus = _normalized_taus(taus)
    all_samples = [sample for samples in samples_by_task.values() for sample in samples]
    include_error_history = bool(all_samples) and all(
        hasattr(sample, "tool_result") for sample in all_samples
    )

    rows: list[dict[str, float]] = []
    bin_counts_by_row: list[Counter[str]] = []
    targets = {name: [] for name in TARGET_NAMES}
    masks = {name: [] for name in TARGET_NAMES}
    sample_ids: list[str] = []
    task_ids: list[str] = []

    for task_id in sorted(samples_by_task):
        calls = sorted(
            samples_by_task[task_id],
            key=lambda sample: (sample.tool_ts_start, sample.sample_id),
        )
        task_rows = _features_for_task(
            calls,
            prior,
            cost_table,
            normalized_taus,
            include_error_history=include_error_history,
        )
        for sample, row, bin_counts in task_rows:
            rows.append(row)
            bin_counts_by_row.append(bin_counts)
            values, eligibility = _targets(sample)
            for name in TARGET_NAMES:
                targets[name].append(values[name])
                masks[name].append(eligibility[name])
            sample_ids.append(sample.sample_id)
            task_ids.append(task_id)

    bin_names = sorted(cost_table or {})
    bin_features = tuple(f"bin_count:{name}" for name in bin_names)
    error_features = ("prior_error_timeout_count",) if include_error_history else ()
    tau_features = tuple(_tau_feature_name(tau) for tau in normalized_taus)
    feature_names = (
        _GROUP_A_FEATURES
        + bin_features
        + _GROUP_B_FEATURES
        + _GROUP_C_FEATURES
        + error_features
        + _GROUP_D_FEATURES
        + tau_features
    )
    features = {
        name: np.asarray(
            [
                (
                    float(bin_counts.get(name.removeprefix("bin_count:"), 0))
                    if name.startswith("bin_count:")
                    else row[name]
                )
                for row, bin_counts in zip(rows, bin_counts_by_row, strict=True)
            ],
            dtype=float,
        )
        for name in feature_names
    }
    repo_folds = assign_repo_folds(
        (repo_of(task_id) for task_id in samples_by_task),
        n_folds=_N_FOLDS,
        seed=_FOLD_SEED,
    )
    return TabularDataset(
        metadata={"command_clause_parser": dict(_PARSER_PROVENANCE)},
        features=features,
        targets={
            name: np.asarray(values, dtype=float) for name, values in targets.items()
        },
        eligibility_masks={
            name: np.asarray(values, dtype=bool) for name, values in masks.items()
        },
        sample_ids=np.asarray(sample_ids, dtype=object),
        task_ids=np.asarray(task_ids, dtype=object),
        fold_assignment=np.asarray(
            [repo_folds[repo_of(task_id)] for task_id in task_ids], dtype=int
        ),
        repo_folds=repo_folds,
        feature_names=feature_names,
    )


def _features_for_task(
    calls: Sequence[ResourceCallSample],
    prior: LatencyPrior,
    cost_table: Mapping[str, Mapping[str, float]] | None,
    taus: Sequence[float],
    *,
    include_error_history: bool,
) -> list[tuple[ResourceCallSample, dict[str, float], Counter[str]]]:
    exact_history: dict[str, _History] = {}
    prefix_history: dict[str, _History] = {}
    exact_recurrences: Counter[str] = Counter()
    pending: list[tuple[float, int, ResourceCallSample, str, str, float]] = []
    rows: list[tuple[ResourceCallSample, dict[str, float], Counter[str]]] = []
    t0 = calls[0].tool_ts_start if calls else 0.0
    previous_end: float | None = None
    previous_duration_ms = 0.0
    duration_ewma_ms: float | None = None
    prior_error_count = 0

    for index, sample in enumerate(calls):
        while pending and pending[0][0] <= sample.tool_ts_start:
            (
                previous_end,
                _,
                completed,
                completed_command,
                completed_prefix,
                previous_duration_ms,
            ) = heapq.heappop(pending)
            if completed_command:
                exact_history.setdefault(completed_command, _History()).update(
                    completed, previous_duration_ms
                )
            if completed_prefix:
                prefix_history.setdefault(completed_prefix, _History()).update(
                    completed, previous_duration_ms
                )
            duration_ewma_ms = (
                previous_duration_ms
                if duration_ewma_ms is None
                else _EWMA_ALPHA * previous_duration_ms
                + (1.0 - _EWMA_ALPHA) * duration_ewma_ms
            )
            if include_error_history and _has_error_or_timeout(completed):
                prior_error_count += 1

        command = _command(sample)
        prefix_key = _prefix_key(sample)
        exact = exact_history.get(command) if command else None
        prefix = prefix_history.get(prefix_key) if prefix_key else None
        row, bin_counts = _static_features(command, cost_table)
        row.update(
            {
                "exact_command_recurrence_count": float(exact_recurrences[command]),
                "same_command_last_latency_ms": _history_value(exact, "latency_ms"),
                "same_command_last_peak_memory_mb": _history_value(
                    exact, "peak_memory_mb"
                ),
                "same_command_last_peak_cpu_cores": _history_value(
                    exact, "peak_cpu_cores"
                ),
                "same_prefix_last_latency_ms": _history_value(prefix, "latency_ms"),
                "same_prefix_last_peak_memory_mb": _history_value(
                    prefix, "peak_memory_mb"
                ),
                "same_prefix_last_peak_cpu_cores": _history_value(
                    prefix, "peak_cpu_cores"
                ),
                "call_index": float(index),
                "elapsed_task_s": sample.tool_ts_start - t0,
                "prior_duration_ewma_ms": duration_ewma_ms or 0.0,
                "gap_since_previous_call_gt_5s": float(
                    previous_end is not None
                    and sample.tool_ts_start - previous_end > 5.0
                ),
                "previous_call_duration_ms": previous_duration_ms,
            }
        )
        if include_error_history:
            row["prior_error_timeout_count"] = float(prior_error_count)
        row.update(_prior_features(sample, prior, taus))
        rows.append((sample, row, bin_counts))

        latency_ms = _latency_ms(sample)
        if command:
            exact_recurrences[command] += 1
        heapq.heappush(
            pending,
            (
                sample.tool_ts_end,
                index,
                sample,
                command,
                prefix_key,
                latency_ms,
            ),
        )
    return rows


def _static_features(
    command: str,
    cost_table: Mapping[str, Mapping[str, float]] | None,
) -> tuple[dict[str, float], Counter[str]]:
    parsed = (
        parse_command_clauses(command)
        if command
        else {
            "clauses": [],
            "parse_failed": False,
        }
    )
    clauses = parsed["clauses"]
    bins = Counter(str(clause["bin"]) for clause in clauses)
    cpu_values: list[float] = []
    memory_values: list[float] = []
    heavy_count = 0
    light_count = 0
    for clause in clauses:
        entry = cost_table.get(clause["bin"]) if cost_table is not None else None
        if entry is None:
            light_count += cost_table is not None
            continue
        heavy_count += 1
        cpu_values.append(_cost_value(entry, "mean_cpu_s"))
        memory_values.append(_cost_value(entry, "mean_mem_kb"))

    pipeline_depth = max(
        (
            int(clause["pipeline_position"]) + 1
            for clause in clauses
            if clause["in_pipe"]
        ),
        default=1 if clauses else 0,
    )
    features = {
        "clause_count": float(len(clauses)),
        "pipeline_depth": float(pipeline_depth),
        "has_loop": float(any(clause["in_loop"] for clause in clauses)),
        "has_pipeline": float(any(clause["in_pipe"] for clause in clauses)),
        "has_wrapper": float(any(clause["bin"] in _WRAPPER_BINS for clause in clauses)),
        "has_substitution": float(any(clause["in_subst"] for clause in clauses)),
        "parse_failed": float(parsed["parse_failed"]),
        "heavy_bin_count": float(heavy_count),
        "light_bin_count": float(light_count),
        "heavy_bin_mean_cpu_s_sum": math.fsum(cpu_values),
        "heavy_bin_mean_cpu_s_max": max(cpu_values, default=0.0),
        "heavy_bin_mean_mem_kb_sum": math.fsum(memory_values),
        "heavy_bin_mean_mem_kb_max": max(memory_values, default=0.0),
    }
    features.update(_argument_features(clauses))
    return features, bins


def _argument_features(clauses: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    result = {
        "pytest_target_count": 0.0,
        "pytest_has_x": 0.0,
        "pytest_has_k": 0.0,
        "pip_install": 0.0,
        "git_class_read": 0.0,
        "git_class_write": 0.0,
        "git_class_network": 0.0,
        "git_class_other": 0.0,
        "apt_install": 0.0,
    }
    for clause in clauses:
        binary = str(clause["bin"])
        argv = [str(value) for value in clause["argv"]]
        args = argv[1:]
        if binary in {"pytest", "py.test"}:
            result["pytest_has_x"] = float(
                any(
                    arg == "-x"
                    or (
                        arg.startswith("-")
                        and not arg.startswith("--")
                        and "x" in arg[1:]
                    )
                    for arg in args
                )
            )
            result["pytest_has_k"] = float(
                any(arg == "-k" or arg.startswith("-k") for arg in args)
            )
            skip = {index + 1 for index, arg in enumerate(args) if arg == "-k"}
            result["pytest_target_count"] += sum(
                not arg.startswith("-") and index not in skip
                for index, arg in enumerate(args)
            )
        if (
            binary.startswith("pip")
            and args[:1] == ["install"]
            or binary.startswith("python")
            and len(args) >= 3
            and args[:2] == ["-m", "pip"]
            and args[2] == "install"
        ):
            result["pip_install"] = 1.0
        if binary == "git":
            subcommand = _git_subcommand(args)
            if subcommand in _GIT_READ:
                result["git_class_read"] = 1.0
            elif subcommand in _GIT_WRITE:
                result["git_class_write"] = 1.0
            elif subcommand in _GIT_NETWORK:
                result["git_class_network"] = 1.0
            elif subcommand:
                result["git_class_other"] = 1.0
        if binary in {"apt", "apt-get"} and "install" in args:
            result["apt_install"] = 1.0
    return result


def _prior_features(
    sample: ResourceCallSample, prior: LatencyPrior, taus: Sequence[float]
) -> dict[str, float]:
    row = {"tool_name": sample.tool_name, "tool_args": sample.tool_args}
    node = latency_prior_hierarchy(
        prior,
        sample.tool_name,
        _PREFIX_KEYS(row),
        min_tool_history=1,
        min_profile_tasks=1,
    )[-1]
    n = len(node.values)
    features = {
        "prior_latency_p50_ms": ecdf_quantile(node.values, 0.5),
        "prior_latency_p90_ms": ecdf_quantile(node.values, 0.9),
    }
    features.update(
        {
            _tau_feature_name(tau): (n - bisect_right(node.values, tau)) / n
            for tau in taus
        }
    )
    return features


def _targets(
    sample: ResourceCallSample,
) -> tuple[dict[str, float], dict[str, bool]]:
    latency_eligible = not sample.censored
    peak_cpu_eligible = (
        sample.peak_cpu_cores_eligible and sample.peak_cpu_cores is not None
    )
    peak_memory_eligible = (
        sample.peak_memory_mb_eligible and sample.peak_memory_mb is not None
    )
    masks = {
        "latency_ms": latency_eligible,
        "peak_cpu_cores": peak_cpu_eligible,
        "peak_memory_mb": peak_memory_eligible,
    }
    values = {
        "latency_ms": _latency_ms(sample) if latency_eligible else math.nan,
        "peak_cpu_cores": (
            float(sample.peak_cpu_cores) if peak_cpu_eligible else math.nan
        ),
        "peak_memory_mb": (
            float(sample.peak_memory_mb) if peak_memory_eligible else math.nan
        ),
    }
    return values, masks


def _byte_to_character_offsets(command: str) -> tuple[int, ...]:
    encoded = command.encode()
    offsets = [-1] * (len(encoded) + 1)
    byte_offset = 0
    for character_offset, character in enumerate(command):
        offsets[byte_offset] = character_offset
        byte_offset += len(character.encode())
    offsets[byte_offset] = len(command)
    return tuple(offsets)


def _clause_from_adapter(
    command: str,
    raw_clause: object,
    byte_to_character: Sequence[int],
) -> dict[str, Any]:
    if not isinstance(raw_clause, dict):
        raise MvdanClientError("mvdan adapter returned a non-object clause")
    raw_span = raw_clause.get("span")
    argv = raw_clause.get("argv")
    if (
        not isinstance(raw_span, list)
        or len(raw_span) != 2
        or not all(isinstance(offset, int) for offset in raw_span)
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(argument, str) for argument in argv)
    ):
        raise MvdanClientError("mvdan adapter returned an invalid clause")
    byte_start, byte_end = raw_span
    if (
        byte_start < 0
        or byte_end < byte_start
        or byte_end >= len(byte_to_character)
        or byte_to_character[byte_start] < 0
        or byte_to_character[byte_end] < 0
    ):
        raise MvdanClientError(f"mvdan adapter returned invalid byte span {raw_span}")
    start = byte_to_character[byte_start]
    end = byte_to_character[byte_end]
    raw_words = raw_clause.get("words")
    if not isinstance(raw_words, list):
        raise MvdanClientError("mvdan adapter returned invalid word intents")
    structural_context = raw_clause.get("structural_context")
    if not isinstance(structural_context, list) or not all(
        isinstance(item, str) for item in structural_context
    ):
        raise MvdanClientError("mvdan adapter returned invalid structural context")

    def intent_span(raw: object) -> tuple[int, int]:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not all(isinstance(offset, int) for offset in raw)
            or raw[0] < 0
            or raw[1] < raw[0]
            or raw[1] >= len(byte_to_character)
            or byte_to_character[raw[0]] < 0
            or byte_to_character[raw[1]] < 0
        ):
            raise MvdanClientError("mvdan adapter returned invalid word span")
        return byte_to_character[raw[0]], byte_to_character[raw[1]]

    word_intents: list[dict[str, Any]] = []
    for raw_word in raw_words:
        if not isinstance(raw_word, dict) or not isinstance(
            raw_word.get("components"), list
        ):
            raise MvdanClientError("mvdan adapter returned invalid word intent")
        components: list[dict[str, Any]] = []
        for raw_component in raw_word["components"]:
            if (
                not isinstance(raw_component, dict)
                or raw_component.get("kind")
                not in {
                    "literal",
                    "parameter",
                    "command_substitution",
                    "arithmetic_expansion",
                    "process_substitution",
                    "pathname_expansion",
                    "unsupported",
                }
                or not isinstance(raw_component.get("source"), str)
                or not isinstance(raw_component.get("quoted"), bool)
                or not isinstance(raw_component.get("escaped"), bool)
            ):
                raise MvdanClientError(
                    "mvdan adapter returned invalid word component"
                )
            components.append(
                {
                    "kind": raw_component["kind"],
                    "source": raw_component["source"],
                    "span": intent_span(raw_component.get("span")),
                    "quoted": raw_component["quoted"],
                    "escaped": raw_component["escaped"],
                }
            )
        if (
            not isinstance(raw_word.get("cooked"), str)
            or not isinstance(raw_word.get("source"), str)
            or not isinstance(raw_word.get("quoted"), bool)
            or not isinstance(raw_word.get("escaped"), bool)
        ):
            raise MvdanClientError("mvdan adapter returned invalid word intent")
        word_intents.append(
            {
                "cooked": raw_word["cooked"],
                "source": raw_word["source"],
                "span": intent_span(raw_word.get("span")),
                "quoted": raw_word["quoted"],
                "escaped": raw_word["escaped"],
                "components": components,
            }
        )
    if word_intents and [word["cooked"] for word in word_intents] != argv:
        raise MvdanClientError("mvdan adapter word intents disagree with argv")
    return {
        "bin": str(raw_clause["bin"]),
        "argv": argv,
        "original": command[start:end],
        "span": (start, end),
        "in_loop": bool(raw_clause["in_loop"]),
        "in_pipe": bool(raw_clause["in_pipe"]),
        "in_subst": bool(raw_clause["in_subst"]),
        "pipeline_position": int(raw_clause["pipeline_position"]),
        "structural_context": structural_context,
        "word_intents": word_intents,
    }


def _control_edge_from_adapter(
    raw_edge: object,
    edge_id: int,
    clause_count: int,
    byte_to_character: Sequence[int],
) -> dict[str, Any]:
    if (
        not isinstance(raw_edge, dict)
        or raw_edge.get("id") != edge_id
        or raw_edge.get("operator") not in {"&&", "||"}
    ):
        raise MvdanClientError("mvdan adapter returned an invalid control edge")

    def operand(name: str) -> dict[str, Any]:
        raw = raw_edge.get(name)
        if not isinstance(raw, dict):
            raise MvdanClientError("mvdan adapter returned an invalid control operand")
        kind, index, indices = raw.get("kind"), raw.get("index"), raw.get(
            "clause_indices"
        )
        raw_span = raw.get("span")
        negated = raw.get("negated")
        contains_pipeline = raw.get("contains_pipeline")
        contains_subshell = raw.get("contains_subshell")
        if (
            kind not in {"clause", "edge", "unsupported"}
            or not isinstance(index, int)
            or not isinstance(indices, list)
            or not all(
                isinstance(item, int) and 0 <= item < clause_count
                for item in indices
            )
            or (kind == "clause" and (index not in indices or len(indices) != 1))
            or (kind == "edge" and not 0 <= index < edge_id)
            or (kind == "unsupported" and index != -1)
            or not isinstance(raw_span, list)
            or len(raw_span) != 2
            or not all(isinstance(offset, int) for offset in raw_span)
            or raw_span[0] < 0
            or raw_span[1] < raw_span[0]
            or raw_span[1] >= len(byte_to_character)
            or byte_to_character[raw_span[0]] < 0
            or byte_to_character[raw_span[1]] < 0
            or not isinstance(negated, bool)
            or not isinstance(contains_pipeline, bool)
            or not isinstance(contains_subshell, bool)
        ):
            raise MvdanClientError("mvdan adapter returned an invalid control operand")
        return {
            "kind": kind,
            "index": index,
            "clause_indices": indices,
            "span": (
                byte_to_character[raw_span[0]],
                byte_to_character[raw_span[1]],
            ),
            "negated": negated,
            "contains_pipeline": contains_pipeline,
            "contains_subshell": contains_subshell,
        }

    return {
        "id": edge_id,
        "operator": raw_edge["operator"],
        "lhs": operand("lhs"),
        "rhs": operand("rhs"),
    }


def _fallback_clauses(command: str) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for segment in shell_command_segments(command):
        groups: list[list[str]] = [[]]
        for token in segment:
            if token in {"|", "|&", "&"}:
                if groups[-1]:
                    groups.append([])
            else:
                groups[-1].append(token)
        groups = [group for group in groups if group]
        for position, group in enumerate(groups):
            clause = _clause_from_words(group)
            if clause is None:
                continue
            clauses.append(
                {
                    **clause,
                    "original": command,
                    "span": (0, len(command)),
                    "in_loop": False,
                    "in_pipe": len(groups) > 1,
                    "in_subst": False,
                    "pipeline_position": position if len(groups) > 1 else -1,
                }
            )
    return clauses


def _clause_from_words(words: Sequence[str]) -> dict[str, Any] | None:
    start = next(
        (index for index, word in enumerate(words) if not _ENV_ASSIGNMENT.match(word)),
        None,
    )
    if start is None:
        return None
    argv = list(words[start:])
    return {"bin": argv[0].rsplit("/", 1)[-1], "argv": argv}


def _command(sample: ResourceCallSample) -> str:
    args = sample.tool_args
    if sample.tool_name == "exec" and isinstance(args, dict):
        command = args.get("command")
        if isinstance(command, str):
            return command.strip()
    return ""


def _prefix_key(sample: ResourceCallSample) -> str:
    keys = _PREFIX_KEYS({"tool_name": sample.tool_name, "tool_args": sample.tool_args})
    return keys[-1] if keys else ""


def _history_value(history: _History | None, field: str) -> float:
    value = getattr(history, field) if history is not None else None
    return float(value) if value is not None else 0.0


def _latency_ms(sample: ResourceCallSample) -> float:
    return (sample.tool_ts_end - sample.tool_ts_start) * 1000.0


def _cost_value(entry: Mapping[str, float], field: str) -> float:
    value = float(entry.get(field, 0.0))
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"cost_table {field} must be finite and non-negative")
    return value


def _git_subcommand(args: Sequence[str]) -> str:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
    return args[index] if index < len(args) else ""


def _has_error_or_timeout(sample: ResourceCallSample) -> bool:
    result = str(getattr(sample, "tool_result", "") or "").lower()
    return "error" in result or "timeout" in result


def _normalized_taus(taus: Sequence[float]) -> tuple[float, ...]:
    values = tuple(sorted({float(tau) for tau in taus}))
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("taus must be finite and non-negative")
    return values


def _tau_feature_name(tau: float) -> str:
    return f"prior_latency_survival_gt_{tau:g}_ms"


__all__ = [
    "TARGET_NAMES",
    "TabularDataset",
    "assign_repo_folds",
    "build_tabular_dataset",
    "parse_command_clauses",
    "repo_of",
]
