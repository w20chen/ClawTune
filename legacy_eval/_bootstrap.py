"""Import-path bootstrap so the legacy evaluator can import ClawTune's
algorithm modules without modifying the repository.

The prediction algorithms live under ``services/sidecar/src`` (packages
``tool_resource`` and ``tool_time``).  The sidecar package is not installed
into site-packages, so callers must have that directory on ``sys.path``.  This
module makes the evaluator self-contained: ``ensure_paths()`` inserts the
repo-relative sidecar source directory if it is not already present.

The repo root is derived from this file's location
(``<repo>/legacy_eval/_bootstrap.py``), so the evaluator works regardless of
the process working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SIDECAR_SRC = _REPO_ROOT / "services" / "sidecar" / "src"

_INSERTED = False


def ensure_paths() -> None:
    """Idempotently add the sidecar source tree to ``sys.path``."""

    global _INSERTED
    if _INSERTED:
        return
    target = str(_SIDECAR_SRC)
    if target not in sys.path:
        sys.path.insert(0, target)
    _INSERTED = True


__all__ = ["ensure_paths", "_REPO_ROOT", "_SIDECAR_SRC"]
