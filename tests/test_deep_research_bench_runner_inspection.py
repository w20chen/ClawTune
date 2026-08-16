from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from swe_rebench.docker import ContainerResult

import deep_research_bench.host_runner as host_runner
from deep_research_bench.config import DRBConfig
from deep_research_bench.host_runner import (
    _apply_web_search_key,
    _pin_web_search_provider,
    _web_search_config_patch,
    _write_drb_task_inputs,
)
from deep_research_bench.runner import (
    _drb_agent_diagnostics,
    _drb_required_telemetry_error,
    _result_dict,
)
from deep_research_bench.task_source import DRBTask


def _config(gate_required: bool = True) -> DRBConfig:
    config = DRBConfig.from_yaml(
        Path(__file__).resolve().parents[1] / "deep_research_bench" / "config.example.yaml"
    )
    config.gate_required = gate_required
    return config


def _result(
    *,
    tool_spans: int = 1,
    sampled: int = 1,
    launcher: int = 1,
    has_llm: bool = True,
    sandbox_attributed: int = 0,
) -> dict:
    inspection = [
        {
            "tool_span_ends": tool_spans,
            "launcher_tool_span_ends": launcher,
            "llm_span_ends": 1 if has_llm else 0,
            "has_llm_span": has_llm,
            "has_tool_span": tool_spans > 0,
            "resource_sampled_tool_span_ends": sampled,
            "shared_sandbox_tool_span_ends": sandbox_attributed,
            "docker_exec_pid_tool_span_ends": 0,
            "cgroup_tool_span_ends": 0,
        }
    ]
    return {
        "resource_summary": {
            "tool_span_ends": tool_spans,
            "launcher_tool_span_ends": launcher,
            "resource_sampled_tool_span_ends": sampled,
            "shared_sandbox_tool_span_ends": sandbox_attributed,
            "docker_exec_pid_tool_span_ends": 0,
            "cgroup_tool_span_ends": 0,
        },
        "trace_inspection": inspection,
    }


def test_gate_disabled_never_fails() -> None:
    config = _config(gate_required=False)
    assert _drb_required_telemetry_error(config, _result(tool_spans=0)) is None


def test_gate_fails_on_no_tool_spans() -> None:
    config = _config()
    error = _drb_required_telemetry_error(config, _result(tool_spans=0))
    assert error is not None
    assert "no tool spans" in error


def test_gate_fails_on_incomplete_launcher_sampling() -> None:
    config = _config()
    error = _drb_required_telemetry_error(
        config, _result(tool_spans=3, launcher=3, sampled=2)
    )
    assert error is not None
    assert "sampled 2/3 launcher tool spans" in error


def test_gate_ignores_unsampled_in_process_tool_spans() -> None:
    """In-process (non-launcher) tool spans carry no sandbox resource sampling,
    so an otherwise fully-sampled launcher population must not be penalized."""
    config = _config()
    # 3 tool spans total: 2 launcher (all sampled) + 1 in-process (unsampled).
    error = _drb_required_telemetry_error(
        config, _result(tool_spans=3, launcher=2, sampled=2)
    )
    assert error is None


def test_gate_fails_on_no_llm_spans() -> None:
    config = _config()
    error = _drb_required_telemetry_error(config, _result(has_llm=False))
    assert error is not None
    assert "no LLM spans" in error


def test_gate_passes_with_llm_and_sampled_tool_spans() -> None:
    config = _config()
    assert _drb_required_telemetry_error(config, _result()) is None


def test_drb_agent_diagnostics_ignores_missing_patch() -> None:
    # A research task answers a question; a missing code patch is not a failure.
    artifacts = {
        "result_summary.json": {"summary": {"has_patch": False}},
        "agent-stdout.txt": {"preview": ""},
        "agent-stderr.txt": {"preview": ""},
    }
    smoke = {"agent_exit_code": 0, "has_patch": False}
    diagnostics = _drb_agent_diagnostics([], artifacts, smoke)
    assert diagnostics["failure_kind"] is None
    assert diagnostics["failure"] is None


