from types import SimpleNamespace

import pytest

from benchmarks import compose
from benchmarks.runner import _result_summary
from swe_rebench.docker import local_image_available, pull_image


def test_task_tool_contracts_are_isolated_and_preserve_source(tmp_path):
    import json
    from swe_rebench.host_openclaw import _stage_benchmark_tool_contracts
    source = tmp_path / "plugin"
    source.mkdir()
    original = {"id": "clawtune", "contracts": {"other": ["preserved"]}}
    (source / "openclaw.plugin.json").write_text(json.dumps(original))
    for name in ("terminal_exec", "bfcl_function"):
        trace = tmp_path / name
        trace.mkdir()
        bridge = trace / "bridge.json"
        bridge.write_text(json.dumps({"schema": "clawtune.tool-bridge.v1",
            "endpoint": "http://127.0.0.1:1234/call", "token": "x" * 32,
            "tools": [{"name": name, "parameters": {"type": "object"}}]}))
        staged = _stage_benchmark_tool_contracts(plugin_dir=source, trace_dir=trace, tools_manifest=bridge)
        manifest = json.loads((staged / "openclaw.plugin.json").read_text())
        assert manifest["contracts"] == {"other": ["preserved"], "tools": [name]}
    assert json.loads((source / "openclaw.plugin.json").read_text()) == original


def test_host_openclaw_uses_run_benchmark_gateway_identity(tmp_path):
    from swe_rebench.host_openclaw import _gateway_id, _openclaw_env

    config = SimpleNamespace(
        benchmark_gateway_id="terminal-bench",
        docker=SimpleNamespace(cgroup_required=False, platform=""),
        llm=SimpleNamespace(
            upstream_base_url="http://example.invalid",
            api_key="test-key",
            model="test-model",
        ),
        runtime=SimpleNamespace(ebpf_required=False, kb_frozen=False),
    )
    assert _gateway_id(config) == "terminal-bench"
    env = _openclaw_env(tmp_path / "home", 8765, config, tmp_path / "workspace")
    assert env["CLAWTUNE_GATEWAY_ID"] == "terminal-bench"


def test_terminal_compose_failure_preserves_live_diagnostics(monkeypatch, tmp_path):
    import subprocess
    from benchmarks import backends
    backend = backends.TerminalBackend.__new__(backends.TerminalBackend)
    backend.root, backend.env, backend.command, backend.deadline = tmp_path, {}, ["compose"], None
    backend.log_dir = tmp_path
    def fail(argv, **kwargs):
        kwargs["stdout"].write("compose build requires buildx 0.17.0 or later\n")
        kwargs["stdout"].flush()
        assert "requires buildx" in (tmp_path / "compose-up.log").read_text()
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(backends, "run_command", fail)
    with pytest.raises(RuntimeError, match="compose-up.log"):
        backend._run(["up", "-d", "--build"], timeout=30)
    assert "requires buildx" in (tmp_path / "compose-up.log").read_text()


def test_compose_prefers_working_native_plugin(monkeypatch):
    monkeypatch.setattr(compose.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(compose, "_invoking_home", lambda: pytest.fail("unneeded user fallback"))
    assert compose.compose_argv() == ["docker", "compose"]


def test_compose_falls_back_to_invoking_users_plugin(monkeypatch, tmp_path):
    plugin = tmp_path / ".docker/cli-plugins/docker-compose"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("placeholder")
    monkeypatch.setattr(compose, "_invoking_home", lambda: tmp_path)
    monkeypatch.setattr(compose.os, "access", lambda *a: True)
    commands = []
    def probe(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=1 if len(commands) == 1 else 0)
    monkeypatch.setattr(compose.subprocess, "run", probe)
    assert compose.compose_argv() == [str(plugin.resolve())]
    assert commands[-1] == [str(plugin.resolve()), "version"]


def test_compose_missing_dependency_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(compose.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(compose, "_invoking_home", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="Compose is unavailable"):
        compose.compose_argv()


@pytest.mark.parametrize("exit_code,error,status", [(0, None, "completed"), (0, "telemetry invalid\nmissing root", "failed"), (1, None, "failed")])
def test_result_status_includes_validation_errors(exit_code, error, status):
    row = {"exit_code": exit_code, "error": error}
    text = _result_summary("task", row, 1, 2)
    assert f"status={status}, agent_exit={exit_code}" in text
    assert "\n" not in text
    if error:
        assert "error=telemetry invalid missing root" in text
    assert row == {"exit_code": exit_code, "error": error}


@pytest.mark.parametrize("actual,expected,match", [("v7", "v7", True), ("v8", "v7", False)])
def test_platform_variant_must_match(actual, expected, match):
    client = SimpleNamespace(images=SimpleNamespace(get=lambda _: SimpleNamespace(
        attrs={"Os": "linux", "Architecture": "arm", "Variant": actual})))
    assert local_image_available(client, "image", "linux/arm/" + expected) is match


def test_cli_pull_always_uses_valid_docker_arguments(monkeypatch):
    commands = []
    def invoke(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("swe_rebench.docker.subprocess.run", invoke)
    assert pull_image(None, "image", "always", "linux/amd64")
    assert commands == [["docker", "pull", "--platform", "linux/amd64", "image"]]


def test_sdk_missing_policy_never_pulls_matching_image():
    client = SimpleNamespace(images=SimpleNamespace(
        get=lambda _: SimpleNamespace(attrs={"Os": "linux", "Architecture": "amd64"}),
        pull=lambda *a, **k: pytest.fail("cached image contacted registry")))
    assert pull_image(client, "image", "missing", "linux/amd64")
