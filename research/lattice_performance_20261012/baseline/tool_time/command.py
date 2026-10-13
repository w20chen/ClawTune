"""Data-driven command prefix features for tool-time policies.

Latency for command-running tools (e.g. shell ``exec``) depends on *what*
command runs, so grouping by tool name alone collapses to the base rate.
This module derives a chain of nested prefix keys from the normalized
command token stream: ``make -j12 all`` yields ``exec:make``,
``exec:make -j12``, ``exec:make -j12 all`` (up to a configurable depth).
Every observation feeds all its prefix nodes, forming a prefix tree whose
nodes carry latency samples; predictors then use the deepest node with
enough evidence and back off to shallower nodes, the tool, and the global
distribution. This separates e.g. ``make -j2`` from ``make -j12`` or
``pytest`` from ``pytest --collect-only`` when data supports it, while
unseen variants still land on their shared shallower node.

Only generic shell semantics are used for normalization - operator
splitting, env-assignment prefixes, redirection operators/targets/fd
numbers, path basenames on command heads - and the resulting nodes are
whatever the data contains; no command classes are hardcoded. Shell
comments (``# ...``) are dropped by tokenization. Command substitution
bodies (``$(date)``) contribute their inner tokens - a known
over-splitting limitation, harmless under the backoff hierarchy. A token
containing a literal space or the ``:`` delimiter could in principle
collide with another node; such a collision only merges two nodes'
histories, never breaks causality. Tokenization cannot distinguish the fd
of ``2> err.log`` from a numeric argument before ``>``, so only the
``2>&1`` dup idiom drops its fd digit; a bare ``2>`` leaves a spurious
``2`` token in deep nodes (arg-vs-fd ambiguity, granularity-only).

Commands that cannot be tokenized (e.g. unbalanced quotes emitted by the
model) yield no keys and group at the tool level - the designed fallback
of the prefix -> tool -> global hierarchy, not an error, because malformed
commands are expected input in agent traces.
"""

from __future__ import annotations

import functools
import re
import shlex
from typing import Any, Callable

# Tokens made purely of these characters separate commands (&&, ||, ;, |, &,
# subshell parens); redirection operators (>, >>, <, >&) and their target /
# fd-number words are dropped from the normalized stream.
_HEAD_SEPARATOR_CHARS = frozenset(";|&()")
_REDIRECTION_CHARS = frozenset("<>&")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Separators whose sides execute one after another, so their times add.
# Pipes (|) and background (&) run concurrently and stay within one unit;
# `||` alternatives rarely both run - counting them as sequential
# over-approximates total time, documented.
_SEQUENTIAL_SEPARATOR_TOKENS = frozenset({"&&", ";", ";;", "||"})
_GROUPING_TOKENS = frozenset({"(", ")"})


def shell_command_heads(command: str) -> list[str]:
    """Command heads (first word of each pipeline/list segment) of a shell command.

    Heads are path-basenamed so ``/usr/bin/python`` and ``python`` group
    together. Untokenizable commands return an empty list.
    """

    return [token for token, is_head in _normalized_tokens(command) if is_head]


def shell_command_prefix_tokens(
    command: str,
    *,
    skip_leading_cd: bool = False,
) -> list[str]:
    """Normalized token stream of a shell command for prefix-tree keys.

    Heads are path-basenamed; env assignments, redirection operators,
    redirection targets, and fd numbers are dropped; command separators
    (``&&``, ``|``, ...) are kept as structural tokens. Untokenizable
    commands return an empty list.

    ``skip_leading_cd`` drops leading ``cd <dir> <sep>`` segments
    (repeatedly) so the prefix depth budget indexes the actual workload
    instead of the working directory. ``cd`` is a generic POSIX builtin with
    negligible cost that never defines the workload - the same spirit as the
    env-assignment and redirection stripping, not a command class. Commands
    consisting only of ``cd`` segments are kept unchanged.
    """

    tokens = _normalized_tokens(command)
    if skip_leading_cd:
        tokens = _skip_leading_cd_segments(tokens)
    return [token for token, _ in tokens]


