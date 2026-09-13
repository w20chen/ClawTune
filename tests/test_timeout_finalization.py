import json
from types import SimpleNamespace

import pytest

from swe_rebench import host_openclaw as host
from swe_rebench.config import RunnerConfig
from swe_rebench.task_source import TaskDef


@pytest.mark.parametrize("failure", [None, "agent", "sandbox", "abort"])
@pytest.mark.parametrize("outcome", ["timeout", "cancelled"])
def test_timeout_finalizes_only_after_confirmed_cleanup(monkeypatch, tmp_path, failure, outcome):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("runtime:\n  mode: host-openclaw\nbatch:\n  task_timeout_seconds: 0\n")
    config = RunnerConfig.from_yaml(cfg, repo_root=tmp_path)
    trace = tmp_path / "trace"
    calls = []
    for name in ("_write_host_tool_resource_preflight", "_export_testbed_from_image",
                 "_make_sandbox_workspace_writable", "_install_sandbox_runtime",
                 "_write_task_inputs", "_ensure_openclaw_sandbox_image", "_verify_sandbox_launcher",
                 "_verify_sandbox_task_environment", "_configure_openclaw", "_cleanup_runtime_artifacts",
                 "_collect_patch", "_collect_runtime_traces", "_collect_runtime_ebpf_artifacts",
                 "_delete_runtime_scope"):
        monkeypatch.setattr(host, name, lambda *a, **k: None)
    def agent(**kwargs):
        calls.append("agent")
        if failure == "agent":
            raise host.ContainerCleanupError("agent exit unconfirmed")
        if outcome == "cancelled":
            kwargs["stopped_event"].set()
            raise host.TaskCancelled("benchmark cancelled")
        host._write_timeout_record(trace, scope="task", message="task timed out after 1800s", configured_seconds=1800)
        return 124
    def cleanup(*args, **kwargs):
        if "agent" in calls:
            calls.append("sandbox")
            if failure == "sandbox":
                raise host.ContainerCleanupError("sandbox cleanup failed")
    def abort(*args, **kwargs):
        assert calls[-2:] == ["agent", "sandbox"]
        assert kwargs["reason"] == ("cancelled" if outcome == "cancelled" else "task_timeout")
        calls.append("abort")
        if failure == "abort":
            raise RuntimeError("abort failed")
    monkeypatch.setattr(host, "_run_openclaw_agent", agent)
    monkeypatch.setattr(host, "_cleanup_openclaw_sandbox_containers", cleanup)
    monkeypatch.setattr(host, "_abort_runtime", abort)
    monkeypatch.setattr(host, "_drain_runtime", lambda *a, **k: calls.append("drain"))
    kwargs = dict(task=TaskDef(instance_id="org__repo-1", image="cached"), trace_dir=trace,
                  config=config, runtime_assets_dir=tmp_path / "assets", sidecar_port=19090,
                  shared_sidecar_trace_dir=tmp_path / "shared", manage_sidecar=False)
    if failure:
        with pytest.raises(RuntimeError, match="unconfirmed|cleanup failed|abort failed"):
            host.run_host_openclaw_task(**kwargs)
    else:
        result = host.run_host_openclaw_task(**kwargs)
        assert result.exit_code == (-1 if outcome == "cancelled" else 124)
        assert result.error == ("benchmark cancelled" if outcome == "cancelled" else "task timed out after 1800s")
        assert calls == ["agent", "sandbox", "abort", "drain"]
    if failure in ("agent", "sandbox"):
        assert "abort" not in calls


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_terminal_build_failure_returns_case_result_only_after_cleanup(monkeypatch, tmp_path, cleanup_failed):
    from benchmarks import backends, runtime
    from benchmarks.adapters import Task
    def build(*args, **kwargs):
        if cleanup_failed:
            raise RuntimeError("compose down failed")
        raise backends.TerminalCaseBuildFailure("compose up failed after successful cleanup")
    monkeypatch.setattr(backends, "TerminalBackend", build)
    monkeypatch.setattr(runtime, "_required_terminal_preflight", lambda *a: None)
    config = SimpleNamespace(batch=SimpleNamespace(task_timeout_seconds=0),
                             docker=SimpleNamespace(platform="linux/amd64"),
                             runtime=SimpleNamespace(ebpf_required=True), kb_repo="test")
    task = Task("terminal-bench", "bad-build", "system", "terminal", "work")
    trace = tmp_path / "trace"
    trace.mkdir()
    if cleanup_failed:
        with pytest.raises(RuntimeError, match="compose down failed"):
            runtime._execute_bridged(task, config, tmp_path, 19090, trace)
    else:
        result = runtime._execute_bridged(task, config, tmp_path, 19090, trace)
        assert result.exit_code == 1
        assert "compose up failed" in result.error


