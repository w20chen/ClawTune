"""Session scheduling hints derived from complete plugin snapshots and fresh progress.

No duration model or scheduling policy lives here. A snapshot is replaceable;
progress cannot complete a logical tool or revive a previous batch.
"""
from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field


def session_key(owner):
    return tuple(owner.get(k) for k in ("gateway_id", "runtime_id", "session_id"))


@dataclass
class Session:
    owner: dict
    observation: dict
    seen_at: float
    revision: int = 0
    state_revision: int = 0
    state: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    forecast: tuple | None = None


class SessionStateManager:
    def __init__(self, publish, *, clock=time.time, capacity=10000):
        self.publish = publish
        self.clock = clock
        self.capacity = capacity
        self.sessions = {}
        self.latest = {}

    def observe(self, message):
        owner, data = message["identity"], message["payload"]
        key = session_key(owner)
        now = self.clock()
        if now - message["observed_at"] > 15:
            return False  # A queued old heartbeat must not restore live state.
        seen_at = min(now, message["observed_at"])
        batch = data.get("batch")
        if batch and len({t["tool_call_id"] for t in batch["tools"]}) != len(batch["tools"]):
            raise ValueError("duplicate_batch_member")
        current = self.sessions.get(key)
        if current is not None:
            previous = current.observation
            old_epoch = (previous["observer_started_at"], previous["observer_id"])
            epoch = (data["observer_started_at"], data["observer_id"])
            if epoch < old_epoch or (epoch == old_epoch and data["revision"] < previous["revision"]):
                return False
            if epoch == old_epoch and data["revision"] == previous["revision"]:
                if data != previous or owner != current.owner:
                    raise ValueError("session_revision_conflict")
                current.seen_at = max(current.seen_at, seen_at)
                self._update(key, current)
                return False
            old_batch = previous.get("batch")
            new_batch = data.get("batch")
            if (epoch != old_epoch or owner["run_id"] != current.owner["run_id"]
                    or (old_batch or {}).get("batch_id") != (new_batch or {}).get("batch_id")):
                current.progress.clear()
            current.owner = deepcopy(owner)
            current.observation = deepcopy(data)
            current.seen_at = seen_at
        else:
            if len(self.sessions) >= self.capacity:
                raise ValueError("session_capacity_exceeded")
            current = Session(deepcopy(owner), deepcopy(data), seen_at)
            self.sessions[key] = current
        self._update(key, current)
        return True

    def batch_key(self, owner):
        session = self.sessions.get(session_key(owner))
        if session is None or session.owner["run_id"] != owner.get("run_id"):
            return None
        data = session.observation
        batch = data.get("batch")
        if batch and any(t["tool_call_id"] == owner.get("tool_call_id")
                         and not t["settled"] for t in batch["tools"]):
            return data["observer_id"], batch["batch_id"]
        return None

    def progress(self, owner, data, observed_at, expires_at):
        key = session_key(owner)
        session = self.sessions.get(key)
        if session is None or session.owner["run_id"] != owner.get("run_id"):
            return
        batch = session.observation.get("batch")
        tool_id = owner.get("tool_call_id")
        if not batch or not any(t["tool_call_id"] == tool_id and not t["settled"]
                                and t["foreground"] for t in batch["tools"]):
            return
        previous = session.progress.get(tool_id)
        if previous and observed_at <= previous[0]:
            return
        session.progress[tool_id] = (observed_at, expires_at,
                                     observed_at + data["remaining_duration_ms"] / 1000)
        self._update(key, session)

    def process_exited(self, owner):
        # Process exit is not logical completion: invalidate ETA until the hook returns.
        key = session_key(owner)
        session = self.sessions.get(key)
        if session and session.owner["run_id"] == owner.get("run_id"):
            session.progress.pop(owner.get("tool_call_id"), None)
            self._update(key, session)

    def tick(self):
        for key, session in self.sessions.items():
            self._update(key, session)

    def _update(self, key, session):
        now, data = self.clock(), session.observation
        batch = data.get("batch")
        tools = batch["tools"] if batch else []
        status = data["run_status"]
        if now - session.seen_at > 15:
            phase, reason = "UNKNOWN", "observation_lost"
        elif status == "yielded":
            phase, reason = "WAIT_EVENT", "run_yielded"
        elif status in {"ended", "error", "cancelled", "closed"}:
            phase, reason = "IDLE", "run_" + status
        elif data["model_call_id"] is not None:
            phase, reason = "LLM", "model_started"
        elif status == "finishing":
            phase, reason = "CONTROL", "run_finishing"
        elif batch:
            phase = "HANDOFF" if all(t["settled"] for t in tools) else "TOOLS"
            reason = "batch_settled" if phase == "HANDOFF" else "batch_active"
        else:
            phase, reason = "CONTROL", "run_active"
        state = {"phase": phase, "reason": reason,
                 "batch_id": batch["batch_id"] if batch else None,
                 "parent_session_key": data["parent_session_key"],
                 "session_key": data["session_key"],
                 "run_status": status, "model_call_id": data["model_call_id"]}
        # Run identity must also trigger a new state, even if the phase is unchanged.
        signature = {**state, "run_id": session.owner["run_id"],
                     "observer_id": data["observer_id"]}
        if signature != session.state:
            session.state = signature
            session.state_revision += 1
            session.forecast = None
            self._emit(key, session, "session.state", None, None, now)
        forecast = None
        if phase == "TOOLS":
            outstanding = [t for t in tools if not t["settled"]]
            evidence = [session.progress.get(t["tool_call_id"]) if t["foreground"] else None
                        for t in outstanding]
            # No pending work is estimated. When every outstanding member has
            # fresh process progress, they are observably executing concurrently.
            if evidence and all(e and e[1] > now and e[2] > now for e in evidence):
                forecast = (max(e[2] for e in evidence), min(e[1] for e in evidence))
        if forecast != session.forecast:
            session.forecast = forecast
            self._emit(key, session, "session.handoff_forecast",
                       max(0, (forecast[0] - now) * 1000) if forecast else None,
                       forecast[1] if forecast else None, now)

    def _emit(self, key, session, kind, remaining, expires, now):
        session.revision += 1
        payload = {k: v for k, v in session.state.items() if k not in {"run_id", "observer_id"}}
        payload.update(revision=session.revision, state_revision=session.state_revision,
                       remaining_ms=remaining)
        message = self.publish(kind, payload, owner=session.owner,
                               observed_at=now, expires_at=expires)
        # Every message is a full state snapshot; a forecast may safely overtake
        # a state event. Revision is shared by both types within this session.
        self.latest[key] = message

    def snapshots(self):
        self.tick()
        return list(self.latest.values())
