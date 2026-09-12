from __future__ import annotations

import io
import os
import subprocess
import sys
import urllib.error
from types import SimpleNamespace

import pytest

from benchmarks.adapters import Task
from benchmarks.exec_control import (
    GATE_SCRIPT,
    ExecutionStartRejected,
    parse_gate_identity,
    SidecarExecutions,
    SidecarUnavailable,
)
from benchmarks.runner import _benchmark_requires_ebpf


@pytest.mark.parametrize(
    "line,expected",
    [
        ("CLAWTUNE-GATE 42 4026532 100\n", (42, 4026532, 100)),
        ("  CLAWTUNE-GATE 1 2 3  ", (1, 2, 3)),
    ],
)
def test_parse_gate_identity_accepts_valid_lines(line, expected):
    assert parse_gate_identity(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "CLAWTUNE-GATE 0 1 2\n",
        "CLAWTUNE-GATE 1 2\n",
        "CLAWTUNE-GATE a b c\n",
        "CLAWTUNE-GATE-ERROR identity_unavailable",
    ],
)
def test_parse_gate_identity_rejects_malformed_lines(line):
    assert parse_gate_identity(line) is None


def test_gate_script_releases_only_after_the_host_token():
    assert '"CLAWTUNE-GATE" "$$" "$namespace" "$starttime"' in GATE_SCRIPT
    assert "IFS= read -r release || exit 125" in GATE_SCRIPT
    assert '[ "$release" = "go" ] || exit 125' in GATE_SCRIPT
    assert 'exec "$@"' in GATE_SCRIPT
    assert "exit 126" in GATE_SCRIPT


@pytest.mark.parametrize(
    "kind,required",
    [
        ("repository", True),
        ("terminal", True),
        ("research", False),
        ("functions", False),
    ],
)
def test_benchmark_requires_ebpf_for_lifecycle_backed_kinds(kind, required):
    task = Task("terminal-bench", "task-1", "group", kind, "prompt")

    assert _benchmark_requires_ebpf(task) is required


def test_required_terminal_preflight_runs_only_for_required_terminal_tasks(
    monkeypatch, tmp_path
):
    from benchmarks import runtime
    from swe_rebench import host_openclaw

    calls = []
    monkeypatch.setattr(
        host_openclaw,
        "_write_host_tool_resource_preflight",
        lambda trace, config, deadline=None: calls.append((trace, config, deadline)),
    )
    config = SimpleNamespace(runtime=SimpleNamespace(ebpf_required=True))
    terminal = SimpleNamespace(kind="terminal")

    runtime._required_terminal_preflight(config, terminal, tmp_path, 12.0)
    assert calls == [(tmp_path, config, 12.0)]

    calls.clear()
    config.runtime.ebpf_required = False
    runtime._required_terminal_preflight(config, terminal, tmp_path, 12.0)
    assert calls == []

    config.runtime.ebpf_required = True
    runtime._required_terminal_preflight(
        config, SimpleNamespace(kind="functions"), tmp_path, 12.0
    )
    assert calls == []


class _TrackingStdin(io.StringIO):
    def __init__(self):
        super().__init__()
        self.written = ""

    def write(self, value):
        self.written += value
        return super().write(value)


class _FakeProcess:
    def __init__(self, identity_line="CLAWTUNE-GATE 42 4026532 100\n", output="payload out"):
        self.stdin = _TrackingStdin()
        self.stdout = io.StringIO(identity_line)
        self.stderr = io.StringIO("")
        self.output = output
        self.returncode = None
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.stdin.write(input)
            self.stdin.close()
        self.returncode = 0
        return self.output, self.stderr.getvalue()

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakeSidecar:
    def __init__(self, reject_start=False):
        self.calls = []
        self.reject_start = reject_start

    def register(self, **kwargs):
        self.calls.append(("register", kwargs))
        return "one-time-token"

    def claim(self, execution_id, token, *, launcher_pid):
        self.calls.append(("claim", {"execution_id": execution_id, "token": token}))
        return "update-token"

    def started(self, execution_id, update_token, **kwargs):
        self.calls.append(("started", {"execution_id": execution_id, **kwargs}))
        if self.reject_start:
            raise ExecutionStartRejected("tool_resource_ebpf_start_failed")

    def exited(self, execution_id, update_token, *, exit_code, term_signal=None):
        self.calls.append(
            ("exited", {"execution_id": execution_id, "exit_code": exit_code})
        )

    def store_container_scope(self, runtime_id, scope, *, gateway_id=None):
        self.calls.append(
            ("scope", {"runtime_id": runtime_id, "scope": scope, "gateway_id": gateway_id})
        )

    def delete_container_scope(self, runtime_id, *, gateway_id=None):
        self.calls.append(("delete-scope", {"runtime_id": runtime_id}))


def _terminal_backend(tmp_path, sidecar):
    from benchmarks import backends

    backend = backends.TerminalBackend.__new__(backends.TerminalBackend)
    backend.container = "client-container"
    backend.timeout = 300
    backend.deadline = None
    backend.log_dir = tmp_path
    backend.sidecar = sidecar
    backend.gate_available = True
    backend.telemetry_required = False
    backend.gate_install_error = None
    backend.runtime_id = "runtime-1"
    backend.gateway_id = "swe-rebench"
    backend.repo = "terminal-bench:test"
    return backend


