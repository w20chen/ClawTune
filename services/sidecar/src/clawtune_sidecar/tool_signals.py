"""Run one gated payload, observing its output without a progress file."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
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

if __package__:
    from .tool_progress import RemainingTime, ToolProgress, adapter_for
else:  # Executed by absolute path, including inside a launcher-only sandbox.
    from tool_progress import RemainingTime, ToolProgress, adapter_for


def wrap_tool_command(command: str, execution_id: str) -> str:
    if not (os.environ.get("CLAWTUNE_GPU_SIGNAL_ENDPOINT")
            and os.environ.get("CLAWTUNE_PREDICTED_TOOL_DURATION_MS")):
        return command
    return shlex.join([sys.executable, str(Path(__file__).resolve()), execution_id, command])


class CpuToolSignals:
    def __init__(self, execution_id, is_running):
        self.endpoint = os.environ["CLAWTUNE_GPU_SIGNAL_ENDPOINT"]
        self.predicted_ms = float(os.environ["CLAWTUNE_PREDICTED_TOOL_DURATION_MS"])
        request_id = os.environ.get("CLAWTUNE_TOOL_CALL_ID") or execution_id
        self.identity = dict(schema_version=1, request_id=request_id,
                             engine_request_id=execution_id, agent_request_id=request_id,
                             session_id=os.environ.get("CLAWTUNE_SESSION_ID") or None,
                             engine_id="cpu-tool", role="A", engine_step=0)
        self.is_running = is_running
        self.finishing_sent = False
        self.pending = queue.Queue()
        self.worker = threading.Thread(target=self._deliver, daemon=True)
        self.worker.start()

    def start(self, adapter):
        self.pending.put(dict(event="tool_start", t=time.time(),
                              predicted_duration_ms=self.predicted_ms,
                              progress_adapter=type(adapter).__name__))

    def remaining(self, value: RemainingTime | None):
        if (value is None or self.finishing_sent or not self.is_running()
                or not math.isfinite(value.seconds) or not 0 < value.seconds <= 2):
            return
        self.finishing_sent = True
        self.pending.put(dict(event="tool_finishing_soon", t=time.time(),
                              remaining_duration_ms=value.seconds * 1000,
                              remaining_source=value.source, remaining_kind=value.kind))

    def close(self):
        self.pending.put(None)
        self.worker.join()

    def _deliver(self):
        while (event := self.pending.get()) is not None:
            if event["event"] == "tool_finishing_soon" and not self.is_running():
                continue
            request = urllib.request.Request(self.endpoint,
                data=json.dumps({**self.identity, **event}).encode(),
                headers={"content-type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=1):
                    pass
            except OSError as exc:
                # Signal delivery must not cancel or replay the actual tool.
                print(f"clawtune signal {event['event']} failed: {exc}", file=sys.stderr)


def _forward(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def run_tool(command: str, execution_id: str) -> int:
    adapter = adapter_for(command)
    observes_output = type(adapter).observe is not ToolProgress.observe
    outputs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE) if observes_output else {}
    # Children must not inherit eligibility and accidentally emit this call's
    # signals again if they themselves use a ClawTune launcher.
    env = dict(os.environ)
    env.pop("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", None)
    started = time.monotonic()
    child = subprocess.Popen(["/bin/sh", "-c", command], env=env, start_new_session=True, **outputs)
    signals = CpuToolSignals(execution_id, lambda: child.poll() is None)
    signals.start(adapter)
    if not observes_output:
        print(f"clawtune progress unavailable: {type(adapter).__name__}", file=sys.stderr)

    def forward(signum, _frame):
        if child.poll() is None:
            os.killpg(child.pid, signum)

    previous = {sig: signal.signal(sig, forward)
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        if observes_output:
            _observe_output(child, adapter, signals, started)
        else:
            while child.poll() is None:
                signals.remaining(adapter.exact_remaining())
                time.sleep(0.05)
        returncode = child.wait()
        if observes_output and not signals.finishing_sent:
            print(f"clawtune progress: no usable <=2s observation from {type(adapter).__name__}",
                  file=sys.stderr)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        signals.close()
    return returncode if returncode >= 0 else 128 - returncode


def _observe_output(child, adapter, signals, started):
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
                lines = re.split(rb"[\r\n]", pending[fd] + data)
                pending[fd] = lines.pop()[-65536:]
                elapsed = time.monotonic() - started
                # CR-based meters often leave the current update unterminated
                # until the next refresh. Parse it now instead of one second late.
                for line in [*lines, pending[fd]]:
                    # Restrict parsing to the tool's progress stream. In
                    # particular a downloaded response body is not progress.
                    if fd == adapter.output_fd and child.poll() is None:
                        exact = adapter.exact_remaining()
                        signals.remaining(exact if exact is not None else adapter.observe(
                            re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line.decode("utf-8", errors="replace")),
                            elapsed))
            if child.poll() is None:
                signals.remaining(adapter.exact_remaining())


if __name__ == "__main__":
    raise SystemExit(run_tool(sys.argv[2], sys.argv[1]))
