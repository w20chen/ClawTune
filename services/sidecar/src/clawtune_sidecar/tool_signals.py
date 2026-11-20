"""Run one gated payload, observing its output without a progress file."""
from __future__ import annotations

import json
import math
import os
import queue
import re
import selectors
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

if __package__:
    from .tool_progress import RemainingTime, ToolProgress, adapter_for
else:  # Executed by absolute path, including inside a launcher-only sandbox.
    from tool_progress import RemainingTime, ToolProgress, adapter_for


def _duration_from_env(name: str, *, allow_zero: bool = False) -> float | None:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        return None
    return value


def wrap_tool_command(command: str, execution_id: str) -> str:
    if os.name != "posix" or not os.environ.get("CLAWTUNE_GPU_SIGNAL_ENDPOINT"):
        return command
    if (_duration_from_env("CLAWTUNE_PREDICTED_TOOL_DURATION_MS") is None
            and _duration_from_env("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", allow_zero=True) is None):
        return command
    # The launcher signals and waits for this PID. Leaving a shell between it
    # and the supervisor loses cancellation and reports exit before the payload.
    return "exec " + shlex.join([
        sys.executable, str(Path(__file__).resolve()), execution_id, command,
    ])


class CpuToolSignals:
    def __init__(self, execution_id, is_running):
        self.endpoint = os.environ["CLAWTUNE_GPU_SIGNAL_ENDPOINT"]
        self.predicted_ms = _duration_from_env("CLAWTUNE_PREDICTED_TOOL_DURATION_MS")
        self.lattice_predicted_ms = _duration_from_env(
            "CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", allow_zero=True)
        request_id = os.environ.get("CLAWTUNE_TOOL_CALL_ID") or execution_id
        self.identity = {
            "schema_version": 1, "request_id": request_id,
            "engine_request_id": execution_id, "agent_request_id": request_id,
            "session_id": os.environ.get("CLAWTUNE_SESSION_ID") or None,
            "engine_id": "cpu-tool", "role": "A", "engine_step": 0,
        }
        self.is_running = is_running
        self.finishing_sent = False
        self.update_sent = False
        self.pending = queue.Queue()
        self.worker = threading.Thread(target=self._deliver, daemon=True)
        self.worker.start()

    def start(self, adapter):
        if self.predicted_ms is None:
            return
        self.pending.put({"event": "tool_start", "t": time.time(),
                          "predicted_duration_ms": self.predicted_ms,
                          "progress_adapter": type(adapter).__name__})

    def remaining(self, value: RemainingTime | None, elapsed_seconds: float | None = None):
        if (value is None or not self.is_running()
                or not math.isfinite(value.seconds) or value.seconds <= 0):
            return
        # Only fresh progress observations reach here; do not count down an old
        # ETA or the original KB prediction to manufacture an overrun update.
        if (not self.update_sent and self.lattice_predicted_ms is not None
                and elapsed_seconds is not None and math.isfinite(elapsed_seconds)):
            elapsed_ms = elapsed_seconds * 1000
            remaining_ms = value.seconds * 1000
            total_ms = elapsed_ms + remaining_ms
            if (elapsed_ms > self.lattice_predicted_ms + 1000 and math.isfinite(total_ms)):
                self.update_sent = True
                self.pending.put({
                    "event": "tool_prediction_updated", "t": time.time(),
                    "baseline_source": "lattice", "baseline_statistic": "p50",
                    "baseline_prediction_ms": self.lattice_predicted_ms,
                    "overrun_threshold_ms": 1000, "elapsed_duration_ms": elapsed_ms,
                    "remaining_duration_ms": remaining_ms,
                    "predicted_total_duration_ms": total_ms,
                    "remaining_source": value.source, "remaining_kind": value.kind,
                })
        if self.predicted_ms is None or self.finishing_sent or value.seconds > 2:
            return
        self.finishing_sent = True
        self.pending.put({"event": "tool_finishing_soon", "t": time.time(),
                          "remaining_duration_ms": value.seconds * 1000,
                          "remaining_source": value.source, "remaining_kind": value.kind})

    def close(self):
        self.pending.put(None)
        # urllib's timeout bounds individual socket operations, not the whole
        # response. An unhealthy receiver must not hold the execution open.
        self.worker.join(timeout=1.1)

    def _deliver(self):
        while (event := self.pending.get()) is not None:
            if event["event"] != "tool_start" and not self.is_running():
                continue
            try:
                request = urllib.request.Request(self.endpoint,
                    data=json.dumps({**self.identity, **event}).encode(),
                    headers={"content-type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=1):
                    pass
            except (OSError, ValueError) as exc:
                # Signal delivery must not cancel or replay the actual tool.
                print(f"clawtune signal {event['event']} failed: {exc}", file=sys.stderr)


def _forward(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _ProgressReader:
    """Disable a faulty observer while continuing to drain the payload output."""

    def __init__(self, adapter):
        self.adapter = adapter

    def remaining(self, line=None, elapsed=0):
        try:
            exact = self.adapter.exact_remaining()
            if exact is not None or line is None:
                return exact
            return self.adapter.observe(line, elapsed)
        except Exception as exc:  # noqa: BLE001 -- optional observers must not cancel payloads
            print(f"clawtune progress disabled: {type(exc).__name__}", file=sys.stderr)
            self.adapter = ToolProgress()
            return None


def run_tool(command: str, execution_id: str) -> int:
    adapter = adapter_for(command)
    observes_output = type(adapter).observe is not ToolProgress.observe
    outputs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if observes_output else {}
    # Children must not inherit eligibility and accidentally emit this call's
    # signals again if they themselves use a ClawTune launcher.
    env = dict(os.environ)
    env.pop("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", None)
    env.pop("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", None)
    child = None
    signals = None
    pending_signals = []

    def forward(signum, _frame):
        if child is None:
            pending_signals.append(signum)
        else:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    # Install before spawning: cancellation can arrive while Popen is creating
    # the child, before its process-group ID has been returned to this process.
    previous = {sig: signal.signal(sig, forward)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        started = time.monotonic()
        child = subprocess.Popen(["/bin/sh", "-c", command], env=env,
                                 start_new_session=True, **outputs)
        for signum in pending_signals:
            forward(signum, None)
        signals = CpuToolSignals(execution_id, lambda: child.poll() is None)
        signals.start(adapter)
        progress = _ProgressReader(adapter)
        if observes_output:
            _observe_output(child, adapter, progress, signals, started)
        else:
            print(f"clawtune progress unavailable: {type(adapter).__name__}", file=sys.stderr)
            while child.poll() is None:
                signals.remaining(progress.remaining(), time.monotonic() - started)
                time.sleep(0.05)
        returncode = child.wait()
        if observes_output and signals.predicted_ms is not None and not signals.finishing_sent:
            print(f"clawtune progress: no usable <=2s observation from {type(adapter).__name__}",
                  file=sys.stderr)
    finally:
        if child is not None and child.poll() is None:
            forward(signal.SIGTERM, None)
            child.wait()
        if signals is not None:
            signals.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return returncode


def _observe_output(child, adapter, progress, signals, started):
    pending = {1: b"", 2: b""}
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ, 1)
        selector.register(child.stderr, selectors.EVENT_READ, 2)
        while selector.get_map():
            for key, _ in selector.select(timeout=0.05):
                data = os.read(key.fileobj.fileno(), 65536)
                fd = key.data
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                _forward(fd, data)
                previous_pending = pending[fd]
                lines = re.split(rb"[\r\n]", previous_pending + data)
                pending[fd] = lines.pop()[-65536:]
                # We already observed the unterminated update on the previous
                # read. Its later newline alone is not a fresh ETA observation.
                if previous_pending and lines and lines[0] == previous_pending:
                    lines.pop(0)
                elapsed = time.monotonic() - started
                # CR-based meters often leave the current update unterminated
                # until the next refresh. Parse it now instead of one second late.
                for line in [*lines, pending[fd]]:
                    # Restrict parsing to the tool's progress stream. In
                    # particular a downloaded response body is not progress.
                    if fd == adapter.output_fd and child.poll() is None:
                        signals.remaining(progress.remaining(
                            re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line.decode("utf-8", errors="replace")),
                            elapsed), elapsed)
            if child.poll() is None:
                signals.remaining(progress.remaining(), time.monotonic() - started)


if __name__ == "__main__":
    status = run_tool(sys.argv[2], sys.argv[1])
    if status < 0:
        # Preserve waitpid/Popen signal status for the launcher's /exited event;
        # normal exit(128 + signal) would incorrectly turn it into an exit code.
        signum = -status
        if signum not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(status)
