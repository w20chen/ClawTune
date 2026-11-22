"""Session state and next-model forecasts derived from verified observations."""
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field


def session_key(owner):
    return tuple(owner.get(k) for k in ("gateway_id", "runtime_id", "session_id"))


def _runtime_key(owner):
    return owner.get("gateway_id"), owner.get("runtime_id")


@dataclass
class Session:
    owner: dict
    observation: dict
    seen_at: float
    revision: int = 0
    state_revision: int = 0
    state: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    started_at: dict = field(default_factory=dict)
    completed_at: dict = field(default_factory=dict)
    forecast: tuple | None = None
    forecast_inputs: tuple | None = None
    target_batch_id: str | None = None
    target_settled_at: float | None = None
    event_model_call_id: str | None = None
    handoff_from_batch_id: str | None = None


class SessionStateManager:
    def __init__(self, publish, *, clock=time.time, capacity=10000):
        self.publish = publish
        self.clock = clock
        self.capacity = capacity
        self.sessions = {}
        self.latest = {}
        # Process-lifetime, strictly causal evidence for tool-return -> next-model delay.
        self.handoff_history = defaultdict(lambda: deque(maxlen=256))

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
        wait = data.get("wait")
        active_children = set(data["active_child_session_keys"])
        if wait and (data["run_status"] != "yielded" or
                     any(child not in active_children for child in wait["session_keys"])):
            raise ValueError("invalid_session_wait")
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
            changed_batch = (epoch != old_epoch or owner["run_id"] != current.owner["run_id"]
                             or (old_batch or {}).get("batch_id") != (new_batch or {}).get("batch_id"))
            if data.get("model_call_id") and old_batch and current.target_batch_id:
                self._resolve_target(current, data["model_call_id"], seen_at)
            if changed_batch:
                self._reset_batch(current, new_batch, seen_at)
            else:
                self._track_tools(current, previous, data, seen_at)
            if data["run_status"] in {"ended", "error", "cancelled", "yielded", "closed"}:
                self._withdraw_target(current)
            current.owner = deepcopy(owner)
            current.observation = deepcopy(data)
            current.seen_at = seen_at
            if data.get("model_call_id"):
                current.event_model_call_id = data["model_call_id"]
            elif new_batch or previous.get("model_call_id") is not None or data["run_status"] in {
                    "ended", "error", "cancelled", "yielded", "closed"}:
                current.event_model_call_id = None
                current.handoff_from_batch_id = None
        else:
            if len(self.sessions) >= self.capacity:
                raise ValueError("session_capacity_exceeded")
            current = Session(deepcopy(owner), deepcopy(data), seen_at)
            self.sessions[key] = current
            self._reset_batch(current, batch, seen_at)
            if data["run_status"] in {"ended", "error", "cancelled", "yielded", "closed"}:
                self._withdraw_target(current)
            current.event_model_call_id = data.get("model_call_id")
        self._update(key, current)
        return True

    def _reset_batch(self, session, batch, observed_at):
        session.progress.clear()
        session.started_at.clear()
        session.completed_at.clear()
        session.forecast = None
        session.forecast_inputs = None
        session.target_batch_id = batch["batch_id"] if batch else None
        session.target_settled_at = None
        if batch:
            for tool in batch["tools"]:
                tool_id = tool["tool_call_id"]
                if tool.get("started"):
                    session.started_at[tool_id] = observed_at
                if tool["settled"]:
                    session.completed_at[tool_id] = observed_at
            if all(tool["settled"] for tool in batch["tools"]):
                session.target_settled_at = observed_at

    def _track_tools(self, session, previous, data, observed_at):
        old_batch, batch = previous.get("batch"), data.get("batch")
        if not old_batch or not batch:
            return
        old = {tool["tool_call_id"]: tool for tool in old_batch["tools"]}
        for tool in batch["tools"]:
            tool_id = tool["tool_call_id"]
            prior = old.get(tool_id, {})
            if tool.get("started") and not prior.get("started"):
                session.started_at[tool_id] = observed_at
            if tool["settled"] and not prior.get("settled"):
                session.completed_at[tool_id] = observed_at
        if all(tool["settled"] or tool["tool_call_id"] in session.completed_at
               for tool in batch["tools"]):
            session.target_settled_at = max(session.completed_at.values(), default=observed_at)

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
        elapsed = data.get("elapsed_duration_ms")
        if tool_id not in session.started_at and isinstance(elapsed, (int, float)):
            session.started_at[tool_id] = observed_at - elapsed / 1000
        self._update(key, session)

    def process_exited(self, owner):
        # Process exit is not logical completion: invalidate live ETA until the hook returns.
        key = session_key(owner)
        session = self.sessions.get(key)
        if session and session.owner["run_id"] == owner.get("run_id"):
            session.progress.pop(owner.get("tool_call_id"), None)
            self._update(key, session)

    def tool_completed(self, owner, observed_at):
        key = session_key(owner)
        session = self.sessions.get(key)
        if session is None or session.owner["run_id"] != owner.get("run_id"):
            return
        batch = session.observation.get("batch")
        tool_id = owner.get("tool_call_id")
        if not batch or not any(tool["tool_call_id"] == tool_id for tool in batch["tools"]):
            return
        session.completed_at[tool_id] = observed_at
        if all(tool["settled"] or tool["tool_call_id"] in session.completed_at
               for tool in batch["tools"]):
            session.target_settled_at = max(session.completed_at.values())
        self._update(key, session)

    def model_started(self, owner, call_id, observed_at):
        if not call_id:
            return
        key = session_key(owner)
        session = self.sessions.get(key)
        if session is None or session.owner["run_id"] != owner.get("run_id"):
            return
        self._resolve_target(session, call_id, observed_at)
        session.event_model_call_id = call_id
        session.seen_at = max(session.seen_at, min(self.clock(), observed_at))
        self._update(key, session)

    def model_completed(self, owner, call_id, observed_at):
        key = session_key(owner)
        session = self.sessions.get(key)
        if session is None or session.owner["run_id"] != owner.get("run_id"):
            return
        if call_id and session.event_model_call_id not in {None, call_id}:
            return
        session.event_model_call_id = None
        session.handoff_from_batch_id = None
        session.seen_at = max(session.seen_at, min(self.clock(), observed_at))
        self._update(key, session)

    def _resolve_target(self, session, call_id, observed_at):
        del call_id  # The emitted state carries it; batch identity owns calibration.
        batch_id = session.target_batch_id
        if batch_id is None:
            return
        session.handoff_from_batch_id = batch_id
        if session.target_settled_at is not None and observed_at >= session.target_settled_at:
            self.handoff_history[_runtime_key(session.owner)].append(
                (observed_at - session.target_settled_at) * 1000
            )
        session.target_batch_id = None
        session.target_settled_at = None
        session.progress.clear()
        session.forecast = None
        session.forecast_inputs = None

    @staticmethod
    def _withdraw_target(session):
        session.target_batch_id = None
        session.target_settled_at = None
        session.handoff_from_batch_id = None
        session.progress.clear()
        session.forecast = None
        session.forecast_inputs = None

    def tick(self):
        for key, session in self.sessions.items():
            self._update(key, session)

    def _phase(self, session, now):
        data = session.observation
        batch = data.get("batch")
        tools = batch["tools"] if batch else []
        status = data["run_status"]
        model_call_id = session.event_model_call_id
        if now - session.seen_at > 15:
            return "UNKNOWN", "observation_lost", model_call_id, batch, tools
        if status == "yielded":
            return "WAIT_EVENT", "run_yielded", model_call_id, None, []
        if status in {"ended", "error", "cancelled", "closed"}:
            return "IDLE", "run_" + status, model_call_id, None, []
        if model_call_id is not None:
            return "LLM", "model_started", model_call_id, None, []
        if status == "finishing":
            return "CONTROL", "run_finishing", model_call_id, batch, tools
        if batch:
            if session.target_batch_id != batch["batch_id"]:
                return "CONTROL", "batch_target_resolved", model_call_id, None, []
            settled = all(tool["settled"] or tool["tool_call_id"] in session.completed_at
                          for tool in tools)
            return ("HANDOFF", "batch_settled", model_call_id, batch, tools) if settled else (
                "TOOLS", "batch_active", model_call_id, batch, tools)
        return "CONTROL", "run_active", model_call_id, batch, tools

    def _update(self, key, session):
        now = self.clock()
        phase, reason, model_call_id, batch, tools = self._phase(session, now)
        execution_mode = batch.get("execution_mode") if batch else None
        mode_source = batch.get("mode_source") if batch else None
        state = {
            "phase": phase,
            "reason": reason,
            "batch_id": None if phase == "LLM" else (batch["batch_id"] if batch else None),
            "handoff_from_batch_id": session.handoff_from_batch_id if phase == "LLM" else None,
            "parent_session_key": session.observation["parent_session_key"],
            "session_key": session.observation["session_key"],
            "active_child_session_keys": session.observation["active_child_session_keys"],
            "wait": session.observation["wait"],
            "run_status": session.observation["run_status"],
            "model_call_id": model_call_id,
            "target": "next_model_call_started",
            "execution_mode": execution_mode,
            "mode_source": mode_source,
        }
        signature = {**state, "run_id": session.owner["run_id"],
                     "observer_id": session.observation["observer_id"]}
        if signature != session.state:
            session.state = signature
            session.state_revision += 1
            session.forecast = None
            session.forecast_inputs = None
            self._emit(key, session, "session.state", None, None, now,
                       forecast_basis=None, unavailable_reason=None, prediction_source=None)
        if phase not in {"TOOLS", "HANDOFF"}:
            return
        forecast, inputs = self._forecast(session, phase, tools, now)
        if inputs == session.forecast_inputs and session.forecast is not None:
            expires = session.forecast[1]
            if expires is None or expires > now:
                return
        if forecast != session.forecast:
            session.forecast = forecast
            session.forecast_inputs = inputs
            deadline, expires, basis, unavailable, prediction_source = forecast
            remaining = max(0, (deadline - now) * 1000) if deadline is not None else None
            self._emit(key, session, "session.handoff_forecast", remaining, expires, now,
                       forecast_basis=basis, unavailable_reason=unavailable,
                       prediction_source=prediction_source)

    def _forecast(self, session, phase, tools, now):
        mode = session.state.get("execution_mode")
        history = self.handoff_history.get(_runtime_key(session.owner))
        overhead = statistics.median(history) if history else None
        tool_inputs = []
        values = []
        evidence_expiry = []
        live = False
        unavailable = None
        used_sources = set()
        for tool in tools:
            tool_id = tool["tool_call_id"]
            settled = tool["settled"] or tool_id in session.completed_at
            progress = session.progress.get(tool_id) if tool["foreground"] else None
            tool_inputs.append((tool_id, tool.get("started"), settled,
                                tool.get("predicted_duration_ms"), tool.get("prediction_source"), progress))
            if settled:
                continue
            if progress and progress[1] > now and progress[2] > now:
                values.append(max(0, (progress[2] - now) * 1000))
                evidence_expiry.append(progress[1])
                live = True
                continue
            duration = tool.get("predicted_duration_ms")
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                unavailable = "duration_prediction_unavailable"
                continue
            started_at = session.started_at.get(tool_id) if tool.get("started") else None
            if started_at is not None:
                remaining = duration - max(0, now - started_at) * 1000
                if remaining <= 0:
                    unavailable = "duration_prediction_expired"
                    continue
                evidence_expiry.append(started_at + duration / 1000)
            else:
                remaining = duration
            values.append(remaining)
            source = tool.get("prediction_source")
            if source in {"lattice", "tool"}:
                used_sources.add(source)
        inputs = (phase, session.state.get("batch_id"), mode, session.state.get("mode_source"),
                  tuple(tool_inputs), overhead)
        basis = "post_tools" if phase == "HANDOFF" else (
            "live_progress" if live else "pre_execution")
        prediction_source = (next(iter(used_sources)) if len(used_sources) == 1
                             else "mixed" if used_sources else None)
        if unavailable is not None:
            return (None, None, basis, unavailable, prediction_source), inputs
        if overhead is None:
            return (None, None, basis, "handoff_overhead_unavailable", prediction_source), inputs
        if phase == "HANDOFF":
            tool_remaining = 0
        elif mode == "parallel":
            tool_remaining = max(values, default=0)
        elif mode == "sequential":
            tool_remaining = sum(values)
        else:
            return (None, None, basis, "execution_mode_unknown", prediction_source), inputs
        base = session.target_settled_at if phase == "HANDOFF" else now
        deadline = base + (tool_remaining + overhead) / 1000
        if deadline <= now:
            return (None, None, basis, "forecast_expired", prediction_source), inputs
        expires = min([deadline, *evidence_expiry])
        return (deadline, expires, basis, None, prediction_source), inputs

    def _emit(self, key, session, kind, remaining, expires, now, *,
              forecast_basis, unavailable_reason, prediction_source):
        session.revision += 1
        payload = {k: v for k, v in session.state.items() if k not in {"run_id", "observer_id"}}
        payload.update(
            revision=session.revision,
            state_revision=session.state_revision,
            remaining_ms=remaining,
            forecast_basis=forecast_basis,
            prediction_model="lattice_then_tool",
            prediction_statistic="p50",
            prediction_source=prediction_source,
            unavailable_reason=unavailable_reason,
        )
        message = self.publish(kind, payload, owner=session.owner,
                               observed_at=now, expires_at=expires)
        self.latest[key] = message

    def snapshots(self):
        self.tick()
        return list(self.latest.values())
