"""Independent offline evaluation of ClawTune's tool-resource prediction
algorithms over external "legacy" trace datasets.

This package NEVER modifies the vendored algorithm code under
``services/sidecar/src/tool_resource`` and ``services/sidecar/src/tool_time``.
It only:

* parses the external (legacy) dataset format into the algorithm input objects
  (``ClauseObservation`` / ``CompletedCall``),
* randomly splits tasks into a training set (default 80%) and a test set
  (default 20%),
* trains the prediction KBs on the training split only,
* replays the test split and records a prediction before every tool call
  (pure static protocol: test observations are never fed back into the KB),
* reports per-algorithm metrics.

Legacy format
-------------
Each task directory ``<org>__<repo>-<pr>`` contains an ``attempt_1/`` folder
with a eBPF ``clause_telemetry.json`` artifact and an action-level
``trace.jsonl``.  The clause artifacts are structurally identical to
ClawTune's own eBPF clause telemetry and pass the native
``tool_resource.sdk._load_valid_artifact`` validation unchanged; only the
surrounding trace layout differs, which this package adapts.
"""

from __future__ import annotations

from legacy_eval._bootstrap import ensure_paths

ensure_paths()

__all__ = [
    "evaluate",
    "load_all",
    "split_tasks",
]

# Public convenience re-exports (heavy modules import lazily via engine).
from legacy_eval.engine import EvalConfig, EvalResult, evaluate  # noqa: E402
from legacy_eval.loader import TaskArtifacts, load_all  # noqa: E402
from legacy_eval.split import split_tasks  # noqa: E402
