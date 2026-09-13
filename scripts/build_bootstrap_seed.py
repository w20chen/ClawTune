"""Build the small, repository-neutral release prior from historical clauses.

No model, Docker, or external trace writes. The default source is an immutable
Git object so the removed historical bundle need not remain in the checkout.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/sidecar/src"))

from clawtune_kb import FILES, create_seed
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, RuntimeToolResourceKB
from tool_time.lattice_kb import LatticeTimeKB

SOURCE_OBJECT = "41e993395d82723deb7d4193467dc3bd5283574a:seeds/demo-v1/clause-lattice-time-kb.json"
BINS = ("cat", "find", "grep", "ls", "which")
PER_BIN = 8
SOURCE_QUOTA_CORES = 8
MIN_RESOURCE_DURATION_MS = 20


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def select_observations(payload):
    """Balance source repositories, retain duration strata, then erase identity."""
    by_bin_repo = defaultdict(lambda: defaultdict(list))
    for row in payload["observations"]:
        if row.get("bin") not in BINS or any(row.get(k) for k in ("in_pipe", "in_loop", "in_subst")):
            continue
        if not finite(row.get("latency_ms")) or row["latency_ms"] <= 0:
            continue
        if not isinstance(row.get("repo"), str) or not row["repo"]:
            continue
        by_bin_repo[row["bin"]][row["repo"]].append(row)
    selected, coverage = [], {}
    def order(row):
        return (row["latency_ms"], json.dumps(row, sort_keys=True))
    for bin_ in BINS:
        groups = by_bin_repo[bin_]
        # One median observation per source repository avoids letting a large
        # project dominate. Midpoint quantiles preserve the duration range;
        # selecting only fast commands would produce an optimistic prior.
        candidates = sorted((sorted(rows, key=order)[len(rows) // 2]
                             for rows in groups.values()), key=order)
        if len(candidates) < PER_BIN:
            raise ValueError(f"{bin_}: need at least {PER_BIN} independent source repositories")
        coverage[bin_] = {"candidate_repositories": len(candidates), "selected": PER_BIN}
        for index in range(PER_BIN):
            row = candidates[int((index + .5) * len(candidates) / PER_BIN)]
            latency = row["latency_ms"]
            cpu = row.get("cpu_ns_cumulative")
            memory = row.get("sampled_peak_rss_mb")
            peak = row.get("peak_cpu_cores")
            # Historical CPU accounting is coarse and short RSS windows are
            # under-sampled. Keep duration but withhold these resource labels.
            if (latency < MIN_RESOURCE_DURATION_MS or not finite(cpu)
                    or not 0 < cpu <= latency * 1e6 * SOURCE_QUOTA_CORES):
                cpu = None
            if (latency < MIN_RESOURCE_DURATION_MS or not finite(memory)
                    or memory * 1e6 < 65536):
                memory = None
            if latency < 500 or not finite(peak) or not 0 <= peak <= SOURCE_QUOTA_CORES:
                peak = None
            # Executable-only aggregation deliberately removes all argument,
            # path, search-pattern, repo, and task-specific matching features.
            selected.append(ClauseObservation(
                repo="", bin=bin_, argv=(bin_,), ts_start=0,
                ts_end=latency / 1000, latency_ms=latency,
                cpu_ns_cumulative=cpu, sampled_peak_rss_mb=memory,
                cpu_peak_cores=peak,
            ))
    return selected, coverage


def build(output: Path, source: Path | None = None):
    if source is not None:
        raw = source.read_bytes()
    else:
        try:
            raw = subprocess.check_output(
                ["git", "show", SOURCE_OBJECT], cwd=ROOT, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise ValueError(
                "Historical source snapshot is unavailable in this checkout. "
                "Use --source <original-finalized-snapshot.json> or a checkout "
                f"containing {SOURCE_OBJECT}. Normal startup uses the bundled seed "
                "and does not require this history."
            ) from exc
    payload = json.loads(raw)
    if payload.get("schema") != "clause_lattice_kb_v2" or payload.get("pending"):
        raise ValueError("source must be a finalized clause lattice v2 snapshot")
    observations, coverage = select_observations(payload)
    trie = ClauseResourceKB.fit_public(observations)
    tool = RuntimeToolResourceKB()
    lattice = LatticeTimeKB.fit(observations)
    counts = Counter(duration_ms=len(observations))
    for observation in observations:
        if observation.cpu_ns_cumulative is not None:
            counts.update(("cpu_time_seconds", "cpu_avg_cores"))
        if observation.cpu_peak_cores is not None:
            counts.update(("cpu_peak_cores",))
    return create_seed(output, dict(zip(FILES, (
        trie.to_json_obj(), tool.to_json_obj(), lattice.to_json_obj()))), provenance={
            "kind": "repository-neutral-bootstrap", "recipe_version": 2,
            "source_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "source": "historical SWE-Rebench clause training data",
            "source_quota_cores": SOURCE_QUOTA_CORES,
            "selection": "one median per source repository, then eight midpoint duration quantiles per executable",
            "identity_policy": "no repository nodes, names, task IDs, original argv, paths, or search patterns; executable-only priors",
            "resource_policy": {"min_duration_ms": MIN_RESOURCE_DURATION_MS,
                                "min_rss_bytes": 65536, "peak_window_ms": 500},
            "bin_coverage": coverage, "observation_count": len(observations),
            "target_counts": dict(counts),
            "ToolKB": "empty: source has no eligible whole-call or PMU labels; learns online",
            "limitations": "small uncalibrated SWE-derived prior under source conditions; not independent held-out evaluation, not task replay or hardware-invariant demand",
        })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional finalized historical LatticeKB snapshot")
    parser.add_argument("--output", type=Path, required=True, help="New seed directory (never overwritten)")
    args = parser.parse_args()
    try:
        manifest = build(args.output, args.source)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest["provenance"], indent=2))
