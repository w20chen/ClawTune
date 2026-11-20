"""Real processes and HTTP; progress is produced by curl/wget/pytest themselves.

The HTTP fixture stands in for sidecar registration and observation ingestion.
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

from clawtune_kb.contracts import validate
from clawtune_sidecar.tool_progress import ToolProgress
from clawtune_sidecar.tool_signals import ToolObservations

requires_posix = pytest.mark.skipif(os.name != "posix", reason="requires POSIX launcher processes")


@pytest.fixture
def service():
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
                            "signals_enabled": commands.get("enabled", True),
                            "profiling": {"enable_cgroup": False, "enable_affinity": False,
                                          "enable_numa": False}}
            elif self.path == "/v1/signals":
                assert self.headers["Authorization"] == "Bearer test"
                validate(payload, "signal.schema.json")
                rows.append({**payload, "received_at": time.time()})
            elif self.path.endswith("/exited"):
                exits.append(payload)
            self.send_response(503 if commands.get("fail_signals") and self.path == "/v1/signals" else 200)
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


def launcher_invocation(service, command, mode, *, enabled=True, token="test"):
    url, _, commands, _, _ = service
    commands["test-exec"] = command
    commands["enabled"] = enabled
    env = dict(os.environ, CLAWTUNE_LAUNCH_MODE=mode,
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


@requires_posix
@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
@pytest.mark.parametrize("tool", ["curl", "wget", "pytest"])
def test_real_progress_is_sent_to_sidecar_with_identity_and_original_output(service, tmp_path, mode, tool):
    url, rows, _, content, _ = service
    if tool in {"curl", "wget"} and not shutil.which(tool):
        pytest.skip(f"{tool} is not installed")
    if tool == "curl":
        command = shlex.join(["curl", "--show-error", url + "/data"])
    elif tool == "wget":
        command = shlex.join(["wget", "--progress=bar:force:noscroll", "-O", "-", url + "/data"])
    else:
        workload = tmp_path / "test_workload.py"
        workload.write_text("import time,pytest\n@pytest.mark.parametrize('case', range(6))\n"
                            "def test_work(case): time.sleep(.4)\n")
        command = shlex.join([sys.executable, "-m", "pytest", "-v", "--color=no", str(workload)])
    result, finished = run_launcher(service, command, mode, tmp_path)
    assert rows[0]["type"] == "execution.started"
    estimates = [r for r in rows if r["type"] == "execution.progress"]
    assert estimates, result.stderr
    assert all(r["identity"]["execution_id"] == "test-exec" for r in rows)
    assert all(r["payload"]["remaining_kind"] == "estimated" for r in estimates)
    assert all(r["payload"]["remaining_source"].startswith(tool + ".") for r in estimates)
    assert all(r["received_at"] < finished for r in rows)
    if tool != "pytest":
        assert result.stdout == content
    else:
        assert b"6 passed" in result.stdout


@requires_posix
@pytest.mark.parametrize("token", [None, "unavailable"])
@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
def test_unregistered_fail_open_execution_runs_once_without_unauthenticated_observations(service, tmp_path, token, mode):
    marker = tmp_path / "once"
    result, _ = run_launcher(service, f"printf x >> {shlex.quote(str(marker))}; printf done",
                             mode, tmp_path, token=token)
    assert marker.read_text() == "x"
    assert result.stdout == b"done"
    assert service[1] == []


@requires_posix
def test_observation_failure_does_not_cancel_payload(service, tmp_path):
    service[2]["fail_signals"] = True
    result, _ = run_launcher(service, "printf done", "subprocess", tmp_path)
    assert result.stdout == b"done"
    assert b"observation delivery failed" in result.stderr
    assert len({row["signal_id"] for row in service[1]}) == 1


@requires_posix
@pytest.mark.parametrize("enabled", [True, False])
def test_unobservable_payload_has_no_eta_and_no_observer_credentials(service, tmp_path, enabled):
    command = shlex.join([sys.executable, "-c", ("import os; "
        "assert 'CLAWTUNE_SIGNAL_UPDATE_TOKEN' not in os.environ; "
        "assert 'CLAWTUNE_SIGNAL_INGRESS' not in os.environ; print('done')")])
    result, _ = run_launcher(service, command, "subprocess", tmp_path, enabled=enabled)
    assert result.stdout == b"done\n"
    assert [r["type"] for r in service[1]] == (["execution.started"] if enabled else [])


@pytest.mark.skipif(os.name != "nt", reason="native Windows launcher")
def test_windows_preserves_execution_when_observations_are_enabled(service, tmp_path):
    command = subprocess.list2cmdline([sys.executable, "-c", "print('done')"])
    result, _ = run_launcher(service, command, "subprocess", tmp_path)
    assert result.stdout.strip() == b"done"
    assert service[1] == []




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
        assert len(service[4]) == 1
        exit_payload = service[4][0]
        assert exit_payload["update_token"] == "test"
        assert exit_payload["exit_code"] is None
        assert exit_payload["signal"] == signum
        assert exit_payload["observed_at"] > 0
        assert exit_payload["elapsed_duration_ms"] > 0
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
    assert len(service[4]) == 1
    exit_payload = service[4][0]
    assert exit_payload["update_token"] == "test"
    assert exit_payload["exit_code"] == exit_code
    assert exit_payload["signal"] is None
    assert exit_payload["observed_at"] > 0
    assert exit_payload["elapsed_duration_ms"] > 0


@requires_posix
def test_broken_progress_observer_does_not_cancel_or_truncate_payload(service, monkeypatch, capfd):
    from clawtune_sidecar import tool_signals

    class BrokenProgress(ToolProgress):
        def observe(self, line, elapsed):
            raise ValueError("unsupported progress")

    monkeypatch.setattr(tool_signals, "adapter_for", lambda _command: BrokenProgress())
    monkeypatch.setenv("CLAWTUNE_SIGNAL_INGRESS", service[0] + "/v1/signals")
    command = shlex.join([sys.executable, "-c",
        ("import sys,time; print('start', flush=True); time.sleep(.2); "
         "sys.stdout.buffer.write(b'end\\x00'); sys.exit(7)")])
    monkeypatch.setenv("CLAWTUNE_SIGNAL_UPDATE_TOKEN", "test")
    assert tool_signals.run_tool(command, "broken-progress") == 7
    output = capfd.readouterr()
    assert output.out == "start\nend\x00"
    assert output.err.count("progress disabled") == 1
    assert [row["type"] for row in service[1]] == ["execution.started"]


def test_signal_shutdown_does_not_wait_forever_for_receiver(monkeypatch):
    from clawtune_sidecar import tool_signals

    entered, release = threading.Event(), threading.Event()

    def stalled_receiver(*_args, **_kwargs):
        entered.set()
        release.wait(5)
        raise OSError("receiver stalled")

    monkeypatch.setenv("CLAWTUNE_SIGNAL_INGRESS", "http://receiver/v1/signals")
    monkeypatch.setattr(tool_signals, "_open_request", stalled_receiver)
    monkeypatch.setenv("CLAWTUNE_SIGNAL_UPDATE_TOKEN", "test")
    signals = ToolObservations("stalled", lambda: True)
    try:
        signals.start(ToolProgress(), 123)
        assert entered.wait(2)
        started = time.monotonic()
        signals.close()
        assert time.monotonic() - started < 2
    finally:
        release.set()
        signals.worker.join(timeout=2)


@requires_posix
def test_late_newline_does_not_replay_an_old_eta_as_fresh_progress(service, monkeypatch, capfd):
    from clawtune_sidecar import tool_signals
    from clawtune_sidecar.tool_progress import CurlProgress

    monkeypatch.setenv("CLAWTUNE_SIGNAL_INGRESS", service[0] + "/v1/signals")
    monkeypatch.setattr(tool_signals, "adapter_for", lambda _command: CurlProgress())
    line = "80 1024k 80 819k 0 0 200k 0 0:00:08 0:00:04 0:00:04 200k"
    command = shlex.join([sys.executable, "-c",
        (f"import sys,time; sys.stderr.write({line!r}); sys.stderr.flush(); time.sleep(1.2); "
         "sys.stderr.write('\\n'); sys.stderr.flush(); time.sleep(.2)")])
    monkeypatch.setenv("CLAWTUNE_SIGNAL_UPDATE_TOKEN", "test")
    assert tool_signals.run_tool(command, "delayed-newline") == 0
    assert capfd.readouterr().err == line + "\n"
    assert [row["type"] for row in service[1]] == ["execution.started", "execution.progress"]
