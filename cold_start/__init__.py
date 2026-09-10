"""Versioned task-held-out cold-start seed construction from read-only traces."""
from pathlib import Path
import sys

_source = str(Path(__file__).resolve().parents[1] / "services/sidecar/src")
if _source not in sys.path:
    sys.path.insert(0, _source)
