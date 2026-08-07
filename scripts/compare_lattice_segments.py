"""Segment comparison: where does each lattice algorithm win?

Slices the point-prediction records of a report.json by exact/non-exact match,
evidence count, and command family, then reports bucketed-classification
accuracy per algorithm so we can see which segments shrinkage (loso /
max_cardinality) is strongest in.

Usage::

    python scripts/compare_lattice_segments.py <report.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_EDGES = (600.0, 2000.0, 10000.0, 60000.0)
_ALGORITHMS = ("shrinkage", "loso", "max_cardinality")

_FAMILIES = {
    "git": ("git",),
    "python": ("python", "python3", "pytest", "pip", "pip3"),
    "test/build": ("pytest", "tox", "make", "cmake", "ninja", "mvn", "gradle", "cargo"),
    "file/search": ("grep", "rg", "find", "fd", "sed", "awk", "ls", "cat"),
    "net": ("curl", "wget", "pip", "npm", "pip3"),
    "other": (),
}


def _family(bin_name: str) -> str:
    for name, members in _FAMILIES.items():
        if bin_name in members:
            return name
    return "other"


def _bucket(ms: float) -> int:
    for index, edge in enumerate(_EDGES):
        if ms < edge:
            return index
    return len(_EDGES)


def _acc(records: list[dict]) -> float:
    return (
        sum(
            1
            for r in records
            if _bucket(r["predicted_ms"]) == _bucket(r["actual_ms"])
        )
        / len(records)
        if records
        else float("nan")
    )


def _sig(r: dict) -> tuple:
    """Hashable identity of one test clause (same across the three tracks)."""
    return (
        r.get("task_id"),
        r.get("bin"),
        tuple(r.get("argv") or []),
        r.get("actual_ms"),
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    records = report["records"]
    by_alg = {
        alg: [
            r for r in records.get(alg, [])
            if r.get("predicted_ms") is not None and r.get("actual_ms") is not None
        ]
        for alg in _ALGORITHMS
    }

    def _row(label: str, subset: set[tuple] | None) -> None:
        cells = []
        for alg in _ALGORITHMS:
            if subset is None:
                cells.append("n/a")
                continue
            sub = [r for r in by_alg[alg] if _sig(r) in subset]
            cells.append(f"{_acc(sub) * 100:.1f}% (n={len(sub)})")
        print(f"{label:<34s} | " + " | ".join(cells))

    header = " | ".join(f"{a:>10s}" for a in _ALGORITHMS)
    print(f"{'segment':<34s} | {header}")
    print("-" * 34 + "+" + "-" * (len(header) + 4))

    # Base
    base = {_sig(r) for r in by_alg["loso"]}
    _row("ALL", base)

    # Exact vs non-exact
    exact = {_sig(r) for r in by_alg["loso"] if r.get("exact_match") is True}
    non_exact = {_sig(r) for r in by_alg["loso"] if r.get("exact_match") is not True}
    _row("exact_match", exact)
    _row("non-exact", non_exact)

    # Evidence count bins (from loso records as the slice universe)
    def _evid(subset: set[tuple], lo: float, hi: float) -> set[tuple]:
        return {_sig(r) for r in by_alg["loso"] if lo <= r.get("evidence_count", 0) <= hi}

    _row("evid 1-2", _evid(base, 1, 2))
    _row("evid 3-10", _evid(base, 3, 10))
    _row("evid 11-100", _evid(base, 11, 100))
    _row("evid >100", _evid(base, 101, 10**9))

    # Command family
    for fam in _FAMILIES:
        _row(f"family: {fam}", {_sig(r) for r in by_alg["loso"] if _family(r["bin"]) == fam})

    # Repetition proxy: exact + low evidence (near-cold) vs exact + high evidence
    _row("exact & evid<=2", {_sig(r) for r in by_alg["loso"] if r.get("exact_match") is True and r.get("evidence_count", 0) <= 2})
    _row("exact & evid>=5", {_sig(r) for r in by_alg["loso"] if r.get("exact_match") is True and r.get("evidence_count", 0) >= 5})
    _row("non-exact & evid<=2", {_sig(r) for r in by_alg["loso"] if r.get("exact_match") is not True and r.get("evidence_count", 0) <= 2})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
