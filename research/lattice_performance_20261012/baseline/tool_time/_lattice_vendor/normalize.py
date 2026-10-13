"""Text API for the sole schema-driven command representation.

KB internals receive precomputed features; this boundary serves CLI/offline callers.
"""
from __future__ import annotations
import shlex
from functools import lru_cache
from command_parser import ParseContext, default_service
from command_parser.models import FeatureSet


@lru_cache(maxsize=4096)
def split_all_clauses(cmd: str) -> tuple[str, ...]:
    from command_parser.shell import clauses
    return tuple(c.get("original") or shlex.join(c["argv"]) for c in clauses(cmd))


def extract_primary_segment(cmd: str) -> str:
    from command_parser.shell import clauses
    entries = [c for c in clauses(cmd) if c["bin"] not in {"cd", "export"}]
    if not entries:
        raise ValueError("command has no executable clause")
    return entries[0].get("original") or shlex.join(entries[0]["argv"])


@lru_cache(maxsize=8192)
def normalize_command(cmd: str, *, repo: str | None = None, cwd: str | None = None,
                      env_id: str | None = None, max_features: int = 20,
                      is_clause: bool = False) -> tuple[FeatureSet, FeatureSet]:
    context = ParseContext(repo=repo, cwd=cwd, env_id=env_id)
    if is_clause:
        result = default_service().argv(shlex.split(cmd), context, max_features=max_features)
    else:
        from command_parser.shell import clauses
        rows = [c for c in clauses(cmd) if c["bin"] not in {"cd", "export"}]
        if not rows:
            raise ValueError("command has no executable clause")
        row = rows[0]
        result = default_service().argv(row["argv"], context, word_intents=row.get("word_intents"), max_features=max_features)
    return result.features, result.core
