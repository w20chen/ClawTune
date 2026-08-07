"""Offline bucket sweep over lattice point predictions.

Point predictions (``predicted_ms``/``actual_ms``) are independent of the
bucket edges, so the bucketed-classification accuracy for shrinkage / loso /
max_cardinality can be recomputed for any edge set without re-running the
evaluation.  This shows how much the headline bucket accuracy moves purely as
a function of where the boundaries are drawn.

Usage::

    python scripts/sweep_buckets.py <report.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ALGORITHMS = ("shrinkage", "loso", "max_cardinality")

_EDGE_SETS = [
    ("100/1000/10000/60000 (current)", (100.0, 1000.0, 10000.0, 60000.0)),
    ("200/1000/10000/60000", (200.0, 1000.0, 10000.0, 60000.0)),
    ("100/2000/10000/60000", (100.0, 2000.0, 10000.0, 60000.0)),
    ("500/2000/10000/60000", (500.0, 2000.0, 10000.0, 60000.0)),
    ("200/2000/20000/120000", (200.0, 2000.0, 20000.0, 120000.0)),
    ("100/500/2000/10000 (original)", (100.0, 500.0, 2000.0, 10000.0)),
    ("300/3000/30000/120000", (300.0, 3000.0, 30000.0, 120000.0)),
    ("2000/10000/60000 (4 buckets)", (2000.0, 10000.0, 60000.0)),
    ("500/1000/10000 (4 buckets)", (500.0, 1000.0, 10000.0)),
    ("100/1000/10000 (4 buckets)", (100.0, 1000.0, 10000.0)),
]


def _bucket_id(ms: float, edges: tuple[float, ...]) -> int:
    for index, edge in enumerate(edges):
        if ms < edge:
            return index
    return len(edges)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    records = report["records"]

    header = " | ".join(name for name, _ in _EDGE_SETS)
    print(f"{'algorithm':<14s} | {header}")
    print("-" * 40 + "+" + "-" * (len(header) + 4))

    for algorithm in _ALGORITHMS:
        recs = [
            r for r in records.get(algorithm, [])
            if r.get("predicted_ms") is not None and r.get("actual_ms") is not None
        ]
        if not recs:
            print(f"{algorithm:<14s} | no records")
            continue
        cells = []
        for _, edges in _EDGE_SETS:
            correct = sum(
                1
                for r in recs
                if _bucket_id(r["predicted_ms"], edges)
                == _bucket_id(r["actual_ms"], edges)
            )
            cells.append(f"{correct / len(recs) * 100:.1f}%")
        print(f"{algorithm:<14s} | " + " | ".join(cells))

    # Actual bucket distribution under each edge set (from loso records).
    actuals = [
        r["actual_ms"] for r in records.get("loso", []) if r.get("actual_ms") is not None
    ]
    print("\nActual-bucket distribution per edge set:")
    for name, edges in _EDGE_SETS:
        dist: dict[int, int] = {}
        for a in actuals:
            b = _bucket_id(a, edges)
            dist[b] = dist.get(b, 0) + 1
        print(f"  {name:<30s} {dict(sorted(dist.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
