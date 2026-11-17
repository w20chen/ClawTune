"""Real processes and HTTP; progress is produced by curl/wget/pytest themselves.

The HTTP fixture stands in for sidecar registration and the GPU receiver only.
It never supplies tool progress or a remaining duration.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clawtune_sidecar.tool_progress import RemainingTime
from clawtune_sidecar.tool_signals import CpuToolSignals


@pytest.fixture
def service():
    rows, commands = [], {}
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
            response = {"stored": True}
            if self.path == "/v2/executions/claim":
                response = dict(command=commands[payload["execution_id"]], update_token="test",
                                profiling={"enable_cgroup": False, "enable_affinity": False,
                                           "enable_numa": False})
            elif self.path == "/cpu-events":
                rows.append({**payload, "received_at": time.time()})
            self.send_response(503 if self.path == "/failure" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", rows, commands, content
    server.shutdown()
    server.server_close()
    thread.join()


def run_launcher(service, command, mode, tmp_path, *, prediction="999999", endpoint="/cpu-events"):
    url, rows, commands, _ = service
    commands["test-exec"] = command
    env = dict(os.environ, CLAWTUNE_GPU_SIGNAL_ENDPOINT=url + endpoint,
               CLAWTUNE_PREDICTED_TOOL_DURATION_MS=prediction, CLAWTUNE_LAUNCH_MODE=mode,
               CLAWTUNE_EXECUTION_ID="test-exec", CLAWTUNE_TOOL_CALL_ID="test-call",
               CLAWTUNE_SESSION_ID="test-session", CLAWTUNE_CGROUP_REQUIRED="0",
               CLAWTUNE_PAYLOAD_COMMAND=command)
    # Reproduce a sandbox whose launcher-only PYTHONPATH is stripped for payloads.
    src = str(Path(__file__).resolve().parents[1] / "src")
    env.update(PYTHONPATH=src, CLAWTUNE_LAUNCHER_PYTHONPATH=src)
    result = subprocess.run([sys.executable, "-m", "clawtune_sidecar.launcher", "run",
                             "--execution-id", "test-exec", "--token", "test",
                             "--endpoint", url], env=env, capture_output=True, timeout=25)
    finished = time.time()
    (tmp_path / "stdout.bin").write_bytes(result.stdout)
    (tmp_path / "stderr.txt").write_bytes(result.stderr)
    (tmp_path / "signals.json").write_text(json.dumps({"command": command,
        "launcher_mode": mode, "finished_at": finished, "events": rows}, indent=2))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result, finished


@pytest.mark.parametrize("mode", ["subprocess", "fork-exec"])
@pytest.mark.parametrize("tool", ["curl", "wget", "pytest"])
def test_real_tool_progress_emits_before_exit_without_progress_files(service, tmp_path, mode, tool):
    url, rows, _, content = service
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


def test_old_progress_file_and_elapsed_prediction_cannot_trigger_signal(service, tmp_path, monkeypatch):
    progress = tmp_path / "obsolete-progress"
    progress.write_text("1.5")
    monkeypatch.setenv("CLAWTUNE_TOOL_PROGRESS_PATH", str(progress))
    command = shlex.join([sys.executable, "-c", "import time; time.sleep(.3); print('done')"])
    result, _ = run_launcher(service, command, "subprocess", tmp_path, prediction="200")
    assert result.stdout == b"done\n"
    assert [r["event"] for r in service[1]] == ["tool_start"]


def test_ineligible_call_runs_without_either_signal(service, tmp_path):
    result, _ = run_launcher(service, "printf done", "subprocess", tmp_path, prediction="")
    assert result.stdout == b"done"
    assert service[1] == []


def test_signal_http_failure_preserves_tool_execution(service, tmp_path):
    result, _ = run_launcher(service, "printf done", "subprocess", tmp_path, endpoint="/failure")
    assert result.stdout == b"done"
    assert b"signal tool_start failed" in result.stderr


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
