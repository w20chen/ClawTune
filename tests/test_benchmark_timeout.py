"""One task deadline across setup, turns and backend calls; cleanup stays mandatory."""
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from benchmarks import backends, runtime, tool_bridge
from benchmarks.adapters import Task
from swe_rebench import host_openclaw as host
from swe_rebench.config import BatchConfig, RunnerConfig, _parse_yaml_fallback


@pytest.mark.parametrize("value", [-1, True, 1.5, "1200", float("inf"), None])
def test_task_timeout_rejects_invalid_yaml(value):
    with pytest.raises(ValueError, match="task_timeout_seconds"):
        BatchConfig.from_dict({"task_timeout_seconds": value})


def test_legacy_agent_timeout_warns_and_does_not_override_task_budget():
    with pytest.warns(UserWarning, match="obsolete"):
        config = BatchConfig.from_dict({"agent_timeout_seconds": 90})
    assert config.task_timeout_seconds == 1200
    with pytest.warns(UserWarning, match="obsolete"):
        config = BatchConfig.from_dict({"agent_timeout_seconds": 90, "task_timeout_seconds": 600})
    assert config.task_timeout_seconds == 600


def test_yaml_fallback_keeps_task_timeout_numeric(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("batch:\n  task_timeout_seconds: 1200\n", encoding="utf-8")
    raw = _parse_yaml_fallback(path)
    assert BatchConfig.from_dict(raw["batch"]).task_timeout_seconds == 1200


def test_bfcl_calls_share_deadline_without_300_second_timer(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(backends.time, "monotonic", lambda: clock[0])
    backend = backends.BFCLBackend.__new__(backends.BFCLBackend)
    backend.deadline = 1300
    backend._cancelled = threading.Event()
    stopped = []
    backend._stop = lambda: stopped.append(True)
    backend._worker = SimpleNamespace(is_alive=lambda: True)

    def poll(_timeout):
        clock[0] += 301
        return True

    backend._connection = SimpleNamespace(poll=poll, recv=lambda: (True, "ok"))
    assert backend._receive() == "ok"
    assert backend._receive() == "ok"
    assert not stopped
    clock[0] = 1300
    with pytest.raises(host.TaskDeadlineExceeded):
        backend._receive()
    assert stopped == [True]


def test_terminal_payload_has_remaining_task_budget_not_300_seconds(monkeypatch):
    backend = backends.TerminalBackend.__new__(backends.TerminalBackend)
    backend.deadline = 1300
    backend.container = "owned"
    monkeypatch.setattr(backends.time, "monotonic", lambda: 100)
    process = SimpleNamespace(args=["docker", "exec"], returncode=0)
    monkeypatch.setattr(backends.subprocess, "Popen", lambda *a, **kw: process)
    budgets = []
    backend._communicate = lambda p, *, timeout: (budgets.append(timeout) or "", "")
    backend._plain_exec("long command")
    backend.deadline = None
    backend._plain_exec("unlimited diagnostic")
    assert budgets == [1200, None]


@pytest.fixture
def bridged(monkeypatch, tmp_path):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(path, repo_root=tmp_path)
    config.kb_repo = "test"
    trace = tmp_path / "trace"
    trace.mkdir()

    class Backend:
        tools, system, turns = [], [], ["first", "second"]

        def __init__(self, *args, deadline, **kwargs):
            calls.append(("backend", deadline))

        def quiesce(self):
            calls.append("quiesce")

        def close(self):
            calls.append("close")

    class Bridge:
        def __init__(self, backend): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def manifest(self, path): pass

    monkeypatch.setattr(backends, "BFCLBackend", Backend)
    monkeypatch.setattr(backends, "TerminalBackend", Backend)
    monkeypatch.setattr(tool_bridge, "ToolBridge", Bridge)
    monkeypatch.setattr(runtime, "_required_terminal_preflight", lambda *args: None)
    monkeypatch.setattr(runtime, "run_command", lambda *a, **k: None)
    for name in ("_configure_openclaw", "_collect_runtime_traces"):
        monkeypatch.setattr(host, name, lambda *a, **kw: None)
    monkeypatch.setattr(host, "_openclaw_env", lambda *a: {})
    monkeypatch.setattr(host, "_require_executable", lambda *a: "unused")
    monkeypatch.setattr(host, "_drain_runtime", lambda *a, **kw: calls.append("drain"))
    monkeypatch.setattr(host, "_abort_runtime", lambda *a, **kw: calls.append(("abort", kw["reason"])))

    def run(kind="functions"):
        task = Task("bfcl" if kind == "functions" else "terminal-bench", "test", "g", kind, "work")
        return runtime._execute_bridged(task, config, tmp_path, 19090, trace)

    return SimpleNamespace(clock=clock, calls=calls, run=run, trace=trace, Backend=Backend)


@pytest.mark.parametrize("kind", ["functions", "terminal"])
def test_setup_and_all_turns_use_one_deadline(monkeypatch, bridged, kind):
    def setup(**kwargs):
        assert kwargs["deadline"] == 1300
        bridged.clock[0] += 200

    deadlines = []

    def agent(**kwargs):
        deadlines.append(kwargs["task_deadline"])
        kwargs["stopped_event"].set()
        bridged.clock[0] += 100
        return 0

    monkeypatch.setattr(host, "_configure_openclaw", setup)
    monkeypatch.setattr(host, "_run_openclaw_agent", agent)
    result = bridged.run(kind)
    assert result.exit_code == 0
    assert result.duration_seconds == 400
    assert deadlines == [1300, 1300]
    assert bridged.calls[0] == ("backend", 1300)


@pytest.mark.parametrize("phase", ["constructor", "setup", "agent"])
def test_timeout_is_case_result_after_cleanup(monkeypatch, bridged, phase):
    def expire(*args, **kwargs):
        bridged.clock[0] = 1301
        raise host.TaskDeadlineExceeded(f"task timed out during {phase}")

    if phase == "constructor":
        monkeypatch.setattr(backends, "TerminalBackend", expire)
    elif phase == "setup":
        monkeypatch.setattr(host, "_configure_openclaw", expire)
    else:
        def agent(**kwargs):
            kwargs["stopped_event"].set()
            expire()
        monkeypatch.setattr(host, "_run_openclaw_agent", agent)
    result = bridged.run("terminal")
    assert result.exit_code == 124
    assert "timed out" in result.error
    record = json.loads((bridged.trace / "task-timeout.json").read_text())
    assert record["scope"] == "task"
    assert record["configured_seconds"] == 1200
    if phase != "constructor":
        assert "close" in bridged.calls and "drain" in bridged.calls
    if phase == "agent":
        assert bridged.calls.index("close") < bridged.calls.index(("abort", "task_timeout"))


def test_agent_timeout_record_is_not_overwritten_by_result_collection(monkeypatch, bridged):
    """The first timeout record survives the post-agent deadline re-check."""
    def agent(**kwargs):
        kwargs["stopped_event"].set()
        (bridged.trace / "task-timeout.json").write_text(json.dumps({
            "scope": "task", "message": "task timed out after 1200s", "configured_seconds": 1200,
        }), encoding="utf-8")
        bridged.clock[0] = 1301  # the deadline elapsed while the agent ran
        return 124

    monkeypatch.setattr(host, "_run_openclaw_agent", agent)
    result = bridged.run("terminal")
    assert result.exit_code == 124
    assert result.error == "task timed out after 1200s"
    record = json.loads((bridged.trace / "task-timeout.json").read_text())
    assert record["message"] == "task timed out after 1200s"


def test_cleanup_failure_remains_fatal_after_timeout(monkeypatch, bridged):
    def expire(**kwargs):
        raise host.TaskDeadlineExceeded("expired")

    def cleanup(self):
        raise host.ContainerCleanupError("cleanup unconfirmed")

    monkeypatch.setattr(host, "_configure_openclaw", expire)
    monkeypatch.setattr(bridged.Backend, "close", cleanup)
    with pytest.raises(host.ContainerCleanupError, match="unconfirmed"):
        bridged.run()


def test_short_setup_probe_timeout_is_not_task_timeout(monkeypatch, bridged):
    def expire(**kwargs):
        raise subprocess.TimeoutExpired("probe", 30)

    monkeypatch.setattr(host, "_configure_openclaw", expire)
    result = bridged.run()
    assert result.exit_code == 1
    assert not (bridged.trace / "task-timeout.json").exists()


def test_syntax_probe_respects_remaining_task_budget(monkeypatch):
    clock = [100]
    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])

    def probe(*args, **kwargs):
        assert kwargs["timeout"] == 5
        clock[0] = 105
        raise subprocess.TimeoutExpired("help", 5)

    monkeypatch.setattr(host, "run_command", probe)
    with pytest.raises(host.TaskDeadlineExceeded, match="syntax probe"):
        host._openclaw_uses_agent_flag("openclaw", deadline=105)


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_terminal_build_deadline_waits_for_confirmed_cleanup(monkeypatch, tmp_path, cleanup_failed):
    from benchmarks.adapters import load

    clock = [100]
    monkeypatch.setattr(backends.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("benchmarks.compose.compose_argv", lambda **kw: ["docker", "compose"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "task.yaml").write_text("instruction: work\nmax_agent_timeout_sec: 1\n")
    (source / "Dockerfile").write_text("FROM cached\n")
    calls = []

    def invoke(self, args, *, timeout, cleanup=False):
        calls.append(args[0])
        if args[0] == "up":
            assert timeout is None  # No independent build budget.
            assert self.deadline == 1300
            clock[0] = 1301
            raise host.TaskDeadlineExceeded("task expired during build")
        if args[0] == "down" and cleanup_failed:
            raise subprocess.TimeoutExpired("compose down", 60)
        return SimpleNamespace(stdout='{"services": {"client": {"image": "cached"}}}')

    monkeypatch.setattr(backends.TerminalBackend, "_run", invoke)
    expected = host.ContainerCleanupError if cleanup_failed else host.TaskDeadlineExceeded
    with pytest.raises(expected):
        backends.TerminalBackend(load("terminal-bench", source)[0], tmp_path / "run", deadline=1300)
    assert calls == ["config", "up", "down"]
