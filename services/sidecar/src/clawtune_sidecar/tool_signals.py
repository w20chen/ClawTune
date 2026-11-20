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
import urllib.error
import urllib.request
from pathlib import Path

if __package__:
    from .signal_protocol import envelope
    from .tool_progress import RemainingTime, ToolProgress, adapter_for
else:  # Executed by absolute path, including inside a launcher-only sandbox.
    from signal_protocol import envelope
    from tool_progress import RemainingTime, ToolProgress, adapter_for


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_open_request = urllib.request.build_opener(_NoRedirect()).open


def wrap_tool_command(command: str, execution_id: str, *, endpoint=None, update_token=None) -> str:
    if os.name != "posix" or not endpoint or not update_token:
        return command
    # The execution-scoped token is passed only to the trusted supervisor and
    # removed before it creates the user's process tree. Never put it in argv.
    os.environ["CLAWTUNE_SIGNAL_INGRESS"] = endpoint.rstrip("/") + "/v1/signals"
    os.environ["CLAWTUNE_SIGNAL_UPDATE_TOKEN"] = update_token
    return "exec " + shlex.join([
        sys.executable, str(Path(__file__).resolve()), execution_id, command,
    ])


class ToolObservations:
    def __init__(self, execution_id, is_running):
        self.endpoint = os.environ["CLAWTUNE_SIGNAL_INGRESS"]
        self.token = os.environ["CLAWTUNE_SIGNAL_UPDATE_TOKEN"]
        self.identity = {"execution_id": execution_id}
        self.is_running = is_running
        self.pending = queue.Queue(maxsize=128)
        self.sequence = 0
        self.worker = threading.Thread(target=self._deliver, daemon=True)
        self.worker.start()

    def _put(self, kind, payload, expires_at=None):
        self.sequence += 1
        message = envelope(kind, payload, source="launcher", identity=self.identity,
                           sequence=self.sequence, expires_at=expires_at)
        try:
            self.pending.put_nowait(message)
        except queue.Full:
            print("clawtune observation queue full", file=sys.stderr)

    def start(self, adapter, pid):
        self._put("execution.started", {"pid": pid, "progress_adapter": type(adapter).__name__})

    def remaining(self, value: RemainingTime | None, elapsed_seconds: float | None = None):
        if (value is None or not self.is_running() or elapsed_seconds is None
                or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0
                or not math.isfinite(value.seconds) or value.seconds <= 0):
            return
        self._put("execution.progress", {
            "elapsed_duration_ms": elapsed_seconds * 1000,
            "remaining_duration_ms": value.seconds * 1000,
            "remaining_source": value.source, "remaining_kind": value.kind,
        }, expires_at=time.time() + min(2, value.seconds))

    def close(self):
        try:
            self.pending.put_nowait(None)
        except queue.Full:
            # The daemon must stop after draining its bounded queue.
            self.stopping = True
        self.worker.join(timeout=1.1)

    def _deliver(self):
        while True:
            try:
                message = self.pending.get(timeout=.1)
            except queue.Empty:
                if getattr(self, "stopping", False):
                    return
                continue
            if message is None:
                return
            for attempt in range(2):
                if message["type"] == "execution.progress" and (
                        not self.is_running() or message["expires_at"] <= time.time()):
                    break
                retry = True
                try:
                    request = urllib.request.Request(self.endpoint,
                        data=json.dumps(message, allow_nan=False).encode(),
                        headers={"content-type": "application/json",
                                 "Authorization": "Bearer " + self.token}, method="POST")
                    with _open_request(request, timeout=.5):
                        pass
                    break
                except urllib.error.HTTPError as exc:
                    retry = exc.code in {408, 429} or exc.code >= 500
                except (OSError, ValueError):
                    pass
                if attempt == 1 or not retry:
                    print("clawtune observation delivery failed: " + message["type"], file=sys.stderr)
                    break
                time.sleep(.05)


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
    env.pop("CLAWTUNE_SIGNAL_INGRESS", None)
    env.pop("CLAWTUNE_SIGNAL_UPDATE_TOKEN", None)
    env.pop("CLAWTUNE_TOKEN", None)
    env.pop("CLAWTUNE_EXECUTION_TOKEN", None)
    env.pop("CLAWTUNE_SIGNAL_TOKEN", None)
    env.pop("CLAWTUNE_SIGNAL_ENDPOINT", None)
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
        signals = ToolObservations(execution_id, lambda: child.poll() is None)
        signals.start(adapter, child.pid)
        progress = _ProgressReader(adapter)
        if observes_output:
            _observe_output(child, adapter, progress, signals, started)
        else:
            print(f"clawtune progress unavailable: {type(adapter).__name__}", file=sys.stderr)
            while child.poll() is None:
                signals.remaining(progress.remaining(), time.monotonic() - started)
                time.sleep(0.05)
        returncode = child.wait()
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