def test_terminal_backend_gated_call_drives_full_execution_lifecycle(monkeypatch, tmp_path):
    sidecar = _FakeSidecar()
    backend = _terminal_backend(tmp_path, sidecar)
    process = _FakeProcess()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)

    result = backend.call("terminal_exec", {"command": "echo hi"}, call_id="call-1")

    assert [name for name, _ in sidecar.calls] == ["register", "claim", "started", "exited"]
    registered = sidecar.calls[0][1]
    assert registered["tool_call_id"] == "call-1"
    assert registered["runtime_id"] == "runtime-1"
    assert registered["repo"] == "terminal-bench:test"
    assert registered["command"] == "echo hi"
    started = sidecar.calls[2][1]
    assert started["container_pid"] == 42
    assert started["namespace_inode"] == 4026532
    assert started["starttime_ticks"] == 100
    assert started["container_id"] == "client-container"
    assert sidecar.calls[3][1]["exit_code"] == 0
    # The gate was released after /started and the payload ran with the
    # original argv; stdin is closed so the payload cannot block on it.
    assert process.stdin.written == "go\n"
    assert process.stdin.closed is True
    assert result == {"exit_code": 0, "stdout": "payload out", "stderr": ""}


def test_terminal_backend_degrades_when_gate_identity_is_unavailable(monkeypatch, tmp_path):
    sidecar = _FakeSidecar()
    backend = _terminal_backend(tmp_path, sidecar)
    process = _FakeProcess(identity_line="")
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)
    monkeypatch.setattr(
        backend,
        "_plain_exec",
        lambda command: SimpleNamespace(returncode=7, stdout="plain", stderr="err"),
    )

    result = backend.call("terminal_exec", {"command": "echo hi"}, call_id="call-1")

    assert result == {"exit_code": 7, "stdout": "plain", "stderr": "err"}
    assert backend.gate_available is False
    assert process.stdin.closed is True
    assert sidecar.calls == []
    assert "terminal-gate.log" in [path.name for path in tmp_path.iterdir()]


def test_terminal_backend_fails_closed_when_the_start_is_rejected(monkeypatch, tmp_path):
    sidecar = _FakeSidecar(reject_start=True)
    backend = _terminal_backend(tmp_path, sidecar)
    process = _FakeProcess()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)

    with pytest.raises(ExecutionStartRejected):
        backend.call("terminal_exec", {"command": "echo hi"}, call_id="call-1")

    assert process.stdin.closed is True
    assert process.stdin.written == ""
    assert [name for name, _ in sidecar.calls] == ["register", "claim", "started", "exited"]


def test_terminal_backend_registers_the_client_container_scope(monkeypatch, tmp_path):
    sidecar = _FakeSidecar()
    backend = _terminal_backend(tmp_path, sidecar)
    scope = {"kind": "cgroup-v2", "container_id": "client-container", "cgroup_path": "/c"}
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._docker_container_scope",
        lambda docker, container_id: scope,
    )

    backend._register_container_scope()

    assert sidecar.calls == [
        ("scope", {"runtime_id": "runtime-1", "scope": scope, "gateway_id": "swe-rebench"})
    ]


def _real_gate():
    return subprocess.Popen(
        [sys.executable, "-u", "-c",
         'import sys; print("CLAWTUNE-GATE 42 4026532 100", flush=True); '
         'token = sys.stdin.readline(); '
         'sys.exit(125) if token != "go\\n" else None; '
         'print("payload out"); print("payload err", file=sys.stderr)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_real_subprocess_releases_and_preserves_result(monkeypatch, tmp_path):
    sidecar = _FakeSidecar()
    backend = _terminal_backend(tmp_path, sidecar)
    process = _real_gate()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)
    try:
        assert backend.call("terminal_exec", {"command": "payload"}, call_id="real") == {
            "exit_code": 0, "stdout": "payload out\n", "stderr": "payload err\n",
        }
        assert [name for name, _ in sidecar.calls] == ["register", "claim", "started", "exited"]
    finally:
        backend._terminate(process)


@pytest.mark.parametrize("phase", ["register", "claim", "started"])
def test_real_gate_aborts_on_http_failure(monkeypatch, tmp_path, phase):
    sidecar = _FakeSidecar()
    client = SidecarExecutions(1)
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("http://localhost", 502, "bad gateway", {}, io.BytesIO(b"failed"))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr(sidecar, phase, getattr(client, phase))
    backend = _terminal_backend(tmp_path, sidecar)
    process = _real_gate()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)
    plain_calls = []
    def plain(command):
        assert process.poll() == 125  # remote gate consumed EOF, no payload
        plain_calls.append(command)
        return SimpleNamespace(returncode=0, stdout="fallback", stderr="")
    monkeypatch.setattr(backend, "_plain_exec", plain)
    try:
        assert backend.call("terminal_exec", {"command": "payload"}, call_id="real")["stdout"] == "fallback"
        assert plain_calls == ["payload"]
        if phase == "started":
            assert sidecar.calls[-1][0] == "exited"
    finally:
        backend._terminate(process)


