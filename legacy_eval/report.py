"""Human-readable Markdown report for a legacy evaluation run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from legacy_eval.engine import (
    BUCKET_TRACK,
    CONTINUOUS_CPU_TRACK,
    CONTINUOUS_LATENCY_TRACK,
    EvalResult,
    LATTICE_TRACKS,
    TRACKS,
)


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100.0:.1f}%"


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _table(rows: list[tuple[str, str]]) -> str:
    """Render a two-column table with spaced separators (lint-clean)."""

    header = "| metric | value |"
    separator = "| --- | --- |"
    return "\n".join(
        [header, separator, *[f"| {key} | {value} |" for key, value in rows]]
    )


def _bucket_table(summary: dict[str, Any]) -> str:
    return _table(
        [
            ("samples", str(summary["n"])),
            ("coverage", _pct(summary.get("coverage"))),
            ("top-1 bucket accuracy", _pct(summary.get("accuracy"))),
            ("Brier score", _num(summary.get("brier_score"), 4)),
        ]
    )


def _lattice_table(summary: dict[str, Any]) -> str:
    return _table(
        [
            ("samples", str(summary["n"])),
            ("coverage", _pct(summary.get("coverage"))),
            ("MAE (ms)", _num(summary.get("mae_ms"))),
            ("median abs error (ms)", _num(summary.get("median_abs_error_ms"))),
            ("relative error", _num(summary.get("relative_error"))),
            ("mean predicted (ms)", _num(summary.get("mean_predicted_ms"))),
            ("mean actual (ms)", _num(summary.get("mean_actual_ms"))),
            ("predicted p90 (ms)", _num(summary.get("predicted_p90_ms"))),
            ("actual p90 (ms)", _num(summary.get("actual_p90_ms"))),
        ]
    )


def _continuous_table(summary: dict[str, Any]) -> str:
    return _table(
        [
            ("samples", str(summary["n"])),
            ("coverage", _pct(summary.get("coverage"))),
            (
                f"pinball loss (q={summary.get('quantile', 0.9)})",
                _num(summary.get("pinball_q")),
            ),
            ("mean predicted", _num(summary.get("mean_predicted"))),
            ("mean actual", _num(summary.get("mean_actual"))),
            ("predicted q", _num(summary.get("predicted_q"))),
            ("actual q", _num(summary.get("actual_q"))),
        ]
    )


def render_markdown(result: EvalResult) -> str:
    """Render a self-contained Markdown report for *result*."""

    sections: list[str] = []

    header = [
        "# Legacy evaluation report",
        "",
        f"- dataset: `{result.dataset_dir}`",
        (
            f"- split: {len(result.train_ids)} train / {len(result.test_ids)} test "
            f"(train_frac={result.config.train_frac}, seed={result.config.seed})"
        ),
        f"- bucket edges (ms): {list(result.config.bucket_edges_ms)}",
        f"- protocol: {result.meta.get('protocol')}",
        f"- generated: {result.meta.get('generated_at_utc')}",
    ]
    sections.append("\n".join(header))

    counts = result.counts
    counts_table = _table(
        [
            ("train tasks", str(counts["train_tasks"])),
            ("test tasks", str(counts["test_tasks"])),
            (
                "train clause observations (all)",
                str(counts["train_clause_observations"]),
            ),
            (
                "train clause observations (eligible)",
                str(counts["train_clause_observations_eligible"]),
            ),
            ("train successful tool calls", str(counts["train_tool_calls_success"])),
            ("test clause events", str(counts["test_clause_events"])),
            ("test tool calls", str(counts["test_tool_calls"])),
        ]
    )
    sections.append("## Data counts\n\n" + counts_table)

    sections.append(
        f"## `{BUCKET_TRACK}` (ClauseResourceKB, clause-level)\n\n"
        + _bucket_table(result.summaries[BUCKET_TRACK])
    )
    for track in LATTICE_TRACKS:
        sections.append(
            f"## `{track}` (LatticeTimeKB, clause-level point prediction)\n\n"
            + _lattice_table(result.summaries[track])
        )
    sections.append(
        f"## `{CONTINUOUS_LATENCY_TRACK}` "
        "(RuntimeToolResourceKB, call-level p90)\n\n"
        + _continuous_table(result.summaries[CONTINUOUS_LATENCY_TRACK])
    )
    sections.append(
        f"## `{CONTINUOUS_CPU_TRACK}` "
        "(RuntimeToolResourceKB, call-level p90)\n\n"
        + _continuous_table(result.summaries[CONTINUOUS_CPU_TRACK])
    )

    notes = result.meta.get("notes", {})
    note_lines = ["## Notes"]
    if isinstance(notes, dict):
        for key, value in notes.items():
            if value:
                note_lines.append(f"- **{key}**: {value}")
    sections.append("\n".join(note_lines))

    return "\n\n".join(sections) + "\n"


def write_markdown_report(result: EvalResult, path: str | Path) -> Path:
    """Write the rendered Markdown report to *path*."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result), encoding="utf-8")
    return path


__all__ = ["render_markdown", "write_markdown_report"]
