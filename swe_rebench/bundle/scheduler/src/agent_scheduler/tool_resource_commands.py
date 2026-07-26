from __future__ import annotations

import json
from typing import Any

from tool_resource.features import parse_command_clauses


def extract_command(raw_params: Any) -> str | None:
    if isinstance(raw_params, str):
        try:
            parsed = json.loads(raw_params)
        except (json.JSONDecodeError, TypeError):
            return None
        return extract_command(parsed)
    if not isinstance(raw_params, dict):
        return None
    value = raw_params.get("command") or raw_params.get("cmd")
    if isinstance(value, str) and value:
        return value
    nested = raw_params.get("exec")
    if isinstance(nested, dict):
        nested_value = nested.get("command") or nested.get("cmd")
        if isinstance(nested_value, str) and nested_value:
            return nested_value
    return None


def operation_from_tool_request(tool_name: str, raw_params: Any) -> str | None:
    if tool_name != "exec":
        return tool_name or None
    command = extract_command(raw_params)
    if command is None:
        return None
    try:
        parsed = parse_command_clauses(command)
    except Exception:
        return None
    clauses = parsed.get("clauses")
    if not isinstance(clauses, list) or len(clauses) != 1:
        return None
    clause = clauses[0]
    if not isinstance(clause, dict):
        return None
    bin_ = clause.get("bin")
    return bin_ if isinstance(bin_, str) and bin_ else None


def operation_from_request(request: Any) -> str | None:
    return operation_from_tool_request(
        getattr(request, "tool_name", "unknown"),
        getattr(request, "raw_params", None),
    )
