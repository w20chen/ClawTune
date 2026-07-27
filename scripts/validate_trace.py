#!/usr/bin/env python3
"""Trace v6 validator.

Reads a JSONL trace file and reports validation diagnostics.

Usage:
    python scripts/validate_trace.py trace.jsonl
    python scripts/validate_trace.py -q trace.jsonl   # quiet mode
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def validate_trace(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "records": 0,
        "metadata_records": 0,
        "span_starts": 0,
        "span_ends": 0,
        "complete_spans": 0,
        "incomplete_spans": 0,
        "unresolved_parents": 0,
        "duplicate_ends": 0,
        "ends_without_starts": 0,
        "invalid_coverage_ratios": 0,
        "possible_secret_leaks": 0,
        "duration_mismatches": 0,
        "inconsistent_span_identity": 0,
        "errors": [],
    }

    parsed: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            parsed.append(obj)
        except json.JSONDecodeError:
            result["errors"].append(f"line {i+1}: invalid JSON")

    result["records"] = len(parsed)

    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, list[dict[str, Any]]] = {}
    ended_span_ids: set[str] = set()

    for record in parsed:
        if not isinstance(record, dict):
            continue

        rt = record.get("record_type")
        sv = record.get("schema_version")

        if rt is not None and sv != 6:
            result["errors"].append(f"record has schema_version={sv}, expected 6")

        if _contains_possible_secret(record):
            result["possible_secret_leaks"] += 1

        if rt == "trace_metadata":
            result["metadata_records"] += 1
            continue

        if rt == "span_start":
            result["span_starts"] += 1
            sid = record.get("span_id")
            if isinstance(sid, str) and sid:
                starts[sid] = record
            continue

        if rt == "span_end":
            result["span_ends"] += 1
            sid = record.get("span_id")
            if isinstance(sid, str) and sid:
                existing = ends.get(sid, [])
                if existing:
                    result["duplicate_ends"] += 1
                existing.append(record)
                ends[sid] = existing
                ended_span_ids.add(sid)

            if record.get("kind") == "tool" and record.get("parent_span_id") is None:
                result["unresolved_parents"] += 1

            # Validate coverage ratio
            cr = record.get("resources", {}).get("coverage_ratio")
            if cr is not None and isinstance(cr, (int, float)):
                if cr < 0 or cr > 1:
                    result["invalid_coverage_ratios"] += 1
                    result["errors"].append(f"span {sid}: coverage_ratio={cr} out of [0,1]")

            # Validate duration_ns non-negative
            dur_ns_str = record.get("duration_ns")
            if dur_ns_str is not None:
                try:
                    dur_ns = int(dur_ns_str)
                    if dur_ns < 0:
                        result["errors"].append(f"span {sid}: negative duration_ns")
                except (ValueError, TypeError):
                    result["errors"].append(f"span {sid}: invalid duration_ns")

            # Validate duration_sec non-negative and consistent with duration_ns
            dur_sec_str = record.get("duration_sec")
            if dur_sec_str is not None:
                try:
                    dur_sec = float(dur_sec_str)
                    if dur_sec < 0:
                        result["errors"].append(f"span {sid}: negative duration_sec")
                except (ValueError, TypeError):
                    result["errors"].append(f"span {sid}: invalid duration_sec")

                # Cross-validate consistency with duration_ns if both present
                if dur_ns_str is not None:
                    try:
                        dur_ns = int(dur_ns_str)
                        expected_sec = dur_ns / 1e9
                        if abs(dur_sec - expected_sec) > max(1e-9, expected_sec * 1e-6):
                            result["errors"].append(
                                f"span {sid}: duration_sec ({dur_sec}) inconsistent "
                                f"with duration_ns ({dur_ns} -> {expected_sec})"
                            )
                    except (ValueError, TypeError):
                        pass  # already reported above

    # Check completeness
    for sid in starts:
        if sid in ended_span_ids:
            result["complete_spans"] += 1
        else:
            result["incomplete_spans"] += 1

    # Check ends without starts
    for sid in ends:
        if sid not in starts:
            result["ends_without_starts"] += 1

    # Check span identity consistency
    for sid, end_list in ends.items():
        start = starts.get(sid)
        if start is None:
            continue
        for end in end_list:
            if not _spans_match(start, end):
                result["inconsistent_span_identity"] += 1
                result["errors"].append(f"span {sid}: start and end have inconsistent identity fields")

    return result


def _contains_possible_secret(obj: Any, depth: int = 0) -> bool:
    """Heuristic check for plaintext secrets in trace data."""
    if depth > 10:
        return False
    if isinstance(obj, str):
        if "Bearer " in obj and "<redacted>" not in obj:
            # Check if it's a long token-like value after Bearer
            import re
            if re.search(r"Bearer\s+[A-Za-z0-9+/=._-]{8,}", obj):
                return True
        if "--token" in obj.lower() and "<redacted>" not in obj:
            import re
            if re.search(r"--token[= ]\s*[A-Za-z0-9+/=._-]{4,}", obj):
                return True
    elif isinstance(obj, list):
        return any(_contains_possible_secret(item, depth + 1) for item in obj)
    elif isinstance(obj, dict):
        return any(_contains_possible_secret(v, depth + 1) for v in obj.values())
    return False


def _spans_match(start: dict[str, Any], end: dict[str, Any]) -> bool:
    return (
        start.get("trace_id") == end.get("trace_id")
        and start.get("span_id") == end.get("span_id")
        and start.get("parent_span_id") == end.get("parent_span_id")
        and start.get("kind") == end.get("kind")
        and start.get("sequence_no") == end.get("sequence_no")
    )


def format_summary(result: dict[str, Any]) -> str:
    lines = [
        "Trace validation summary",
        "",
        f"records: {result['records']}",
        f"spans (from starts): {result['span_starts']}",
        f"complete spans: {result['complete_spans']}",
        f"incomplete spans: {result['incomplete_spans']}",
        f"unresolved parents: {result['unresolved_parents']}",
        f"duplicate ends: {result['duplicate_ends']}",
        f"ends without starts: {result['ends_without_starts']}",
        f"invalid coverage ratios: {result['invalid_coverage_ratios']}",
        f"possible secret leaks: {result['possible_secret_leaks']}",
        f"duration mismatches: {result['duration_mismatches']}",
        f"inconsistent span identity: {result['inconsistent_span_identity']}",
    ]

    if result["errors"]:
        lines.append("")
        lines.append("Errors:")
        for err in result["errors"]:
            lines.append(f"  - {err}")

    lines.append("")

    has_errors = (
        result["invalid_coverage_ratios"] > 0
        or result["possible_secret_leaks"] > 0
        or result["duplicate_ends"] > 0
        or result["duration_mismatches"] > 0
        or len(result["errors"]) > 0
    )
    has_warnings = (
        result["incomplete_spans"] > 0
        or result["unresolved_parents"] > 0
        or result["ends_without_starts"] > 0
        or result["inconsistent_span_identity"] > 0
    )

    if has_errors:
        lines.append("INVALID")
    elif has_warnings:
        lines.append("VALID WITH WARNINGS")
    else:
        lines.append("VALID")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate trace v6 JSONL files")
    parser.add_argument("trace_file", type=str, help="Path to trace JSONL file")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only print summary")
    args = parser.parse_args()

    path = Path(args.trace_file)
    if not path.exists():
        print(f"File not found: {args.trace_file}", file=sys.stderr)
        sys.exit(1)

    lines = path.read_text(encoding="utf-8").splitlines()
    result = validate_trace(lines)
    print(format_summary(result))

    if result["errors"] and not args.quiet:
        print("\nValidation completed with issues.")

    # Exit non-zero if there are hard errors
    has_errors = (
        result["invalid_coverage_ratios"] > 0
        or result["possible_secret_leaks"] > 0
        or result["duplicate_ends"] > 0
        or len(result["errors"]) > 0
    )
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
