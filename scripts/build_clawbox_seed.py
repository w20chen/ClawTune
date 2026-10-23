"""Build a versioned ClawTune seed from validated ClawBox observations.

The input is evidence, not a KB snapshot. ClawTune owns deserialization,
training, seed creation, and the immutable manifest. The output directory is
never overwritten.
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
sys.path.insert(0, str(ROOT / "services/sidecar/src"))

from clawtune_kb import FILES, create_seed, validate_seed
from clawtune_kb.contracts import validate
from clawtune_kb.store import OPTIONAL_FILES, digest
from tool_resource.runtime_kb import (
    ClauseObservation,
    ClauseResourceKB,
    CompletedCall,
    RuntimeToolResourceKB,
)
from tool_time.lattice_kb import LatticeTimeKB


GUEST_MEMORY_MEASUREMENT = "guest_memtotal_minus_memavailable"


def _load_bundle(path: Path) -> tuple[dict, bytes]:
    raw = path.resolve(strict=True).read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid ClawBox observation bundle {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("ClawBox observation bundle must be an object")
    validate(payload, "clawbox-seed-observations.schema.json")
    return payload, raw


def _observations(payload: dict) -> tuple[
    list[CompletedCall], list[ClauseObservation], dict[str, float],
    dict[str, list[ClauseObservation]],
]:
    calls = []
    call_ids = set()
    host_by_execution = {}
    for record in payload["observations"]["completed_calls"]:
        execution_id = record["execution_id"]
        if execution_id in call_ids:
            raise ValueError(f"duplicate completed-call execution_id: {execution_id}")
        call_ids.add(execution_id)
        calls.append(CompletedCall(**record["call"]))
        host = record["host_cgroup_execution_increment_bytes"]
        if host is not None:
            host_by_execution[execution_id] = float(host)

    clauses = []
    clauses_by_execution = {}
    clause_keys = set()
    for record in payload["observations"]["clauses"]:
        key = (record["execution_id"], record["clause_index"])
        if key in clause_keys:
            raise ValueError(f"duplicate clause observation: {key[0]} index {key[1]}")
        if key[0] not in call_ids:
            raise ValueError(f"clause observation has no completed call: {key[0]}")
        clause_keys.add(key)
        row = dict(record["observation"])
        row["argv"] = tuple(row["argv"])
        observation = ClauseObservation(**row)
        clauses.append(observation)
        clauses_by_execution.setdefault(key[0], []).append(observation)

    validation = payload["validation"]
    if len(calls) != validation["public_prior_completed_call_count"]:
        raise ValueError("completed-call count differs from validation metadata")
    if len(clauses) != validation["lattice_clause_observation_count"]:
        raise ValueError("clause count differs from validation metadata")
    if len({call.repo for call in calls}) != validation["public_prior_repository_count"]:
        raise ValueError("repository count differs from validation metadata")
    return calls, clauses, host_by_execution, clauses_by_execution


def _memory_counts(rows) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter] = {}
    for row in rows:
        measurement = row.memory_measurement or "unavailable"
        bucket = counts.setdefault(measurement, Counter())
        if row.memory_eligible:
            for target in ("memory_total_peak_bytes", "memory_extra_peak_bytes"):
                if getattr(row, target) is not None:
                    bucket[target] += 1
        if getattr(row, "sampled_peak_rss_bytes", None) is not None:
            bucket["sampled_peak_rss_bytes"] += 1
        elif getattr(row, "sampled_peak_rss_mb", None) is not None:
            bucket["sampled_peak_rss_bytes"] += 1
    return {measurement: dict(values) for measurement, values in sorted(counts.items())}


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _host_calibration(pairs: list[tuple[float, float]], signal: str) -> dict:
    pairs = [(float(signal_value), float(host)) for signal_value, host in pairs
             if all(math.isfinite(value) and value >= 0 for value in (signal_value, host))]
    if not pairs:
        raise ValueError(f"no paired host measurements for {signal}")
    ratios = [host / signal_value for signal_value, host in pairs if signal_value > 0]
    scale = _quantile(ratios, .9) if ratios else 0.0
    offset = _quantile(
        [max(0.0, host - scale * signal_value) for signal_value, host in pairs], .9
    )
    return {
        "target": "host_cgroup_memory_current_increment",
        "source_signal": signal,
        "method": "paired_p90_ratio_plus_p90_residual",
        "sample_count": len(pairs),
        "scale": scale,
        "offset_bytes": offset,
    }


def build(output: Path, bundle_path: Path, base_seed: Path) -> dict:
    output = output.resolve()
    base_seed = base_seed.resolve(strict=True)
    base_manifest = validate_seed(base_seed)
    payload, raw = _load_bundle(bundle_path)
    calls, clauses, host_by_execution, clauses_by_execution = _observations(payload)

    call_guest_labels = sum(
        call.memory_eligible
        and call.memory_measurement == GUEST_MEMORY_MEASUREMENT
        and call.memory_extra_peak_bytes is not None
        for call in calls
    )
    clause_guest_labels = sum(
        clause.memory_eligible
        and clause.memory_measurement == GUEST_MEMORY_MEASUREMENT
        and clause.memory_extra_peak_bytes is not None
        for clause in clauses
    )
    if not call_guest_labels or not clause_guest_labels:
        raise ValueError("ClawBox bundle must contain guest extra-memory labels for ToolKB and LatticeKB")

    runtime = RuntimeToolResourceKB.fit_public(calls)
    runtime.freeze()
    clause = ClauseResourceKB.fit_public(clauses)
    clause.freeze()
    lattice = LatticeTimeKB.from_json_obj(json.loads(
        (base_seed / "clause-lattice-time-kb.json").read_text(encoding="utf-8")
    ))
    base_lattice_observations = lattice.observation_count
    added_lattice_observations = lattice.merge_historical(clauses)
    lattice.freeze()

    coverage = {
        "completed_calls": len(calls),
        "clause_observations": len(clauses),
        "repositories": len({call.repo for call in calls}),
        "tool_memory_labels_by_measurement": _memory_counts(calls),
        "clause_memory_labels_by_measurement": _memory_counts(clauses),
        "base_lattice_observations": base_lattice_observations,
        "added_lattice_observations": added_lattice_observations,
        "output_lattice_observations": lattice.observation_count,
    }
    lattice_rss_pairs = [
        (max(float(row.sampled_peak_rss_mb) * 1_000_000.0 for row in rows
             if row.sampled_peak_rss_mb is not None), host)
        for execution_id, host in host_by_execution.items()
        for rows in [clauses_by_execution.get(execution_id, [])]
        if any(row.sampled_peak_rss_mb is not None for row in rows)
    ]
    admission = {
        **payload["admission"],
        "host_calibrations": {
            "guest_memory_extra_peak_bytes": payload["admission"]["host_calibration"],
            "lattice_sampled_peak_rss_bytes": _host_calibration(
                lattice_rss_pairs, "lattice_sampled_peak_rss_bytes"
            ),
        },
    }
    provenance = {
        "kind": "clawbox-qualified-observation-import",
        "recipe_version": 3,
        "base_seed_manifest_sha256": digest(base_seed / "manifest.json"),
        "observation_bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "producer": payload["producer"],
        "source_validation": payload["validation"],
        "coverage": coverage,
        "admission": admission,
        "admission_prediction_policy": [
            {
                "model": "lattice",
                "level": "lattice_guest_extra",
                "target": "memory_extra_peak_bytes",
                "measurement": GUEST_MEMORY_MEASUREMENT,
                "semantics": "exact",
                "host_calibration": "guest_memory_extra_peak_bytes",
            },
            {
                "model": "lattice",
                "level": "lattice_rss_proxy",
                "target": "sampled_peak_rss_bytes",
                "measurement": "ebpf_sampled_distinct_mm_rss_peak",
                "semantics": "proxy",
                "host_calibration": "lattice_sampled_peak_rss_bytes",
            },
            {
                "model": "tool",
                "level": "tool_guest_extra",
                "target": "memory_extra_peak_bytes",
                "measurement": GUEST_MEMORY_MEASUREMENT,
                "semantics": "exact",
                "host_calibration": "guest_memory_extra_peak_bytes",
            },
            {
                "model": "tool_class",
                "level": "tool_class_guest_extra",
                "target": "memory_extra_peak_bytes",
                "measurement": GUEST_MEMORY_MEASUREMENT,
                "semantics": "exact",
                "host_calibration": "guest_memory_extra_peak_bytes",
            },
        ],
        "limitations": (
            "Coverage is measured on the imported corpus, not a held-out accuracy set. "
            "The public ToolKB omits repository and exact-command identity; LatticeKB "
            "retains the base seed and imported clause observations."
        ),
    }
    seed_payloads = dict(zip(FILES, (
        clause.to_json_obj(), runtime.to_json_obj(), lattice.to_json_obj(),
    )))
    retained_optional = [
        name for name in OPTIONAL_FILES if name in base_manifest["snapshots"]
    ]
    for name in retained_optional:
        seed_payloads[name] = json.loads(
            (base_seed / name).read_text(encoding="utf-8")
        )
    provenance["retained_base_optional_snapshots"] = retained_optional
    manifest = create_seed(output, seed_payloads, provenance=provenance)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", required=True, type=Path,
                        help="ClawBox clawtune.seed_observations.v1 JSON")
    parser.add_argument("--base-seed", required=True, type=Path,
                        help="validated immutable ClawTune seed to extend")
    parser.add_argument("--output", required=True, type=Path,
                        help="new ClawTune seed directory (never overwritten)")
    args = parser.parse_args()
    try:
        manifest = build(args.output, args.observations, args.base_seed)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest["provenance"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
