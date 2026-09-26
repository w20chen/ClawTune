"""Replay the first N qualified trace-v5 cases into the four bundled KBs.

Reads source traces only. Build into a new directory, validate, then publish the
bundle to seeds/bootstrap-v1. No model calls or recorded commands are executed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/sidecar/src"))

from clawtune_kb import FILES, create_seed
from clawtune_kb.store import digest
from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import plain_execution, predict_call_load
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime, training_event
from cold_start.flat_loader import read_task, valid
from offline.runner import inventory
from tool_resource.runtime_kb import (
    ClauseResourceKB, RuntimeToolResourceKB, ToolCallQuery, _target_values,
    LOAD_TARGET_SOURCES, _clause_value,
)
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import LOAD_TARGETS
from tool_resource.commands import normalized_observation

BUCKET_EDGES = (100, 500, 2000, 10000)


def _candidate_start(dataset, task):
    """Cheap eligibility scan; the shared loader subsequently checks each label."""
    start = math.inf
    has_clause = False
    for file in task["files"]:
        if file["version"] != 5:
            raise ValueError("bootstrap replay currently requires trace-v5 input")
        with (dataset / file["path"]).open("rb") as source:
            for line in source:
                if b'"tool_exec"' not in line:
                    continue
                row = json.loads(line)
                if row.get("type") != "action" or row.get("action_type") != "tool_exec":
                    continue
                if valid(row.get("ts_start")):
                    start = min(start, row["ts_start"])
                observation = (row.get("data") or {}).get("resource_observation") or {}
                if (observation.get("eligible_for_kb") is not True
                        or observation.get("telemetry_quality") != "ok"
                        or observation.get("telemetry_status") != "ok"):
                    continue
                has_clause |= any(
                    c.get("eligible_for_kb") is True
                    and (c.get("availability") or {}).get("latency") == "ok"
                    and valid(c.get("latency_ms"))
                    for c in observation.get("clauses", [])
                )
    return start if has_clause and math.isfinite(start) else None


def select_cases(dataset, count, rss_unit):
    excluded = []
    tasks = inventory(dataset, "swe-rebench", excluded)
    # A case can have multiple recorded attempts. Choose one complete eligible
    # attempt per case, ordered by that attempt's own clock, not an older run
    # of the same case that had no clause telemetry.
    candidates = sorted(
        (start, key, file["path"], file)
        for key, task in tasks.items() for file in task["files"]
        if (start := _candidate_start(dataset, {"files": [file]})) is not None
    )
    selected = []
    selected_ids = set()
    rejected = []
    for start, key, _, file in candidates:
        if key in selected_ids:
            continue
        task = tasks[key]
        path = dataset / file["path"]
        if digest(path) != file["sha256"]:
            raise ValueError(f"source changed: {file['path']}")
        loaded = read_task(path, repo=task["group"], task_id=task["task_id"],
                           rss_unit=rss_unit, preserve_timestamps=True)
        if not any(training_event(row) is not None for row in loaded.clauses):
            rejected.append({"task": key, "file": file["path"],
                             "reason": "no_eligible_timed_clause"})
            continue
        selected.append((key, task, start, [(file, loaded)]))
        selected_ids.add(key)
        if len(selected) == count:
            return selected, excluded + rejected
    raise ValueError(f"need {count} cases with eligible timed clauses; found {len(selected)}")


def replay(selected, *, progress=print):
    trie, tool, lattice = ClauseResourceKB(), RuntimeToolResourceKB(), LatticeTimeKB()
    edge = EdgeKappaRuntime.fit((), BUCKET_EDGES)
    edges = load_bucket_edges(BUCKET_EDGES)
    counts = Counter()
    targets = {name: Counter() for name in ("tool", "trie", "lattice", "edge_kappa")}
    events = []
    for index, (key, task, _, files) in enumerate(selected):
        for file, loaded in files:
            counts.update(loaded.counts)
            for action in loaded.actions:
                identity = hashlib.sha256(
                    f"{key}\0{file['path']}\0{action.action_id}".encode()
                ).hexdigest()
                # Starts precede completions at equal times; KBs independently
                # require end < next query start before making samples visible.
                for timestamp, phase in ((action.ts_start, 0), (action.ts_end, 1)):
                    events.append((timestamp, phase, index, identity, task["group"], action, loaded))
    for timestamp, phase, _, identity, repo, action, loaded in sorted(events, key=lambda e: e[:4]):
        if phase == 0:
            _, diagnostics = predict_call_load(
                runtime=tool, trie=trie, lattice=lattice, edge_kappa=edge,
                query=ToolCallQuery(repo, action.tool_name, action.command, timestamp),
                edges=edges, edge_call_id=identity,
            )
            for backend, result in diagnostics.backends.items():
                for target, prediction in result.targets.items():
                    reason = prediction.unavailable_reason or ""
                    if reason.startswith(("backend_error:", "parse_error:")):
                        raise ValueError(f"{identity}: {backend}/{target}: {reason}")
            counts["prediction_calls"] += 1
        else:
            for row in action.clauses:
                normalized = normalized_observation(row)
                values = {target: value for target in LOAD_TARGETS
                          if (value := _clause_value(normalized, "load:" + target)) is not None}
                if trie.observe_completed_clause(row):
                    targets["trie"].update(values.keys())
                if lattice.observe_completed_clause(row):
                    targets["lattice"].update(values.keys())
            edge.complete_call(identity, action.clauses, commit_time=timestamp)
            if action.call_index is not None:
                call = loaded.calls[action.call_index]
                tool.observe_completed_call(call)
                values = _target_values(call)
                targets["tool"].update(target for target, source in LOAD_TARGET_SOURCES.items()
                                       if source in values)
            counts["completed_calls"] += 1
            if counts["completed_calls"] % 50 == 0:
                progress(f"replayed {counts['completed_calls']} calls", flush=True)
    if not events:
        raise ValueError("selected cases contain no replayable actions")
    horizon = math.nextafter(max(event[0] for event in events), math.inf)
    trie._absorb_completed(horizon)
    tool._absorb_completed(horizon)
    lattice._advance(horizon)
    edge.kb.commit_before(horizon)
    for kb in (trie, tool, lattice):
        kb.freeze()
    edge.kb.freeze()
    edge.frozen = True
    edge_snapshot = edge.to_snapshot()
    if edge_snapshot["tokens"] or edge_snapshot["pending"] or edge_snapshot["runtime"]["calls"]:
        raise ValueError("replay left unfinished Edge predictions")
    targets["edge_kappa"]["duration_ms"] = len(edge_snapshot["observations"])
    payloads = dict(zip(FILES, (trie.to_json_obj(), tool.to_json_obj(), lattice.to_json_obj())))
    payloads["edge-kappa-kb.json"] = edge_snapshot
    return payloads, counts, targets


def build(output: Path, dataset: Path, *, cases: int = 10, rss_unit: str = "MiB", progress=print):
    if cases < 1:
        raise ValueError("cases must be positive")
    dataset, output = dataset.resolve(strict=True), output.resolve()
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError("output must be separate from the read-only dataset")
    if output.exists():
        raise FileExistsError("build into a new directory; publish the validated bundle separately")
    # Fail before reading the corpus if the syntax parser is unavailable.
    clauses, reason = plain_execution("python -V")
    if not clauses or reason:
        raise ValueError(f"shell parser is unavailable: {reason}")
    selected, excluded = select_cases(dataset, cases, rss_unit)
    progress("selected: " + ", ".join(item[1]["task_id"] for item in selected), flush=True)
    payloads, counts, targets = replay(selected, progress=progress)
    return create_seed(output, payloads, provenance={
        "kind": "recorded-case-prefix-replay", "recipe_version": 3,
        "dataset": dataset.name, "case_count": cases, "rss_unit": rss_unit,
        "selection": "one earliest eligible complete attempt per case, ordered by first tool start time; task ID and path break ties",
        "replay": "empty KBs; predict from command before observing completion; recorded event-time order; no public-prior fitting",
        "cases": [{"task_id": task["task_id"], "repo": task["group"], "first_tool_start": start,
                   "files": [file for file, _ in files]} for _, task, start, files in selected],
        "excluded": excluded, "counts": dict(counts),
        "target_counts": {name: {target: values[target] for target in LOAD_TARGETS}
                          for name, values in targets.items()},
        "unavailable_targets": {name: [target for target in LOAD_TARGETS if not values[target]]
                                for name, values in targets.items()},
        "missing_data_policy": "skip absent or unqualified labels per target; no zeros or call-to-clause substitution; no PMU/environment-memory reconstruction",
        "limitations": "recorded observations only, not rerunning commands or reconstructing unrecorded runtime state; repository-specific history is retained",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory; never overwritten")
    parser.add_argument("--cases", type=int, default=10)
    parser.add_argument("--rss-unit", choices=("MB", "MiB"), default="MiB")
    args = parser.parse_args()
    result = build(args.output, args.dataset, cases=args.cases, rss_unit=args.rss_unit)
    print(json.dumps(result["provenance"]["target_counts"], indent=2))
