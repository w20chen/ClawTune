"""One sidecar-owned signal stream and bounded, best-effort HTTP delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass

import httpx

from clawtune_sidecar.session_state import SessionStateManager, session_key
from clawtune_sidecar.signal_protocol import IDENTITY_FIELDS, envelope, event_timestamp

logger = logging.getLogger(__name__)


def identity(value):
    result = {key: getattr(value, key, None) for key in IDENTITY_FIELDS}
    result["prediction_id"] = result["prediction_id"] or getattr(value, "decision_id", None)
    return result


def call_key(value):
    # A tool ID is scoped by its owner, never globally unique.
    return tuple(value.get(k) for k in ("gateway_id", "runtime_id", "run_id", "session_id", "agent_id",
                                      "tool_call_id", "call_id"))


@dataclass
class ExecutionSignals:
    identity: dict
    baseline_id: str | None = None
    baseline_ms: float | None = None
    baseline_source: str | None = None
    started: bool = False
    exited: bool = False
    exit_sent: bool = False
    last_elapsed_ms: float = -1
    last_observed_at: float = -1
    finishing_sent: bool = False
    update_sent: bool = False
    session_batch: tuple | None = None


class SignalHub:
    """All mutations run on the sidecar event loop; network I/O is independent.

    Sequence orders publication within a publisher lifetime, not wall-clock
    observations. No cross-host monotonic clock subtraction is performed.
    """

    def __init__(self, endpoint=None, token=None, *, queue_size=1024, timeout=1.0,
                 client=None, tool_events=True):
        self.endpoint, self.token, self.timeout = endpoint, token, timeout
        self.tool_events = tool_events
        self.publisher_id = str(uuid.uuid4())
        self.sequence = 0
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.task = None
        self.client = client
        self.closed = False
        self.counts = Counter()
        self.recent = deque(maxlen=256)
        self.baselines = OrderedDict()
        self.executions = OrderedDict()
        self.receipts = OrderedDict()
        self.sessions = SessionStateManager(self.publish)
        self.session_pending = OrderedDict()
        self.session_task = None

    def start_sessions(self):
        if self.session_task is None:
            self.session_task = asyncio.create_task(self._deliver_sessions())

    def publish(self, kind, payload, *, source="sidecar", owner=None, observed_at=None,
                expires_at=None, signal_id=None):
        self.sequence += 1
        message = envelope(kind, payload, source=source, identity=owner,
                           signal_id=signal_id, observed_at=observed_at,
                           sequence=self.sequence, publisher_id=self.publisher_id,
                           expires_at=expires_at)
        message["published_at"] = time.time()
        self.recent.append(message)
        self.counts["published"] += 1
        if kind.startswith("session."):
            if self.endpoint and not self.closed:
                self.session_pending[session_key(message["identity"])] = message
            return message
        if self.endpoint and not self.closed and self.tool_events:
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                self.counts["queue_full"] += 1
                logger.warning("signal queue full: %s", kind)
            else:
                if self.task is None:
                    self.task = asyncio.create_task(self._deliver())
        return message

    def prediction(self, request, prediction, decision_id, *, observed_at=None):
        owner = identity(request)
        owner["prediction_id"] = decision_id
        message = self.publish("prediction.created", {
            "prediction_id": decision_id, "prediction": prediction.model_dump(mode="json"),
            "scope": "tool_call", "phase": "before_execution",
        }, owner=owner, observed_at=observed_at)
        baseline = None
        baseline_source = None
        for source, load in (
            ("lattice", prediction.lattice.load),
            ("tool", prediction.tool.load),
        ):
            target = load.targets.get("duration_ms")
            if (target and target.status == "available"
                    and target.backend == ("lattice" if source == "lattice" else "runtime")
                    and target.unit == "ms" and target.p50 is not None
                    and math.isfinite(target.p50) and target.p50 >= 0):
                baseline = target.p50
                baseline_source = source
                break
        self.baselines[decision_id] = (owner, baseline, baseline_source)
        self._bound(self.baselines)
        return message

    def register(self, request):
        if request.execution_id in self.executions:
            return
        owner = identity(request)
        baseline_id = request.prediction_id
        _, baseline, baseline_source = self.baselines.get(baseline_id, (None, None, None))
        self.validate_registration(request)
        self.executions[request.execution_id] = ExecutionSignals(
            owner, baseline_id, baseline, baseline_source,
            session_batch=self.sessions.batch_key(owner))
        # Remove only terminated entries. Active execution state must not vanish.
        if len(self.executions) > 10000:
            for key in list(self.executions):
                if self.executions[key].exited:
                    del self.executions[key]
                    if len(self.executions) <= 10000:
                        break

    def validate_registration(self, request):
        owner, *_ = self.baselines.get(request.prediction_id, (None, None, None))
        if owner is not None and call_key(owner) != call_key(identity(request)):
            raise ValueError("prediction_execution_owner_mismatch")

    def duplicate(self, message):
        key = (message["source"], call_key(message["identity"]),
               message["identity"].get("execution_id"), message["signal_id"])
        digest = hashlib.sha256(json.dumps(message, sort_keys=True, allow_nan=False).encode()).hexdigest()
        previous = self.receipts.get(key)
        if previous is not None and previous != digest:
            raise ValueError("signal_id_payload_mismatch")
        return key, digest, previous is not None

    def acknowledge(self, key, digest):
        self.receipts[key] = digest
        self._bound(self.receipts)

    @staticmethod
    def _bound(mapping):
        while len(mapping) > 10000:
            mapping.popitem(last=False)

    def observe(self, message):
        state = self.executions.get(message["identity"]["execution_id"])
        if state is None:
            self.counts["inactive_observation"] += 1
            return False
        data, kind, observed = message["payload"], message["type"], message["observed_at"]
        if kind == "execution.started":
            # A terminal execution cannot be restarted by a delayed observer
            # request.  Keep this fence ahead of the started idempotency check
            # so completion-before-start is terminal as well.
            if state.exited or state.started:
                return False
            state.started = True
            self.publish(kind, data, source="launcher", owner=state.identity, observed_at=observed)
            return True
        if state.exited:
            self.counts["inactive_observation"] += 1
            return False
        now = time.time()
        if (not state.started or observed <= state.last_observed_at
                or observed > now + .25 or now - observed > 2
                or message["expires_at"] is None or message["expires_at"] <= now):
            self.counts["stale_observation"] += 1
            return False
        elapsed, remaining = data["elapsed_duration_ms"], data["remaining_duration_ms"]
        if not math.isfinite(elapsed + remaining):
            self.counts["invalid_observation"] += 1
            return False
        if elapsed <= state.last_elapsed_ms:
            self.counts["stale_observation"] += 1
            return False
        state.last_elapsed_ms, state.last_observed_at = elapsed, observed
        expires = min(message["expires_at"], observed + min(2, remaining / 1000))
        if expires <= now:
            self.counts["stale_observation"] += 1
            return False
        if state.session_batch is not None and state.session_batch == self.sessions.batch_key(state.identity):
            self.sessions.progress(state.identity, data, observed, expires)
        estimate = {**data, "scope": "tool_call", "statistic": "point_estimate",
                    "predicted_total_duration_ms": elapsed + remaining}
        if (state.baseline_ms is not None and not state.update_sent
                and elapsed > state.baseline_ms + 1000):
            state.update_sent = True
            self.publish("prediction.updated", {
                **estimate, "baseline_prediction_id": state.baseline_id,
                "baseline_source": state.baseline_source, "baseline_statistic": "p50",
                "baseline_prediction_ms": state.baseline_ms, "overrun_threshold_ms": 1000,
            }, owner=state.identity, observed_at=observed, expires_at=expires)
        # Remaining-time evidence is useful even without a historical prediction.
        if remaining <= 2000 and not state.finishing_sent:
            state.finishing_sent = True
            self.publish("execution.finishing_soon", estimate, owner=state.identity,
                         observed_at=observed, expires_at=expires)
        return True

    def exited(self, execution_id, payload, *, observed_at):
        state = self.executions.get(execution_id)
        if state is None or state.exit_sent:
            return
        # Tool completion is terminal for progress, but it is not the same
        # fact as an authenticated process exit.  Publish each fact once.
        state.exited = True
        state.exit_sent = True
        self.sessions.process_exited(state.identity)
        self.publish(
            "execution.exited",
            payload,
            source="launcher",
            owner=state.identity,
            observed_at=observed_at,
        )

    def completed(self, event, sample=None):
        state = self.executions.get(event.execution_id)
        if state is not None:
            state.exited = True
            self.sessions.process_exited(state.identity)
        completed_at = event_timestamp(event.occurred_at)
        self.sessions.tool_completed(identity(event), completed_at)
        self.publish("tool.completed", {
            "tool_name": event.tool_name, "duration_ms": event.duration_ms,
            "error_type": event.error_type,
            "measurement": sample,
        }, source="plugin", owner=identity(event), observed_at=completed_at)
        self.baselines.pop(getattr(event, "decision_id", None), None)

    def aborted(self, execution_id, reason):
        state = self.executions.get(execution_id)
        if state is not None and not state.exited:
            state.exited = True
            self.sessions.process_exited(state.identity)
            self.publish("execution.aborted", {"reason": reason}, owner=state.identity)

    def _obsolete(self, message):
        if message["expires_at"] is not None and message["expires_at"] <= time.time():
            return True
        if message["type"] in {"prediction.updated", "execution.finishing_soon"}:
            state = self.executions.get(message["identity"]["execution_id"])
            return state is None or state.exited
        return False

    async def _deliver(self):
        owned = self.client is None
        client = self.client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            while True:
                message = await self.queue.get()
                try:
                    deadline = time.monotonic() + 5
                    for attempt in range(3):
                        if self._obsolete(message):
                            self.counts["expired"] += 1
                            break
                        try:
                            headers = {"Authorization": "Bearer " + self.token} if self.token else {}
                            response = await asyncio.wait_for(client.post(
                                self.endpoint, json=message, headers=headers, timeout=self.timeout),
                                timeout=min(self.timeout, max(.001, deadline - time.monotonic())))
                            if 200 <= response.status_code < 300:
                                self.counts["delivered"] += 1
                                break
                            retry = response.status_code in {408, 429} or response.status_code >= 500
                        except (httpx.HTTPError, OSError, asyncio.TimeoutError):  # noqa: UP041 - Python 3.10
                            retry = True
                        except (httpx.InvalidURL, ValueError, TypeError):
                            # An invalid message or endpoint must not kill the
                            # publisher and strand every subsequent notification.
                            retry = False
                        if not retry or attempt == 2 or time.monotonic() >= deadline:
                            self.counts["failed"] += 1
                            logger.warning("signal delivery failed: %s %s", message["type"], message["signal_id"])
                            break
                        await asyncio.sleep(.05 * (attempt + 1))
                finally:
                    self.queue.task_done()
        finally:
            if owned:
                await client.aclose()

    async def _deliver_sessions(self):
        """Independent, coalesced delivery; periodic full snapshots repair disconnects.

        Neither this path nor the plugin promises historical event replay.
        A short per-request bound prevents one receiver failure consuming an ETA.
        """
        owned = self.client is None
        client = self.client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        resync_at = time.monotonic() + 5
        tick_at = 0
        try:
            while True:
                if time.monotonic() >= tick_at:
                    self.sessions.tick()
                    tick_at = time.monotonic() + .1
                if time.monotonic() >= resync_at:
                    if self.endpoint:
                        for message in self.sessions.snapshots():
                            self.session_pending[session_key(message["identity"])] = message
                    resync_at = time.monotonic() + 5
                if not self.session_pending or not self.endpoint:
                    await asyncio.sleep(.1)
                    continue
                _, message = self.session_pending.popitem(last=False)
                if self._obsolete(message):
                    continue
                try:
                    headers = {"Authorization": "Bearer " + self.token} if self.token else {}
                    response = await asyncio.wait_for(client.post(
                        self.endpoint, json=message, headers=headers, timeout=min(.2, self.timeout)),
                        timeout=min(.2, self.timeout))
                    self.counts["session_delivered" if 200 <= response.status_code < 300
                                else "session_failed"] += 1
                except (httpx.HTTPError, OSError, asyncio.TimeoutError, httpx.InvalidURL,  # noqa: UP041 - Python 3.10
                        ValueError, TypeError):
                    self.counts["session_failed"] += 1
                # Failed state is retried by periodic resync, never by blocking newer state.
        finally:
            if owned:
                await client.aclose()

    async def close(self):
        self.closed = True
        if self.session_task is not None:
            self.session_task.cancel()
            await asyncio.gather(self.session_task, return_exceptions=True)
        if self.task is not None:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=1.1)
            except asyncio.TimeoutError:  # noqa: UP041 - Python 3.10
                self.counts["shutdown_pending"] += self.queue.qsize()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def status(self):
        return {"publisher_id": self.publisher_id, "sequence": self.sequence,
                "enabled": bool(self.endpoint), "queued": self.queue.qsize(),
                "session_queued": len(self.session_pending),
                "counters": dict(self.counts)}
