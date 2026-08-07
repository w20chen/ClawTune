"""Analyze the worst lattice point predictions and the exact-match / trivial-pipe gap.

Usage::

    python scripts/analyze_lattice_worst.py <report.json> [algorithm]

Reads a legacy_eval report.json (the ``records`` of one lattice algorithm),
prints the worst 20 cases by absolute error and by relative error, the worst
20 exact-match errors, plus an exact-match / trivial-pipe-tool breakdown that
explains part of the gap versus the standalone ``latt`` evaluation.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

_TRIVIAL_PIPE_BINS = {"tail", "head", "wc", "cat", "tee", "cut", "tr"}


def _load_records(report_path: Path, algorithm: str) -> list[dict]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    records = payload["records"].get(algorithm, [])
    return [r for r in records if r.get("predicted_ms") is not None]


def _bucket(ms: float) -> int:
    for i, edge in enumerate((100.0, 1000.0, 10000.0, 60000.0)):
        if ms < edge:
            return i
    return 4


def _print_case(r: dict, tag: str) -> None:
    actual = r["actual_ms"]
    pred = r["predicted_ms"]
    print(
        f"[{tag:>12s}] repo={r.get('repo')} bin={r['bin']} argv={list(r['argv'])} "
        f"actual={actual:.1f}ms pred={pred:.1f}ms "
        f"err={abs(pred - actual):.1f}ms rel={abs(pred - actual) / max(actual, 1e-9):.2f} "
        f"exact={r.get('exact_match')} evid={r.get('evidence_count')}"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    report_path = Path(sys.argv[1])
    algorithm = sys.argv[2] if len(sys.argv) > 2 else "loso"
    records = _load_records(report_path, algorithm)
    if not records:
        print(f"no predicted records for {algorithm}")
        return 1

    by_abs = sorted(records, key=lambda r: abs(r["predicted_ms"] - r["actual_ms"]), reverse=True)
    by_rel = sorted(
        (r for r in records if r["actual_ms"] > 0.0),
        key=lambda r: abs(r["predicted_ms"] - r["actual_ms"]) / r["actual_ms"],
        reverse=True,
    )
    exact_wrong = sorted(
        (r for r in records if r.get("exact_match") is True),
        key=lambda r: abs(r["predicted_ms"] - r["actual_ms"]),
        reverse=True,
    )

    print(f"== {algorithm}: n={len(records)} (predicted-only) ==")
    print(f"exact_match rate: {sum(1 for r in records if r.get('exact_match') is True) / len(records):.1%}")

    # exact vs non-exact bucket accuracy
    for label, subset in (("exact", [r for r in records if r.get("exact_match") is True]),
                          ("non-exact", [r for r in records if r.get("exact_match") is not True])):
        if not subset:
            continue
        acc = sum(1 for r in subset if _bucket(r["predicted_ms"]) == _bucket(r["actual_ms"])) / len(subset)
        print(f"  {label:9s} bucket-acc={acc:.1%}  n={len(subset)}  "
              f"medAE={statistics.median(abs(r['predicted_ms'] - r['actual_ms']) for r in subset):.1f}ms")

    # trivial pipe tools
    tp = [r for r in records if r["bin"] in _TRIVIAL_PIPE_BINS]
    if tp:
        acc = sum(1 for r in tp if _bucket(r["predicted_ms"]) == _bucket(r["actual_ms"])) / len(tp)
        print(f"  trivial-pipe n={len(tp)} ({len(tp) / len(records):.1%}) bucket-acc={acc:.1%}")
    else:
        print("  trivial-pipe n=0")

    print(f"\n-- {algorithm} worst 20 by ABSOLUTE error --")
    for r in by_abs[:20]:
        _print_case(r, "abs")

    print(f"\n-- {algorithm} worst 20 by RELATIVE error --")
    for r in by_rel[:20]:
        _print_case(r, "rel")

    print(f"\n-- {algorithm} worst 20 EXACT-MATCH errors --")
    for r in exact_wrong[:20]:
        _print_case(r, "exact")

    # bucket distribution of the worst 20 by absolute error
    worst = by_abs[:20]
    dist = {}
    for r in worst:
        key = f"b{_bucket(r['actual_ms'])}"
        dist[key] = dist.get(key, 0) + 1
    print(f"\nworst-20 actual-bucket distribution: {dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