def test_abort_request_is_authenticated_and_preserves_audit(monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("CLAWTUNE_TOKEN", "owner-secret")
    def request(req, **kwargs):
        assert req.get_header("Authorization") == "Bearer owner-secret"
        assert json.loads(req.data) == {"reason": "task_timeout", "agent_stopped": True, "sandbox_cleaned": True}
        assert req.full_url.endswith("/gateways/swe-rebench/runtimes/task%2Fa/abort")
        return io.BytesIO(b'{"finalized": true, "aborted_execution_ids": ["exec-a"]}')
    monkeypatch.setattr(host.urllib.request, "urlopen", request)
    host._abort_runtime(19090, "task/a", gateway_id="swe-rebench", reason="task_timeout", trace_dir=tmp_path)
    assert json.loads((tmp_path / "runtime-finalization.json").read_text())["aborted_execution_ids"] == ["exec-a"]


@pytest.mark.parametrize("second", ["ok", "active", "timeout"])
def test_drain_transport_retry_does_not_relax_barrier(monkeypatch, second):
    import io
    calls = []
    def request(req, **kwargs):
        calls.append(req.full_url)
        if len(calls) == 1 or second == "timeout":
            raise TimeoutError("slow transport")
        return io.BytesIO(json.dumps({"drained": second == "ok", "active_executions": 1}).encode())
    monkeypatch.setattr(host.urllib.request, "urlopen", request)
    if second == "ok":
        host._drain_runtime(19090, "runtime", flush_kb=False)
    else:
        with pytest.raises(RuntimeError, match="did not drain|failed to drain"):
            host._drain_runtime(19090, "runtime", flush_kb=False)
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize("outcome", ["cancelled", "cleanup_failed", "supervisor_failed"])
def test_agent_stop_confirmation_is_not_inferred_from_exception(monkeypatch, tmp_path, outcome):
    import threading
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    config = RunnerConfig.from_yaml(cfg, repo_root=tmp_path)
    stopped = threading.Event()
    monkeypatch.setattr(host, "_require_executable", lambda _: "unused")
    monkeypatch.setattr(host, "_openclaw_agent_argv", lambda *a, **k: ["unused"])
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **k: SimpleNamespace(_clawtune_supervised=True))
    def wait(*args):
        if outcome == "supervisor_failed":
            return 125
        raise host.TaskCancelled("cancelled")
    def kill(*args):
        if outcome == "cleanup_failed":
            raise host.ContainerCleanupError("cleanup unconfirmed")
    monkeypatch.setattr(host, "wait_process", wait)
    monkeypatch.setattr(host, "_kill_agent_process_and_confirm", kill)
    with pytest.raises(host.TaskCancelled if outcome == "cancelled" else host.ContainerCleanupError):
        host._run_openclaw_agent(trace_dir=tmp_path, openclaw_home=tmp_path,
            workspace=tmp_path, sidecar_port=19090, task=TaskDef("test", "image", "work"),
            config=config, post_sandbox_scope=False, stopped_event=stopped)
    assert stopped.is_set() == (outcome == "cancelled")


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_terminal_constructor_keeps_cleanup_failure_fatal(monkeypatch, tmp_path, cleanup_failed):
    from benchmarks.backends import TerminalBackend, TerminalCaseBuildFailure
    from benchmarks.adapters import load
    task_dir = tmp_path / "input"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text("instruction: work\n")
    (task_dir / "Dockerfile").write_text("FROM cached\n")
    monkeypatch.setattr("benchmarks.compose.compose_argv", lambda **k: ["docker", "compose"])
    calls = []
    def invoke(self, args, **kwargs):
        calls.append(args[0])
        if args[0] == "up":
            raise TerminalCaseBuildFailure("build failed")
        if args[0] == "down" and cleanup_failed:
            raise RuntimeError("cleanup unconfirmed")
        return SimpleNamespace(stdout='{"services": {"client": {"image": "cached"}}}')
    monkeypatch.setattr(TerminalBackend, "_run", invoke)
    with pytest.raises(RuntimeError if cleanup_failed else TerminalCaseBuildFailure,
                       match="cleanup unconfirmed" if cleanup_failed else "build failed"):
        TerminalBackend(load("terminal-bench", task_dir)[0], tmp_path / "run")
    assert calls == ["config", "up", "down"]