def test_result_dict_inspects_v6_trace(tmp_path) -> None:
    trace = tmp_path / "trace.jsonl"
    lines = [
        {"type": "span_start", "kind": "llm", "name": "model_call"},
        {"type": "span_start", "kind": "tool", "name": "web_search"},
        {
            "type": "span_end",
            "kind": "tool",
            "name": "web_search",
            "status": {"code": "ok"},
            "resources": {
                "scope": "process_tree",
                "attribution_source": "docker-exec-pid",
                "attribution_status": "attributed",
                "sampling_point_count": 5,
                "cpu_time_s": 0.1,
                "rss_peak_bytes": 1024,
                "resource_timeline": [{"t": 0.0}],
            },
            "execution": {"source": "docker-cgroup-diff"},
        },
    ]
    trace.write_text(
        "".join(json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )
    result = ContainerResult(
        task_id="7",
        image="python:3.11-slim",
        exit_code=0,
        error=None,
        trace_dir=tmp_path,
        trace_files=[trace],
        duration_seconds=1.0,
    )
    rendered = _result_dict(_config(), result)
    assert rendered["task_id"] == "7"
    assert rendered["resource_summary"]["tool_span_ends"] == 1
    assert rendered["resource_summary"]["resource_sampled_tool_span_ends"] == 1
    assert rendered["resource_summary"]["docker_exec_pid_tool_span_ends"] == 1
    assert rendered["trace_inspection"][0]["has_llm_span"] is True
    assert rendered["agent_diagnostics"]["failure_kind"] is None


def test_write_drb_task_inputs_writes_manifest_and_reference(tmp_path) -> None:
    task = DRBTask(
        instance_id="7",
        problem_statement="research question",
        reference_answer="the reference article",
        topic="physics",
        difficulty="phd",
        domain="science-technology",
    )
    config = _config()
    workspace = tmp_path / "workspace"
    _write_drb_task_inputs(tmp_path, task, config, workspace)

    prompt = (tmp_path / "agent_prompt.txt").read_text(encoding="utf-8")
    assert "research question" in prompt

    manifest = json.loads((tmp_path / "task_manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_id"] == "7"
    assert manifest["benchmark"] == "deep-research-bench"
    assert manifest["reference_answer_bytes"] == len("the reference article")
    assert manifest["problem_statement_bytes"] == len("research question")
    assert manifest["sandbox_image"] == "python:3.11-slim"

    assert (tmp_path / "reference_answer.txt").read_text(encoding="utf-8") == (
        "the reference article"
    )
    assert (tmp_path / "agent-cwd.txt").read_text(encoding="utf-8").strip() == str(
        workspace
    )


def test_write_drb_task_inputs_skips_empty_reference(tmp_path) -> None:
    task = DRBTask(instance_id="8", problem_statement="q", reference_answer="")
    _write_drb_task_inputs(tmp_path, task, _config(), tmp_path / "workspace")
    assert not (tmp_path / "reference_answer.txt").exists()


def test_apply_web_search_key_injects_env(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = _config()
    config.web_search.enabled = True
    config.web_search.api_key = "tvly-key"
    _apply_web_search_key(config)
    assert os.environ.get("TAVILY_API_KEY") == "tvly-key"
    # _apply_web_search_key mutates the global env directly; restore the
    # pre-test state so later tests do not see the leaked key.
    os.environ.pop("TAVILY_API_KEY", None)


def test_apply_web_search_key_skips_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = _config()
    config.web_search.enabled = False
    config.web_search.api_key = "tvly-key"
    _apply_web_search_key(config)
    assert "TAVILY_API_KEY" not in os.environ


def test_apply_web_search_key_keeps_existing_env(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "existing")
    config = _config()
    config.web_search.api_key = "tvly-key"
    _apply_web_search_key(config)
    assert os.environ.get("TAVILY_API_KEY") == "existing"


def test_web_search_config_patch_pins_tavily() -> None:
    config = _config()
    config.web_search.enabled = True
    config.web_search.provider = "tavily"
    assert _web_search_config_patch(config) == {
        "tools": {
            "alsoAllow": ["web_search", "web_fetch"],
            "sandbox": {
                "tools": {"alsoAllow": ["web_search", "web_fetch"]},
            },
            "web": {"search": {"enabled": True, "provider": "tavily"}},
        }
    }


def test_web_search_config_patch_none_when_disabled() -> None:
    config = _config()
    config.web_search.enabled = False
    assert _web_search_config_patch(config) is None


def test_web_search_config_patch_auto_keeps_detection() -> None:
    config = _config()
    config.web_search.provider = "auto"
    assert _web_search_config_patch(config) == {
        "tools": {
            "alsoAllow": ["web_search", "web_fetch"],
            "sandbox": {
                "tools": {"alsoAllow": ["web_search", "web_fetch"]},
            },
            "web": {"search": {"enabled": True}},
        }
    }


def _fake_result(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=""
    )


def _pin_provider(config: DRBConfig, trace_dir: Path) -> None:
    _pin_web_search_provider(
        trace_dir=trace_dir,
        openclaw_home=trace_dir / "home",
        sidecar_port=12345,
        config=config,
        swe_cfg=config.to_swe_runner_config(),
        workspace=trace_dir / "ws",
    )


def test_pin_web_search_provider_pins_when_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_runner, "_openclaw_env", lambda *a, **k: {})
    monkeypatch.setattr(host_runner, "_require_executable", lambda name: "openclaw")
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _fake_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config()
    config.web_search.provider = "tavily"
    _pin_provider(config, tmp_path)
    assert len(calls) == 1
    assert json.loads(calls[0]) == {
        "tools": {
            "alsoAllow": ["web_search", "web_fetch"],
            "sandbox": {
                "tools": {"alsoAllow": ["web_search", "web_fetch"]},
            },
            "web": {"search": {"enabled": True, "provider": "tavily"}},
        }
    }
    log = (tmp_path / "web-search-config.log").read_text(encoding="utf-8")
    assert "degrading to auto-detection" not in log


def test_pin_web_search_provider_degrades_to_auto_when_provider_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(host_runner, "_openclaw_env", lambda *a, **k: {})
    monkeypatch.setattr(host_runner, "_require_executable", lambda name: "openclaw")
    monkeypatch.setattr(
        host_runner, "_discover_web_search_provider_plugin", lambda provider: None
    )
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _fake_result(0 if len(calls) == 2 else 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config()
    config.web_search.provider = "tavily"
    _pin_provider(config, tmp_path)
    assert len(calls) == 2
    # First attempt pins tavily; the fallback retries with auto-detection.
    assert json.loads(calls[0])["tools"]["web"]["search"]["provider"] == "tavily"
    fallback = json.loads(calls[1])
    assert "provider" not in fallback["tools"]["web"]["search"]
    assert fallback["tools"]["alsoAllow"] == ["web_search", "web_fetch"]
    assert fallback["tools"]["sandbox"]["tools"]["alsoAllow"] == [
        "web_search",
        "web_fetch",
    ]
    log = (tmp_path / "web-search-config.log").read_text(encoding="utf-8")
    assert "degrading to auto-detection" in log
    assert "openclaw doctor --fix" in log


def test_pin_web_search_provider_raises_when_pin_and_fallback_fail(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(host_runner, "_openclaw_env", lambda *a, **k: {})
    monkeypatch.setattr(host_runner, "_require_executable", lambda name: "openclaw")
    monkeypatch.setattr(
        host_runner, "_discover_web_search_provider_plugin", lambda provider: None
    )
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _fake_result(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config()
    config.web_search.provider = "tavily"
    with pytest.raises(RuntimeError, match="openclaw_web_search_config_patch_failed"):
        _pin_provider(config, tmp_path)
    assert len(calls) == 2


def test_pin_web_search_provider_raises_on_auto_failure(tmp_path, monkeypatch) -> None:
    # With provider=auto there is no pinned provider to fall back from, so a
    # patch failure is raised directly (one attempt only).
    monkeypatch.setattr(host_runner, "_openclaw_env", lambda *a, **k: {})
    monkeypatch.setattr(host_runner, "_require_executable", lambda name: "openclaw")
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _fake_result(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config()
    config.web_search.provider = "auto"
    with pytest.raises(RuntimeError, match="openclaw_web_search_config_patch_failed"):
        _pin_provider(config, tmp_path)
    assert len(calls) == 1


def test_pin_web_search_provider_skips_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_runner, "_openclaw_env", lambda *a, **k: {})
    monkeypatch.setattr(host_runner, "_require_executable", lambda name: "openclaw")
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _fake_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config()
    config.web_search.enabled = False
    _pin_provider(config, tmp_path)
    assert calls == []


def test_discover_web_search_provider_plugin_finds_global_install(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin_dir = (
        tmp_path / ".openclaw" / "npm" / "projects" / "openclaw-tavily-plugin-8ad843922d"
    )
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "package.json").write_text(
        '{"name": "@openclaw/tavily-plugin"}', encoding="utf-8"
    )
    assert host_runner._discover_web_search_provider_plugin("tavily") == plugin_dir


def test_discover_web_search_provider_plugin_prefers_plugin_package(
    tmp_path, monkeypatch
) -> None:
    """Prefer the real plugin package under node_modules/<package>.

    The npm project dir's own package.json (the workspace manifest) lacks
    ``openclaw.extensions``, so ``openclaw plugins install --link`` rejects it.
    The actual plugin package carries the manifest and must be returned.
    """
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = (
        tmp_path / ".openclaw" / "npm" / "projects" / "openclaw-tavily-plugin-8ad843922d"
    )
    project_dir.mkdir(parents=True)
    (project_dir / "package.json").write_text('{"name": "project"}', encoding="utf-8")
    plugin_package = project_dir / "node_modules" / "@openclaw" / "tavily-plugin"
    plugin_package.mkdir(parents=True)
    (plugin_package / "package.json").write_text(
        '{"name": "@openclaw/tavily-plugin", '
        '"openclaw": {"extensions": ["./dist/index.js"]}}',
        encoding="utf-8",
    )
    assert host_runner._discover_web_search_provider_plugin("tavily") == plugin_package


def test_discover_web_search_provider_plugin_none_when_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert host_runner._discover_web_search_provider_plugin("tavily") is None


def test_discover_web_search_provider_plugin_none_for_unknown_provider(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert host_runner._discover_web_search_provider_plugin("acme-search") is None


def test_link_web_search_provider_plugin_links_and_enables(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    plugin_dir = tmp_path / "plugin"
    ok = host_runner._link_web_search_provider_plugin(
        openclaw="openclaw",
        env={},
        log_path=tmp_path / "web-search-config.log",
        plugin_dir=plugin_dir,
        plugin_id="tavily",
    )
    assert ok is True
    assert calls == [
        ["openclaw", "plugins", "install", "--link", str(plugin_dir)],
        ["openclaw", "plugins", "enable", "tavily"],
    ]


def test_link_web_search_provider_plugin_fails_on_link_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_result(1))
    ok = host_runner._link_web_search_provider_plugin(
        openclaw="openclaw",
        env={},
        log_path=tmp_path / "web-search-config.log",
        plugin_dir=tmp_path / "plugin",
        plugin_id="tavily",
    )
    assert ok is False


def test_link_web_search_provider_plugin_fails_on_exception(tmp_path, monkeypatch) -> None:
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=[], timeout=60)

    monkeypatch.setattr(subprocess, "run", boom)
    ok = host_runner._link_web_search_provider_plugin(
        openclaw="openclaw",
        env={},
        log_path=tmp_path / "web-search-config.log",
        plugin_dir=tmp_path / "plugin",
        plugin_id="tavily",
    )
    assert ok is False


def test_root_safe_link_target_non_root_returns_plugin_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(host_runner, "_running_as_root", lambda: False)
    plugin_dir = tmp_path / "plugin"
    target = host_runner._root_safe_provider_plugin_link_target(
        plugin_dir=plugin_dir,
        plugin_id="tavily",
        env={},
        log_path=tmp_path / "web-search-config.log",
    )
    assert target == plugin_dir


def test_root_safe_link_target_root_owned_links_in_place(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_runner, "_running_as_root", lambda: True)
    monkeypatch.setattr(host_runner, "_plugin_is_root_owned", lambda path: True)
    plugin_dir = tmp_path / "plugin"
    target = host_runner._root_safe_provider_plugin_link_target(
        plugin_dir=plugin_dir,
        plugin_id="tavily",
        env={},
        log_path=tmp_path / "web-search-config.log",
    )
    assert target == plugin_dir


def test_root_safe_link_target_copies_non_root_plugin_into_isolated_home(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(host_runner, "_running_as_root", lambda: True)
    monkeypatch.setattr(host_runner, "_plugin_is_root_owned", lambda path: False)
    project = tmp_path / ".openclaw" / "npm" / "projects" / "openclaw-tavily-plugin-abc123"
    pkg = project / "node_modules" / "@openclaw" / "tavily-plugin"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        '{"openclaw": {"extensions": ["./dist/index.js"]}}', encoding="utf-8"
    )
    (project / "package.json").write_text('{"name": "project"}', encoding="utf-8")

    copied: list[tuple[Path, Path]] = []

    def fake_copytree(src, dst):
        copied.append((Path(src), Path(dst)))
        # Emulate the copy so the link target exists afterwards.
        (Path(dst) / "node_modules" / "@openclaw" / "tavily-plugin").mkdir(parents=True)
        (Path(dst) / "node_modules" / "@openclaw" / "tavily-plugin" / "package.json").write_text(
            "{}", encoding="utf-8"
        )

    monkeypatch.setattr(shutil, "copytree", fake_copytree)
    monkeypatch.setattr(shutil, "rmtree", lambda p, **k: None)
    env = {"OPENCLAW_HOME": str(tmp_path / "isolated-home")}
    target = host_runner._root_safe_provider_plugin_link_target(
        plugin_dir=pkg,
        plugin_id="tavily",
        env=env,
        log_path=tmp_path / "web-search-config.log",
    )
    assert copied and copied[0][0] == project
    assert target == (
        tmp_path
        / "isolated-home"
        / "linked-provider-plugins"
        / "openclaw-tavily-plugin-abc123"
        / "node_modules"
        / "@openclaw"
        / "tavily-plugin"
    )


def test_pin_web_search_provider_links_global_plugin_then_retries(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(host_runner, "_openclaw_env", lambda *a, **k: {})
    monkeypatch.setattr(host_runner, "_require_executable", lambda name: "openclaw")
    monkeypatch.setattr(
        host_runner,
        "_discover_web_search_provider_plugin",
        lambda provider: tmp_path / "plugin",
    )
    monkeypatch.setattr(host_runner, "_link_web_search_provider_plugin", lambda **k: True)
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs["input"])
        return _fake_result(0 if len(calls) == 2 else 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config()
    config.web_search.provider = "tavily"
    _pin_provider(config, tmp_path)
    # First patch fails; the global plugin is linked; the retry pins tavily
    # again and succeeds — no fallback to auto-detection.
    assert len(calls) == 2
    for payload in calls:
        assert json.loads(payload)["tools"]["web"]["search"]["provider"] == "tavily"
    log = (tmp_path / "web-search-config.log").read_text(encoding="utf-8")
    assert "degrading to auto-detection" not in log
