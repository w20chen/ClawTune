"""Real processes and HTTP; progress is produced by curl/wget/pytest themselves.

The HTTP fixture stands in for sidecar registration and the GPU receiver only.
It never supplies tool progress or a remaining duration.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clawtune_sidecar.tool_progress import RemainingTime, ToolProgress
from clawtune_sidecar.tool_signals import CpuToolSignals

requires_posix = pytest.mark.skipif(os.name != "posix", reason="requires POSIX launcher processes")


@pytest.fixture(autouse=True)
def clear_inherited_lattice_prediction(monkeypatch):
    monkeypatch.delenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", raising=False)


@pytest.fixture
def signal_validator():
    schema_path = Path(__file__).resolve().parents[3] / "contracts/cpu-tool-signal.schema.json"
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def service(signal_validator):
    rows, commands, exits = [], {}, []
    content = bytes(range(256)) * 2048

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            for offset in range(0, len(content), 16384):
                self.wfile.write(content[offset:offset + 16384])
                self.wfile.flush()
                time.sleep(0.12)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v2/executions/claim" and payload.get("token") == "unavailable":
                self.send_response(503)
                self.end_headers()
                return
            response = {"stored": True}
            if self.path == "/v2/executions/claim":
                response = {"command": commands[payload["execution_id"]], "update_token": "test",
                            "profiling": {"enable_cgroup": False, "enable_affinity": False,
                                          "enable_numa": False}}
            elif self.path == "/cpu-events":
                signal_validator.validate(payload)
                rows.append({**payload, "received_at": time.time()})
            elif self.path.endswith("/exited"):
                exits.append(payload)
            self.send_response(503 if self.path == "/failure" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", rows, commands, content, exits
    server.shutdown()
    server.server_close()
    thread.join()


def launcher_invocation(service, command, mode, *, prediction="999999", lattice_prediction="",
                        endpoint="/cpu-events", token="test"):
    url, _, commands, _, _ = service
    commands["test-exec"] = command
    env = dict(os.environ, CLAWTUNE_GPU_SIGNAL_ENDPOINT=url + endpoint,
               CLAWTUNE_PREDICTED_TOOL_DURATION_MS=prediction, CLAWTUNE_LAUNCH_MODE=mode,
               CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS=lattice_prediction,
               CLAWTUNE_EXECUTION_ID="test-exec", CLAWTUNE_TOOL_CALL_ID="test-call",
               CLAWTUNE_SESSION_ID="test-session", CLAWTUNE_CGROUP_REQUIRED="0",
               CLAWTUNE_ENABLE_CGROUP="0",
               CLAWTUNE_PAYLOAD_COMMAND=command, CLAWTUNE_TELEMETRY_FAIL_OPEN="1")
    env.pop("CLAWTUNE_EXECUTION_TOKEN", None)
    # Reproduce a sandbox whose launcher-only PYTHONPATH is stripped for payloads.
    src = str(Path(__file__).resolve().parents[1] / "src")
    env.update(PYTHONPATH=src, CLAWTUNE_LAUNCHER_PYTHONPATH=src)
    argv = [sys.executable, "-m", "clawtune_sidecar.launcher", "run",
            "--execution-id", "test-exec", "--endpoint", url]
    if token is not None:
        argv.extend(["--token", token])
    return argv, env


def run_launcher(service, command, mode, tmp_path, *, expected_returncode=0, **kwargs):
    argv, env = launcher_invocation(service, command, mode, **kwargs)
    result = subprocess.run(argv, env=env, capture_output=True, timeout=25, check=False)
    rows = service[1]
    finished = time.time()
    (tmp_path / "stdout.bin").write_bytes(result.stdout)
    (tmp_path / "stderr.txt").write_bytes(result.stderr)
    (tmp_path / "signals.json").write_text(json.dumps({"command": command,
        "launcher_mode": mode, "finished_at": finished, "events": rows}, indent=2))
    assert result.returncode == expected_returncode, result.stderr.decode(errors="replace")
    return result, finished


@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
@pytest.mark.parametrize("tool", ["curl", "wget", "pytest"])
@requires_posix
def test_real_tool_progress_emits_before_exit_without_progress_files(service, tmp_path, mode, tool):
    url, rows, _, content, _ = service
    if tool in {"curl", "wget"} and not shutil.which(tool):
        pytest.skip(f"{tool} is not installed")
    if tool == "curl":
        command = shlex.join(["curl", "--show-error", url + "/data"])
    elif tool == "wget":
        command = shlex.join(["wget", "--progress=bar:force:noscroll", "-O", "-", url + "/data"])
    else:
        workload = tmp_path / "test_workload.py"
        workload.write_text("import hashlib, time, pytest\n"
            "@pytest.mark.parametrize('case', range(6))\n"
            "def test_hash_work(case):\n"
            "    end = time.monotonic() + .5\n"
            "    while time.monotonic() < end:\n"
            "        assert len(hashlib.sha256(bytes(65536)).digest()) == 32\n")
        command = shlex.join([sys.executable, "-m", "pytest", "-v", "--color=no", str(workload)])
    result, finished = run_launcher(service, command, mode, tmp_path)
    assert [r["event"] for r in rows] == ["tool_start", "tool_finishing_soon"], result.stderr
    start, ending = rows
    assert start["predicted_duration_ms"] == 999999  # Deliberately unrelated to real runtime.
    assert ending["remaining_kind"] == "estimated"
    assert ending["remaining_source"].startswith(tool + ".")
    assert 0 < ending["remaining_duration_ms"] <= 2000
    assert start["received_at"] <= ending["received_at"] < finished
    assert ending["engine_request_id"] == "test-exec"
    assert ending["request_id"] == "test-call"
    assert ending["session_id"] == "test-session"
    if tool != "pytest":
        assert result.stdout == content  # The binary tool result is untouched.
    else:
        assert b"6 passed" in result.stdout


@requires_posix
def test_old_progress_file_and_elapsed_prediction_cannot_trigger_signal(service, tmp_path, monkeypatch):
    progress = tmp_path / "obsolete-progress"
    progress.write_text("1.5")
    monkeypatch.setenv("CLAWTUNE_TOOL_PROGRESS_PATH", str(progress))
    command = shlex.join([sys.executable, "-c", "import time; time.sleep(.3); print('done')"])
    result, _ = run_launcher(service, command, "subprocess", tmp_path, prediction="200")
    assert result.stdout == b"done\n"
    assert [r["event"] for r in service[1]] == ["tool_start"]


@requires_posix
def test_ineligible_call_runs_without_either_signal(service, tmp_path):
    result, _ = run_launcher(service, "printf done", "subprocess", tmp_path, prediction="")
    assert result.stdout == b"done"
    assert service[1] == []


@pytest.mark.skipif(os.name != "nt", reason="requires the native Windows launcher")
def test_windows_preserves_execution_when_posix_signals_are_configured(service, tmp_path):
    command = subprocess.list2cmdline([sys.executable, "-c", "print('done')"])
    result, _ = run_launcher(service, command, "subprocess", tmp_path)
    assert result.stdout.strip() == b"done"
    assert service[1] == []
    assert service[4] == [{"update_token": "test", "exit_code": 0, "signal": None}]


@requires_posix
def test_signal_http_failure_preserves_tool_execution(service, tmp_path):
    result, _ = run_launcher(service, "printf done", "subprocess", tmp_path, endpoint="/failure")
    assert result.stdout == b"done"
    assert b"signal tool_start failed" in result.stderr


@requires_posix
@pytest.mark.parametrize("token", [None, "unavailable"])
@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
def test_registration_fallback_executes_once_with_signals(service, tmp_path, token, mode):
    marker = tmp_path / "execution-count"
    command = f"printf x >> {shlex.quote(str(marker))}; printf done"
    result, _ = run_launcher(service, command, mode, tmp_path, token=token)
    assert result.stdout == b"done"
    assert marker.read_text() == "x"
    assert [row["event"] for row in service[1]] == ["tool_start"]
    assert service[4] == []


def test_exact_interface_preserves_kind_and_sends_at_most_once(service, monkeypatch):
    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "5000")
    signals = CpuToolSignals("exact-interface", lambda: True)
    signals.remaining(RemainingTime(3, "test.exact_api", "exact"))
    signals.remaining(RemainingTime(1.5, "test.exact_api", "exact"))
    signals.remaining(RemainingTime(1, "test.exact_api", "exact"))
    signals.close()
    assert len(service[1]) == 1
    assert service[1][0]["remaining_kind"] == "exact"
    assert service[1][0]["remaining_duration_ms"] == 1500


def _running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@requires_posix
@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", 1)])
def test_cancel_reaches_payload_tree_and_preserves_exit_signal(service, tmp_path, mode, signum):
    ready = tmp_path / "ready.json"
    descendant_ready = tmp_path / "descendant-ready"
    grandchild = (
        "import signal,time,pathlib; "
        "signal.signal(signal.SIGINT, signal.SIG_DFL); "
        f"pathlib.Path({str(descendant_ready)!r}).touch(); time.sleep(30)"
    )
    workload = tmp_path / "workload.py"
    workload.write_text(
        "import json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
        f"child = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"while not Path({str(descendant_ready)!r}).exists(): time.sleep(.01)\n"
        f"ready = Path({str(ready)!r})\n"
        "ready.with_suffix('.tmp').write_text(json.dumps([os.getpid(), child.pid]))\n"
        "ready.with_suffix('.tmp').replace(ready)\n"
        "time.sleep(30)\n"
    )
    command = "exec " + shlex.join([sys.executable, str(workload)])
    argv, env = launcher_invocation(service, command, mode)
    process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    pids = []
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), "payload did not start"
        pids = json.loads(ready.read_text())
        process.send_signal(signum)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 128 + signum, stderr.decode(errors="replace")
        deadline = time.monotonic() + 2
        while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not any(_running(pid) for pid in pids), "cancelled payload survived launcher exit"
        assert service[4] == [{"update_token": "test", "exit_code": None, "signal": signum}]
    finally:
        if pids:
            try:
                os.killpg(pids[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)


@requires_posix
@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
@pytest.mark.parametrize("exit_code", [0, 7, 143])
def test_normal_exit_code_and_shell_output_are_preserved(service, tmp_path, mode, exit_code):
    result, _ = run_launcher(service,
        f"printf 'one\\000two'; printf error >&2; exit {exit_code}", mode, tmp_path,
        expected_returncode=exit_code)
    assert result.stdout == b"one\x00two"
    assert b"error" in result.stderr
    assert service[4] == [{"update_token": "test", "exit_code": exit_code, "signal": None}]


@requires_posix
@pytest.mark.parametrize("prediction", ["invalid", "nan", "inf", "-1", "0"])
def test_invalid_signal_configuration_does_not_break_execution(service, tmp_path, prediction):
    result, _ = run_launcher(service, "printf done", "subprocess", tmp_path, prediction=prediction)
    assert result.stdout == b"done"
    assert service[1] == []


@requires_posix
def test_broken_progress_observer_does_not_cancel_or_truncate_payload(service, monkeypatch, capfd):
    from clawtune_sidecar import tool_signals

    class BrokenProgress(ToolProgress):
        def observe(self, line, elapsed):
            raise ValueError("unsupported progress")

    monkeypatch.setattr(tool_signals, "adapter_for", lambda _command: BrokenProgress())
    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "5000")
    command = shlex.join([sys.executable, "-c",
        ("import sys,time; print('start', flush=True); time.sleep(.2); "
         "sys.stdout.buffer.write(b'end\\x00'); sys.exit(7)")])
    assert tool_signals.run_tool(command, "broken-progress") == 7
    output = capfd.readouterr()
    assert output.out == "start\nend\x00"
    assert output.err.count("progress disabled") == 1
    assert [row["event"] for row in service[1]] == ["tool_start"]


def test_signal_shutdown_does_not_wait_forever_for_receiver(monkeypatch):
    from clawtune_sidecar import tool_signals

    entered, release = threading.Event(), threading.Event()

    def stalled_receiver(*_args, **_kwargs):
        entered.set()
        release.wait(5)
        raise OSError("receiver stalled")

    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", "http://receiver/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "5000")
    monkeypatch.setattr(tool_signals.urllib.request, "urlopen", stalled_receiver)
    signals = CpuToolSignals("stalled", lambda: True)
    try:
        signals.start(ToolProgress())
        assert entered.wait(2)
        started = time.monotonic()
        signals.close()
        assert time.monotonic() - started < 2
    finally:
        release.set()
        signals.worker.join(timeout=2)


@pytest.mark.parametrize("changes", [
    {"remaining_duration_ms": 0}, {"remaining_duration_ms": 2001},
    {"remaining_kind": "unknown"}, {"schema_version": 2},
    {"event": "tool_start"}, {"predicted_duration_ms": 5000},
])
def test_signal_contract_rejects_incompatible_events(signal_validator, changes):
    example_path = (Path(__file__).resolve().parents[3]
                    / "contracts/examples/cpu-tool-finishing-soon.json")
    payload = {**json.loads(example_path.read_text()), **changes}
    assert not signal_validator.is_valid(payload)


def test_overrun_uses_lattice_threshold_and_fresh_progress_once(service, monkeypatch):
    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "999999")
    monkeypatch.setenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", "5000")
    signals = CpuToolSignals("overrun", lambda: True)
    try:
        signals.start(ToolProgress())
        signals.remaining(RemainingTime(10, "test.live_eta"), 5.999)
        signals.remaining(RemainingTime(10, "test.live_eta"), 6)
        assert not signals.update_sent  # Strictly more than prediction + 1 second.
        for seconds in [None, 0, -1, float("nan"), float("inf")]:
            signals.remaining(None if seconds is None else RemainingTime(seconds, "test"), 6.1)
        assert not signals.update_sent
        signals.remaining(RemainingTime(4, "test.live_eta"), 6.2)
        signals.remaining(RemainingTime(3, "test.live_eta"), 7)
        signals.remaining(RemainingTime(1, "test.live_eta"), 8)
    finally:
        signals.close()
    assert [row["event"] for row in service[1]] == [
        "tool_start", "tool_prediction_updated", "tool_finishing_soon"]
    update = service[1][1]
    assert update["baseline_source"] == "lattice"
    assert update["baseline_statistic"] == "p50"
    assert update["baseline_prediction_ms"] == 5000
    assert update["elapsed_duration_ms"] == pytest.approx(6200)
    assert update["remaining_duration_ms"] == 4000
    assert update["predicted_total_duration_ms"] == pytest.approx(10200)


def test_overrun_can_follow_an_earlier_finishing_estimate(service, monkeypatch):
    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "5000")
    monkeypatch.setenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", "5000")
    signals = CpuToolSignals("revised-eta", lambda: True)
    try:
        signals.remaining(RemainingTime(1, "test.live_eta"), 4)
        signals.remaining(RemainingTime(5, "test.live_eta"), 7)
    finally:
        signals.close()
    assert [row["event"] for row in service[1]] == ["tool_finishing_soon", "tool_prediction_updated"]


@pytest.mark.parametrize("baseline", ["", "invalid", "nan", "inf", "-1"])
def test_overrun_never_falls_back_to_primary_prediction(service, monkeypatch, baseline):
    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "100")
    monkeypatch.setenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", baseline)
    signals = CpuToolSignals("no-lattice", lambda: True)
    try:
        signals.remaining(RemainingTime(10, "test.live_eta"), 100)
    finally:
        signals.close()
    assert service[1] == []


def test_zero_lattice_prediction_and_no_update_after_exit(service, monkeypatch):
    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "")
    monkeypatch.setenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", "0")
    signals = CpuToolSignals("zero-baseline", lambda: True)
    try:
        signals.start(ToolProgress())
        signals.remaining(RemainingTime(3, "test.live_eta"), 1.001)
    finally:
        signals.close()
    assert [row["event"] for row in service[1]] == ["tool_prediction_updated"]
    assert service[1][0]["predicted_total_duration_ms"] == pytest.approx(4001)
    stopped = CpuToolSignals("already-exited", lambda: False)
    try:
        stopped.remaining(RemainingTime(3, "test.live_eta"), 5)
    finally:
        stopped.close()
    assert len(service[1]) == 1


def test_queued_overrun_is_dropped_when_payload_exits_before_delivery(monkeypatch):
    from clawtune_sidecar import tool_signals

    entered, release = threading.Event(), threading.Event()
    delivered = []
    running = True

    def blocked_start(request, **_kwargs):
        delivered.append(json.loads(request.data))
        entered.set()
        release.wait(5)
        raise OSError("start delivery failed")

    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", "http://receiver/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "5000")
    monkeypatch.setenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", "100")
    monkeypatch.setattr(tool_signals.urllib.request, "urlopen", blocked_start)
    signals = CpuToolSignals("exit-in-queue", lambda: running)
    try:
        signals.start(ToolProgress())
        assert entered.wait(2)
        signals.remaining(RemainingTime(3, "test.live_eta"), 2)
        running = False
    finally:
        release.set()
        signals.close()
    assert [row["event"] for row in delivered] == ["tool_start"]


@requires_posix
@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
@pytest.mark.parametrize("legacy_prediction", ["", "999999"])
def test_real_pytest_overrun_emits_current_and_total_estimates(service, tmp_path, mode,
                                                             legacy_prediction):
    workload = tmp_path / "test_overrun_workload.py"
    workload.write_text(
        "import time, pytest\n"
        "@pytest.mark.parametrize('case', range(8))\n"
        "def test_work(case):\n    time.sleep(.4)\n")
    command = shlex.join([sys.executable, "-m", "pytest", "-v", "--color=no", str(workload)])
    result, finished = run_launcher(service, command, mode, tmp_path,
        prediction=legacy_prediction, lattice_prediction="200")
    events = service[1]
    expected = (["tool_start", "tool_prediction_updated", "tool_finishing_soon"]
                if legacy_prediction else ["tool_prediction_updated"])
    assert [row["event"] for row in events] == expected, result.stderr
    update = next(row for row in events if row["event"] == "tool_prediction_updated")
    assert update["baseline_prediction_ms"] == 200
    assert update["elapsed_duration_ms"] > 1200
    assert update["remaining_duration_ms"] > 0
    assert update["predicted_total_duration_ms"] == pytest.approx(
        update["elapsed_duration_ms"] + update["remaining_duration_ms"])
    assert update["remaining_source"] == "pytest.completed_percent"
    assert update["remaining_kind"] == "estimated"
    assert update["received_at"] < finished
    assert b"8 passed" in result.stdout


@requires_posix
def test_unsupported_tool_does_not_invent_progress_or_leak_prediction(service, tmp_path):
    command = shlex.join([sys.executable, "-c",
        ("import os,time; time.sleep(1.2); "
         "assert 'CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS' not in os.environ; "
         "assert 'CLAWTUNE_PREDICTED_TOOL_DURATION_MS' not in os.environ; print('done')")])
    result, _ = run_launcher(service, command, "subprocess", tmp_path,
                             prediction="", lattice_prediction="0")
    assert result.stdout == b"done\n"
    assert service[1] == []


@requires_posix
def test_supported_but_silent_tool_does_not_invent_overrun_progress(service, tmp_path):
    workload = tmp_path / "test_silent.py"
    workload.write_text("import time\ndef test_wait(): time.sleep(1.2)\n")
    command = shlex.join([sys.executable, "-m", "pytest", "-q", str(workload)])
    result, _ = run_launcher(service, command, "subprocess", tmp_path,
                             prediction="", lattice_prediction="0")
    assert b"1 passed" in result.stdout
    assert service[1] == []


@requires_posix
def test_late_newline_does_not_replay_an_old_eta_as_fresh_progress(service, monkeypatch, capfd):
    from clawtune_sidecar import tool_signals
    from clawtune_sidecar.tool_progress import CurlProgress

    monkeypatch.setenv("CLAWTUNE_GPU_SIGNAL_ENDPOINT", service[0] + "/cpu-events")
    monkeypatch.setenv("CLAWTUNE_PREDICTED_TOOL_DURATION_MS", "")
    monkeypatch.setenv("CLAWTUNE_LATTICE_PREDICTED_TOOL_DURATION_MS", "0")
    monkeypatch.setattr(tool_signals, "adapter_for", lambda _command: CurlProgress())
    line = "80 1024k 80 819k 0 0 200k 0 0:00:08 0:00:04 0:00:04 200k"
    command = shlex.join([sys.executable, "-c",
        (f"import sys,time; sys.stderr.write({line!r}); sys.stderr.flush(); time.sleep(1.2); "
         "sys.stderr.write('\\n'); sys.stderr.flush(); time.sleep(.2)")])
    assert tool_signals.run_tool(command, "delayed-newline") == 0
    assert capfd.readouterr().err == line + "\n"
    assert service[1] == []


@pytest.mark.parametrize("changes", [
    {"baseline_source": "runtime"}, {"baseline_statistic": "p90"},
    {"baseline_prediction_ms": -1}, {"overrun_threshold_ms": 2000},
    {"elapsed_duration_ms": 1000}, {"remaining_duration_ms": 0},
    {"predicted_total_duration_ms": 0},
])
def test_overrun_contract_rejects_invalid_fields(signal_validator, changes):
    example = (Path(__file__).resolve().parents[3]
               / "contracts/examples/cpu-tool-prediction-updated.json")
    assert not signal_validator.is_valid({**json.loads(example.read_text()), **changes})
