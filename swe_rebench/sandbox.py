"""Shared OpenClaw Docker sandbox helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sandbox_container_prefix(scope: str | Path) -> str:
    """Return a stable Docker sandbox container prefix for a task scope."""
    digest = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:12]
    return f"clawtune-srb-{digest}-"
