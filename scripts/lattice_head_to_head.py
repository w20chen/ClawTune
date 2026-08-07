"""Head-to-head: shrinkage vs max_cardinality (and loso) on the bucket metric.

Finds where shrinkage is strictly better than max_cardinality (correct bucket
while max_card is wrong), where it is worse, and prints examples so we can see
whether the difference comes from shrinkage's variance pooling / risk selection
on noisy or low-evidence nodes.

Usage::

    python scripts/lattice_head_to_head.py <report.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_EDGES = (600.0, 2000.0, 10000.0, 60000.0)
_ALGORITHMS = ("shrinkage", "loso", "max_cardinality")


def _bucket(ms: float) -> int:
    for index, edge in enumerate(_EDGES):
        if ms < edge:
            return index
    return len(_EDGES)


def _sig(r: dict) -> tuple:
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
        alg: {
            _sig(r): r
            for r in records.get(alg, [])
            if r.get("predicted_ms") is not None and r.get("actual_ms") is not None
        }
        for alg in _ALGORITHMS
    }
    base = {s for s in by_alg["shrinkage"] if s in by_alg["max_cardinality"]}

    def _correct(alg: str, sig: tuple) -> bool:
        r = by_alg[alg][sig]
        return _bucket(r["predicted_ms"]) == _bucket(r["actual_ms"])

    shrink_wins = [s for s in base if _correct("shrinkage", s) and not _correct("max_cardinality", s)]
    max_wins = [s for s in base if _correct("max_cardinality", s) and not _correct("shrinkage", s)]
    both = [s for s in base if _correct("shrinkage", s) and _correct("max_cardinality", s)]
    neither = [s for s in base if not _correct("shrinkage", s) and not _correct("max_cardinality", s)]

    print(f"total={len(base)}")
    print(f"  both correct          : {len(both)}")
    print(f"  neither               : {len(neither)}")
    print(f"  shrinkage-only correct: {len(shrink_wins)}")
    print(f"  max_card-only correct : {len(max_wins)}")

    print("\n-- 8 cases where SHRINKAGE is right and MAX_CARDINALITY is wrong --")
    for sig in sorted(shrink_wins, key=lambda s: abs(by_alg["max_cardinality"][s]["predicted_ms"] - by_alg["max_cardinality"][s]["actual_ms"]), reverse=True)[:8]:
        a = by_alg["shrinkage"][sig]
        m = by_alg["max_cardinality"][sig]
        print(
            f"  repo={a.get('repo')} bin={a['bin']} argv={a['argv']} "
            f"actual={a['actual_ms']:.0f}ms | shrink={a['predicted_ms']:.0f}ms "
            f"(exact={a.get('exact_match')}, evid={a.get('evidence_count')}) | "
            f"maxcard={m['predicted_ms']:.0f}ms (exact={m.get('exact_match')}, evid={m.get('evidence_count')})"
        )
    print("\n-- 8 cases where MAX_CARDINALITY is right and SHRINKAGE is wrong --")
    for sig in sorted(max_wins, key=lambda s: abs(by_alg["shrinkage"][s]["predicted_ms"] - by_alg["shrinkage"][s]["actual_ms"]), reverse=True)[:8]:
        a = by_alg["shrinkage"][sig]
        m = by_alg["max_cardinality"][sig]
        print(
            f"  repo={a.get('repo')} bin={a['bin']} argv={a['argv']} "
            f"actual={a['actual_ms']:.0f}ms | shrink={a['predicted_ms']:.0f}ms "
            f"(exact={a.get('exact_match')}, evid={a.get('evidence_count')}) | "
            f"maxcard={m['predicted_ms']:.0f}ms (exact={m.get('exact_match')}, evid={m.get('evidence_count')})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
