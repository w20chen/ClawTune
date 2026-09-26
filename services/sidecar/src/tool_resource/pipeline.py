"""Conservative input-dependency evidence, shared by queries and observations."""
from __future__ import annotations

import posixpath
import re
import shlex


def simple_sed_print(argv) -> bool:
    """Only a literal address print, without file operands or script options."""
    return bool(
        isinstance(argv, (list, tuple)) and len(argv) == 3
        and all(isinstance(arg, str) for arg in argv)
        and posixpath.basename(argv[0]) == "sed" and argv[1] == "-n"
        and re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)?p", argv[2])
    )


def pipe_stdin_from_syntax(clause) -> bool | None:
    """Prove the simple command has no redirections, assignments or grouping.

    `original` and pipe position must come from the successful shell AST.
    Unknown syntax stays unknown, including input redirection and heredocs.
    """
    if not simple_sed_print(clause.get("argv")):
        return None
    if not clause.get("in_pipe") or clause.get("pipeline_position", -1) <= 0:
        return None
    context = clause.get("structural_context")
    if context is None or any(not item.startswith("binary:") for item in context):
        return None
    try:
        words = shlex.split(clause["original"], posix=True)
    except (KeyError, ValueError):
        return None
    return True if words == list(clause["argv"]) else None
