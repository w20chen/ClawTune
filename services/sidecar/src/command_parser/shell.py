"""Read shell words from the existing mvdan adapter without shell execution."""
from __future__ import annotations


def clauses(command: str) -> list[dict]:
    from tool_resource.features import parse_command_clauses
    parsed = parse_command_clauses(command)
    if parsed.get("parse_failed"):
        raise ValueError("shell_parse_failed")
    return parsed["clauses"]
