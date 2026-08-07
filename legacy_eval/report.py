"""Human-readable Markdown report for a legacy evaluation run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from legacy_eval.engine import (
    BUCKET_TRACK,
    CONTINUOUS_CPU_TRACK,
    CONTINUOUS_LATENCY_TRACK,
    CONTINUOUS_MEMORY_TRACK,
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
            ("F1 (macro)", _num(summary.get("f1_macro"))),
            ("F1 (weighted)", _num(summary.get("f1_weighted"))),
            ("precision (macro)", _num(summary.get("precision_macro"))),
            ("recall (macro)", _num(summary.get("recall_macro"))),
            ("Brier score", _num(summary.get("brier_score"), 4)),
        ]
    )


def _bucket_breakdown_table(summary: dict[str, Any]) -> str:
    """Render a per-bucket F1 / accuracy table (rows = buckets).

    accuracy is the per-bucket BucketAccuracy (TP / actual support); F1,
    precision, and recall come from the per-class classification report.
    """

    per_class = summary.get("per_class", {})
    if not isinstance(per_class, dict) or not per_class:
        return ""
    header = "| bucket | count | accuracy | precision | recall | F1 |"
    separator = "| --- | --- | --- | --- | --- | --- |"
    rows: list[str] = []
    for cls in sorted(per_class, key=lambda key: int(key)):
        m = per_class[cls]
        rows.append(
            f"| b{cls} | {m.get('support', 0)} | {_pct(m.get('accuracy', 0.0))} "
            f"| {_num(m.get('precision', 0.0))} | {_num(m.get('recall', 0.0))} "
            f"| {_num(m.get('f1', 0.0))} |"
        )
    return "\n".join([header, separator, *rows])


def _confusion_matrix_markdown(summary: dict[str, Any]) -> str:
    """Render a per-bucket confusion matrix (rows = actual, cols = predicted)."""

    matrix = summary.get("confusion_matrix")
    if not isinstance(matrix, list) or not matrix:
        return ""
    n_buckets = len(matrix)
    header = "| actual \\ predicted | " + " | ".join(
        f"b{index}" for index in range(n_buckets)
    ) + " |"
    separator = "| --- | " + " | ".join("---" for _ in range(n_buckets)) + " |"
    lines = [header, separator]
    per_class = summary.get("per_class", {})
    for actual_index in range(n_buckets):
        row = matrix[actual_index]
        support = per_class.get(str(actual_index), {}).get("support", 0)
        cells = " | ".join(str(int(value)) for value in row)
        lines.append(f"| b{actual_index} (support {support}) | {cells} |")
    return "\n".join(lines)


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
            f"- split: {result.counts['train_tool_calls_success']} train / "
            f"{result.counts['test_tool_calls']} tool calls "
            f"(per-repo observation-level, seed={result.config.seed})"
        ),
        f"- bucket edges (ms): {list(result.config.bucket_edges_ms)}",
        f"- protocol: {result.meta.get('protocol')}",
        f"- generated: {result.meta.get('generated_at_utc')}",
    ]
    sections.append("\n".join(header))

    counts = result.counts
    counts_table = _table(
        [
            ("train tasks (distinct)", str(counts["train_tasks"])),
            ("test tasks (distinct)", str(counts["test_tasks"])),
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

    bucket_summary = result.summaries[BUCKET_TRACK]
    bucket_section = (
        f"## `{BUCKET_TRACK}` (ClauseResourceKB, clause-level)\n\n"
        + _bucket_table(bucket_summary)
    )
    breakdown = _bucket_breakdown_table(bucket_summary)
    if breakdown:
        bucket_section += "\n\nPer-bucket F1 / accuracy:\n\n" + breakdown
    confusion = _confusion_matrix_markdown(bucket_summary)
    if confusion:
        bucket_section += "\n\nConfusion matrix (rows = actual, cols = predicted):\n\n" + confusion
    sections.append(bucket_section)

    for track in LATTICE_TRACKS:
        section = (
            f"## `{track}` (LatticeTimeKB, clause-level point prediction)\n\n"
            + _lattice_table(result.summaries[track])
        )
        bucketed = result.summaries.get(f"{track}_bucket")
        if bucketed:
            section += (
                "\n\nBucketed classification (predicted_ms/actual_ms into "
                "latency buckets):\n\n"
                + _bucket_table(bucketed)
            )
            breakdown = _bucket_breakdown_table(bucketed)
            if breakdown:
                section += "\n\nPer-bucket F1 / accuracy:\n\n" + breakdown
            confusion = _confusion_matrix_markdown(bucketed)
            if confusion:
                section += (
                    "\n\nConfusion matrix (rows = actual, cols = predicted):\n\n"
                    + confusion
                )
        sections.append(section)
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
    sections.append(
        f"## `{CONTINUOUS_MEMORY_TRACK}` "
        "(RuntimeToolResourceKB, call-level p90)\n\n"
        + _continuous_table(result.summaries[CONTINUOUS_MEMORY_TRACK])
        + "\n\n"
        + _resource_availability_block(result)
    )

    notes = result.meta.get("notes", {})
    note_lines = ["## Notes"]
    if isinstance(notes, dict):
        for key, value in notes.items():
            if value:
                note_lines.append(f"- **{key}**: {value}")
    sections.append("\n".join(note_lines))

    return "\n\n".join(sections) + "\n"


def _resource_availability_block(result: EvalResult) -> str:
    """Summarize which continuous resource targets the dataset supports."""

    cpu = result.summaries[CONTINUOUS_CPU_TRACK]
    latency = result.summaries[CONTINUOUS_LATENCY_TRACK]
    memory = result.summaries[CONTINUOUS_MEMORY_TRACK]
    return (
        "Resource-target availability:\n\n"
        f"- **latency**: evaluated, coverage {_pct(latency.get('coverage'))}.\n"
        f"- **peak_cpu_cores**: evaluated, coverage {_pct(cpu.get('coverage'))} "
        f"(short clauses <1s have no eligible peak-CPU, so coverage is partial).\n"
        f"- **peak_memory_mb**: evaluated as an absolute value (no ambient "
        f"anchor); this dataset has no per-call memory samples (monitoring "
        f"disabled), so coverage is {_pct(memory.get('coverage'))}."
    )


def write_markdown_report(result: EvalResult, path: str | Path) -> Path:
    """Write the rendered Markdown report to *path*."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result), encoding="utf-8")
    return path


__all__ = ["render_markdown", "write_markdown_report"]