def command_prefix_keys(
    tool_name: str,
    command: str,
    *,
    max_depth: int,
    skip_leading_cd: bool = False,
) -> tuple[str, ...]:
    """Nested prefix-node keys for one command, ordered general -> specific."""

    if max_depth < 1:
        raise ValueError(f"max_depth must be >= 1, got {max_depth}")
    tokens = shell_command_prefix_tokens(
        command,
        skip_leading_cd=skip_leading_cd,
    )
    if not tokens:
        return ()
    depth = min(len(tokens), max_depth)
    return tuple(
        f"{tool_name}:{' '.join(tokens[:length])}" for length in range(1, depth + 1)
    )


def make_row_command_prefix_keys(
    command_field: str,
    *,
    max_depth: int,
    skip_leading_cd: bool = False,
) -> Callable[[dict[str, Any]], tuple[str, ...]]:
    """Row-level prefix-key function reading the command from ``tool_args``.

    Rows without a parseable ``tool_args`` dict or without a string command
    under ``command_field`` yield no keys (tool-level grouping).
    """

    if max_depth < 1:
        raise ValueError(f"max_depth must be >= 1, got {max_depth}")

    def row_keys(row: dict[str, Any]) -> tuple[str, ...]:
        tool_args = row.get("tool_args")
        if not isinstance(tool_args, dict):
            return ()
        command = tool_args.get(command_field)
        if not isinstance(command, str) or not command.strip():
            return ()
        tool_name = row.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return ()
        return command_prefix_keys(
            tool_name,
            command,
            max_depth=max_depth,
            skip_leading_cd=skip_leading_cd,
        )

    return row_keys


def shell_command_segments(command: str) -> list[list[str]]:
    """Sequential execution units of a shell command, as token lists.

    Units are split at sequential separators (``&&``, ``;``, ``||``) whose
    sides run one after another; pipeline (``|``) and background (``&``)
    parts run concurrently and stay within one unit. Grouping parens are
    dropped. Untokenizable commands return an empty list. Escaped
    separators (e.g. find's ``\\;`` exec terminator) are indistinguishable
    from real ones after POSIX unescaping and over-split such commands -
    a rare, granularity-only ambiguity.
    """

    segments: list[list[str]] = []
    current: list[str] = []
    for token in shell_command_prefix_tokens(command):
        if token in _SEQUENTIAL_SEPARATOR_TOKENS:
            if current:
                segments.append(current)
                current = []
        elif token in _GROUPING_TOKENS:
            continue
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _skip_leading_cd_segments(
    tokens: tuple[tuple[str, bool], ...],
) -> tuple[tuple[str, bool], ...]:
    remaining = tokens
    while remaining and remaining[0][1] and remaining[0][0] == "cd":
        separator_index = next(
            (
                index
                for index, (token, _) in enumerate(remaining)
                if token and all(char in _HEAD_SEPARATOR_CHARS for char in token)
            ),
            None,
        )
        if separator_index is None:
            return tokens  # command is only `cd ...`: keep it unchanged
        remaining = remaining[separator_index + 1 :]
    return remaining if remaining else tokens


# Keep a few thousand-command batch hot without retaining unbounded trace text.
@functools.lru_cache(maxsize=4096)
def _normalized_tokens(command: str) -> tuple[tuple[str, bool], ...]:
    """Cached normalized ``(token, is_head)`` stream of a shell command."""

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ()
    result: list[tuple[str, bool]] = []
    expect_head = True
    skip_redirection_target = False
    for index, token in enumerate(tokens):
        if not token:
            continue
        if all(char in _HEAD_SEPARATOR_CHARS for char in token):
            expect_head = True
            skip_redirection_target = False
            result.append((token, False))
            continue
        if _is_redirection_operator(token):
            skip_redirection_target = True
            continue
        if skip_redirection_target:
            skip_redirection_target = False
            continue
        if (
            token.isdigit()
            and index + 1 < len(tokens)
            and _is_redirection_operator(tokens[index + 1])
            and "&" in tokens[index + 1]
        ):
            continue  # fd number of a dup redirection like 2>&1
        if expect_head:
            if _ENV_ASSIGNMENT.match(token):
                continue
            head = token.rsplit("/", 1)[-1]
            if head:
                result.append((head, True))
                expect_head = False
            continue
        result.append((token, False))
    return tuple(result)


def _is_redirection_operator(token: str) -> bool:
    return (
        bool(token)
        and all(char in _REDIRECTION_CHARS for char in token)
        and any(char in "<>" for char in token)
    )


__all__ = [
    "command_prefix_keys",
    "make_row_command_prefix_keys",
    "shell_command_heads",
    "shell_command_prefix_tokens",
    "shell_command_segments",
]
