"""Small, dependency-free producers for contracts/signal.schema.json."""
from __future__ import annotations

import time
import uuid
from datetime import datetime

VERSION = "clawtune.signal.v1"
IDENTITY_FIELDS = ("gateway_id", "runtime_id", "run_id", "session_id", "agent_id",
                   "tool_call_id", "execution_id", "call_id", "prediction_id")


def event_timestamp(value):
    # Python 3.10's fromisoformat does not understand the Z suffix.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("event timestamp must include a timezone")
    return parsed.timestamp()


def envelope(kind, payload, *, source, identity=None, signal_id=None, observed_at=None,
             sequence=0, publisher_id=None, expires_at=None):
    return {
        "schema_version": VERSION, "signal_id": signal_id or str(uuid.uuid4()),
        "type": kind, "source": source,
        "identity": {key: (identity or {}).get(key) for key in IDENTITY_FIELDS},
        "observed_at": time.time() if observed_at is None else observed_at,
        "published_at": None, "publisher_id": publisher_id, "sequence": sequence,
        "expires_at": expires_at, "payload": payload,
    }


def event_signal(event):
    """Wrap an existing typed tool/model observation without changing its labels."""
    if hasattr(event, "model_dump"):
        event = event.model_dump(mode="json")
    kind = {"model_call_started": "model.started", "model_call_ended": "model.completed"}.get(
        event.get("event_type"), "tool.completed")
    observed = event_timestamp(event["occurred_at"])
    return envelope(kind, event, source="plugin", identity={**event, "prediction_id": event.get("decision_id")},
                    signal_id=event["event_id"], observed_at=observed)
