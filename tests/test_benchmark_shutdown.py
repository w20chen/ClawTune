import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.error

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper")
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("parallelism", [1, 4])
def test_supervisor_reaps_detached_descendants(tmp_path, cancelled, parallelism):
    from swe_rebench import process_supervisor
    script = tmp_path / "agent.py"
    script.write_text(
        "import subprocess,sys,time,pathlib\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))\n"
        + ("time.sleep(30)\n" if cancelled else "")
    )
    def run(index):
        child_file = tmp_path / f"child-{index}"
        process = subprocess.Popen([sys.executable, process_supervisor.__file__, "--", sys.executable, str(script), str(child_file)])
        try:
            deadline = time.monotonic() + 5
            while not child_file.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert child_file.exists()
            child = int(child_file.read_text())
            if cancelled:
                process.terminate()
            assert process.wait(timeout=8) == (143 if cancelled else 0)
            assert not Path(f"/proc/{child}").exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    with ThreadPoolExecutor(parallelism) as pool:
        list(pool.map(run, range(parallelism)))


class BlockingBFCL:
    tools = []
    turns = ["test"]
    system = []
    def __init__(self, task, folder):
        self.folder = folder
        self.count = 0
    def call(self, name, arguments, **kwargs):
        self.count += 1
        if name == "spawn":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            return child.pid
        if name == "slow":
            (self.folder / "entered").write_text("1")
            time.sleep(30)
        return self.count
    def close(self):
        (self.folder / "flushed").write_text(str(self.count))


@pytest.mark.parametrize("parallelism", [1, 4])
def test_bridge_cancels_bfcl_worker_before_returning(tmp_path, parallelism):
    from benchmarks.backends import BFCLBackend
    from benchmarks.tool_bridge import ToolBridge
    def run(index):
        folder = tmp_path / str(index)
        folder.mkdir()
        backend = BFCLBackend(None, folder, _factory=BlockingBFCL)
        assert backend.call("increment", {}) == 1
        assert backend.call("increment", {}) == 2
        bridge = ToolBridge(backend)
        bridge.__enter__()
        def request():
            req = urllib.request.Request(f"http://127.0.0.1:{bridge.server.server_port}/call",
                data=json.dumps({"name":"slow", "arguments":{}, "call_id":"slow"}).encode(),
                headers={"Authorization":"Bearer "+bridge.token})
            try:
                urllib.request.urlopen(req, timeout=15).close()
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(request)
            try:
                deadline = time.monotonic() + 5
                while not (folder / "entered").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert (folder / "entered").exists()
                start = time.monotonic()
                bridge.__exit__(None, None, None)
                assert time.monotonic() - start < 10
                assert not backend._worker.is_alive()
                future.result(timeout=3)
            finally:
                backend.close()
    with ThreadPoolExecutor(parallelism) as pool:
        list(pool.map(run, range(parallelism)))


def test_bfcl_normal_close_flushes_state(tmp_path):
    from benchmarks.backends import BFCLBackend
    backend = BFCLBackend(None, tmp_path, _factory=BlockingBFCL)
    backend.call("increment", {})
    backend.cancel()
    backend.close()
    assert (tmp_path / "flushed").read_text() == "1"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper")
def test_bfcl_normal_close_reaps_detached_child(tmp_path):
    from benchmarks.backends import BFCLBackend
    backend = BFCLBackend(None, tmp_path, _factory=BlockingBFCL)
    child = backend.call("spawn", {})
    backend.close()
    assert backend._worker.exitcode == 0
    assert not Path(f"/proc/{child}").exists()


@pytest.mark.parametrize("failures", [1, 3])
def test_required_exit_report_retries_and_fails_closed(monkeypatch, failures):
    from benchmarks.backends import TerminalBackend
    from benchmarks.exec_control import SidecarUnavailable, ExecutionStartRejected
    from unittest.mock import Mock
    backend = TerminalBackend.__new__(TerminalBackend)
    backend.telemetry_required = True
    backend._gate_log = lambda message: None
    backend.sidecar = Mock()
    backend.sidecar.exited.side_effect = [SidecarUnavailable("timeout")] * failures + [None]
    monkeypatch.setattr("benchmarks.backends.time.sleep", lambda seconds: None)
    if failures == 3:
        with pytest.raises(ExecutionStartRejected):
            backend._report_exit("exec", "token", exit_code=0, term_signal=None)
    else:
        backend._report_exit("exec", "token", exit_code=0, term_signal=None)
    assert backend.sidecar.exited.call_count == min(failures + 1, 3)