def test_http_exit_failure_does_not_turn_success_into_retry(monkeypatch, tmp_path):
    sidecar = _FakeSidecar()
    def fail(*args, **kwargs):
        raise SidecarUnavailable("HTTP 503")
    monkeypatch.setattr(sidecar, "exited", fail)
    backend = _terminal_backend(tmp_path, sidecar)
    process = _real_gate()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)
    try:
        assert backend.call("terminal_exec", {"command": "payload"}, call_id="real")["exit_code"] == 0
        assert "exit report failed" in (tmp_path / "terminal-gate.log").read_text()
    finally:
        backend._terminate(process)


@pytest.mark.parametrize("phase,status", [("register", 403), ("claim", 409), ("claim", 503), ("started", 503)])
def test_protocol_rejection_never_runs_payload(monkeypatch, tmp_path, phase, status):
    sidecar = _FakeSidecar()
    client = SidecarExecutions(1)
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("http://localhost", status, "rejected", {}, io.BytesIO(b"rejected"))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr(sidecar, phase, getattr(client, phase))
    backend = _terminal_backend(tmp_path, sidecar)
    process = _real_gate()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)
    try:
        with pytest.raises(ExecutionStartRejected):
            backend.call("terminal_exec", {"command": "payload"}, call_id="real")
        assert process.poll() == 125
    finally:
        backend._terminate(process)


def test_required_telemetry_never_degrades_to_unobserved_execution(monkeypatch, tmp_path):
    sidecar = _FakeSidecar()
    def unavailable(**kwargs):
        raise SidecarUnavailable("connection refused")
    monkeypatch.setattr(sidecar, "register", unavailable)
    backend = _terminal_backend(tmp_path, sidecar)
    backend.telemetry_required = True
    process = _real_gate()
    monkeypatch.setattr(backend, "_start_gated_process", lambda command: process)
    try:
        with pytest.raises(ExecutionStartRejected, match="required terminal telemetry"):
            backend.call("terminal_exec", {"command": "payload"}, call_id="real")
        assert process.poll() == 125
        backend.gate_available = False
        with pytest.raises(ExecutionStartRejected, match="gate is unavailable"):
            backend.call("terminal_exec", {"command": "payload"}, call_id="next")
    finally:
        backend._terminate(process)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs and POSIX shell")
@pytest.mark.parametrize("slow_kill", [False, True])
def test_abort_script_verifies_identity_and_stops_descendants(tmp_path, slow_kill):
    from pathlib import Path
    from benchmarks.exec_control import ABORT_SCRIPT
    # Force a scheduling gap after killing a child so the parent's wait can
    # resume if the abort implementation has not frozen it first.
    script = ABORT_SCRIPT.replace('kill -KILL "$target" 2>/dev/null || :',
        'kill -KILL "$target" 2>/dev/null || :\nsleep 0.05') if slow_kill else ABORT_SCRIPT
    child_file = tmp_path / "child"
    resumed_file = tmp_path / "resumed"
    process = subprocess.Popen(["/bin/sh", "-c", 'sleep 60 & echo $! > "$1"; wait; echo resumed > "$2"', "sh", str(child_file), str(resumed_file)])
    try:
        import time
        deadline = time.monotonic() + 5
        while (not child_file.exists() or not child_file.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.01)
        child = int(child_file.read_text().strip())
        ticks = int(Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        namespace = int(os.readlink(f"/proc/{process.pid}/ns/pid")[5:-1])
        def abort(starttime):
            subprocess.run(["/bin/sh", "-c", script, "abort", str(process.pid), str(namespace), str(starttime)], check=True, timeout=5)
        abort(ticks + 1)
        assert process.poll() is None
        assert Path(f"/proc/{child}").exists()
        abort(ticks)
        assert process.wait(timeout=5) == -9
        assert not resumed_file.exists()
        stat = Path(f"/proc/{child}/stat")
        assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs and POSIX shell")
def test_posix_gate_reports_its_own_identity_before_payload(tmp_path):
    script = tmp_path / "gate.sh"
    script.write_text(GATE_SCRIPT, encoding="utf-8", newline="\n")
    # Delay cut so that the helper's birth cannot accidentally share a clock
    # tick with the shell. The old /proc/self implementation fails reliably.
    helper = tmp_path / "cut"
    helper.write_text('#!/bin/sh\nsleep 0.05\nexec /usr/bin/cut "$@"\n', encoding="utf-8")
    helper.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    process = subprocess.Popen(
        ["/bin/sh", str(script), "/bin/sh", "-c", "printf payload"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        identity = parse_gate_identity(process.stdout.readline())
        from pathlib import Path
        ticks = int(Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        namespace = int(os.readlink(f"/proc/{process.pid}/ns/pid")[5:-1])
        assert identity == (process.pid, namespace, ticks)
        assert process.communicate(input="go\n", timeout=5) == ("payload", "")
        assert process.returncode == 0
    finally:
        from benchmarks.backends import TerminalBackend
        TerminalBackend._terminate(process)
