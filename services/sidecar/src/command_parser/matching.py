"""Execution-identity guards for the legacy command/prefix KB indexes.

These guards restrict selection, not accumulation: old coarse samples remain
available to queries whose identity they actually retain.
"""
from __future__ import annotations

from functools import lru_cache

from .models import ParseContext
from .service import default_service


@lru_cache(maxsize=4096)
def identity_prefix_lengths(
    argv: tuple[str, ...], core: frozenset[str], repo: str,
) -> frozenset[int]:
    """Prefixes that still identify the complete known execution entry.

    Full argv is always an identity match. The original indexes only retain
    four prefix words, so longer prefixes do not need to be parsed here.
    """
    service = default_service()
    return frozenset({len(argv)} | {
        length for length in range(1, min(len(argv), 4) + 1)
        if core <= service.argv(argv[:length], ParseContext(repo=repo)).core
    })


@lru_cache(maxsize=4096)
def argv_identity(argv: tuple[str, ...], repo: str) -> frozenset[str]:
    return default_service().argv(argv, ParseContext(repo=repo)).core