@pytest.mark.parametrize("failure", [None, "agent", "sandbox"])
@pytest.mark.parametrize("completed_turn", [False, True])
def test_terminal_cancellation_finalizes_after_agent_and_container_stop(monkeypatch, tmp_path, failure, completed_turn):
    from benchmarks import backends, runtime, tool_bridge
    from benchmarks.adapters import Task
    calls = []
    class Backend:
        tools, system, turns = [], [], (["first", "work"] if completed_turn else ["work"])
        def start_agent(self): return None
        def quiesce(self): calls.append("quiesce")
        def close(self):
            calls.append("close")
            if failure == "sandbox": raise RuntimeError("cleanup unconfirmed")
    class Bridge:
        def __init__(self, backend): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def manifest(self, path): pass
    monkeypatch.setattr(backends, "TerminalBackend", lambda *a, **k: Backend())
    monkeypatch.setattr(tool_bridge, "ToolBridge", Bridge)
    monkeypatch.setattr(runtime, "_required_terminal_preflight", lambda *a: None)
    monkeypatch.setattr(runtime, "run_command", lambda *a, **k: None)
    for name in ("_configure_openclaw", "_collect_runtime_traces"):
        monkeypatch.setattr(host, name, lambda *a, **k: None)
    monkeypatch.setattr(host, "_openclaw_env", lambda *a: {})
    monkeypatch.setattr(host, "_require_executable", lambda *a: "unused")
    agent_calls = []
    def agent(**kwargs):
        agent_calls.append(1)
        if completed_turn and len(agent_calls) == 1:
            return 0
        if failure == "agent": raise host.ContainerCleanupError("agent unconfirmed")
        kwargs["stopped_event"].set()
        raise host.TaskCancelled("cancelled")
    def abort(*args, **kwargs):
        assert calls == ["quiesce", "close"]
        assert kwargs["reason"] == "cancelled"
        calls.append("abort")
    monkeypatch.setattr(host, "_run_openclaw_agent", agent)
    monkeypatch.setattr(host, "_abort_runtime", abort)
    monkeypatch.setattr(host, "_drain_runtime", lambda *a, **k: calls.append("drain"))
    cfg = tmp_path / "config.yaml"; cfg.write_text("")
    config = RunnerConfig.from_yaml(cfg, repo_root=tmp_path)
    config.kb_repo = "test"
    trace = tmp_path / "trace"; trace.mkdir()
    with pytest.raises(RuntimeError, match="cancelled|unconfirmed"):
        runtime._execute_bridged(Task("terminal-bench", "test", "system", "terminal", "work"),
                                 config, tmp_path, 19090, trace)
    assert ("abort" in calls) == (failure is None)
