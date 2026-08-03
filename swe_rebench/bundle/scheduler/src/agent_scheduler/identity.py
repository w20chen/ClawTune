from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeAlias


OwnerKey: TypeAlias = tuple[str | None, str | None, str | None, str | None, str | None]
CorrelationKey: TypeAlias = tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str,
]


def owner_key(value: Any) -> OwnerKey:
    """Return the protocol owner chain without lossy string concatenation."""

    session_id = _string_attr(value, "session_id")
    if session_id is None:
        session_id = _string_attr(value, "session_key")
    if session_id is None:
        session_id = _string_attr(value, "session_key_hash")
    return (
        _string_attr(value, "gateway_id"),
        _string_attr(value, "runtime_id"),
        _string_attr(value, "agent_id"),
        session_id,
        _string_attr(value, "run_id"),
    )


def correlation_key(value: Any, leaf: str | None = None) -> CorrelationKey:
    owner = owner_key(value)
    event_id = _string_attr(value, "event_id") or "missing-event"
    tool_call_id = _string_attr(value, "tool_call_id")
    return (*owner, leaf or tool_call_id or event_id)


def owners_compatible(expected: Any, actual: Any) -> bool:
    """Reject every owner component that is present and disagrees.

    Optional fields stay backward compatible: a legacy peer which omits a new
    identity component is not rejected solely because a newer peer supplied it.
    """

    return all(
        left is None or right is None or left == right
        for left, right in zip(owner_key(expected), owner_key(actual), strict=True)
    )


def belongs_to_runtime(value: Any, runtime_id: str, gateway_id: str | None = None) -> bool:
    owner = owner_key(value)
    return owner[1] == runtime_id and (
        gateway_id is None or owner[0] is None or owner[0] == gateway_id
    )


def owner_prefix_matches(key: Iterable[str | None], runtime_id: str) -> bool:
    parts = tuple(key)
    return len(parts) >= 2 and parts[1] == runtime_id


def _string_attr(value: Any, name: str) -> str | None:
    item = getattr(value, name, None)
    return item if isinstance(item, str) and item else None
