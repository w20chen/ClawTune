import json
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from swe_rebench.config import RunnerConfig
from swe_rebench.docker import ContainerResult
from swe_rebench.host_sandbox import (
    _cleanup_openclaw_sandbox_containers,
    _cleanup_runtime_artifacts,
    _docker_sandbox_container_ids,
    _ensure_openclaw_sandbox_image,
    _ensure_plugin_built,
    _install_sandbox_launcher,
    _make_sandbox_workspace_writable,
    _openclaw_config,
    _openclaw_env,
    _run_openclaw_agent,
    _reset_directory,
    _sandbox_container_prefix,
    _stage_plugin_for_openclaw_if_needed,
    _start_sidecar,
    _verify_sandbox_launcher,
    _write_host_tool_resource_preflight,
    _write_task_inputs,
)
from swe_rebench.task_source import TaskDef
from swe_rebench.docker import run_container
from swe_rebench.prepare import (
    _ENTRYPOINT_TEMPLATE,
    _PLUGIN_CONFIG,
    _build_plugin_dist,
    _write_entrypoint,
    _write_plugin_config,
    bundle_needs_rebuild,
)
from swe_rebench.sandbox import sandbox_container_prefix
from swe_rebench.task_source import filter_tasks, parse_instance_ids, tasks_from_records
from swe_rebench.runner import (
    _apply_runtime_overrides,
    _inspect_trace,
    _resource_summary,
    _run_one,
    _require_llm_api_key,
    _reset_task_trace_dir,
    _smoke_summary,
    _task_artifacts,
)


def _records() -> list[dict[str, object]]:
    return [
        {
            "instance_id": "django__a",
            "docker_image": "swerebench/sweb.eval.x86_64.django-a:latest",
            "problem_statement": "A",
            "repo": "django/django",
        },
        {
            "instance_id": "flask__b",
            "docker_image": "swerebench/sweb.eval.x86_64.flask-b:latest",
            "problem_statement": "B",
            "repo": "pallets/flask",
        },
        {
            "instance_id": "django__c",
            "docker_image": "swerebench/sweb.eval.x86_64.django-c:latest",
            "problem_statement": "C",
            "repo": "django/django",
        },
    ]


def test_filter_tasks_supports_repo_skip_and_sample() -> None:
    tasks = tasks_from_records(_records())

    selected = filter_tasks(tasks, repo="django/django", skip=1, sample=1)

    assert [task.instance_id for task in selected] == ["django__c"]


def test_filter_tasks_preserves_instance_id_order() -> None:
    tasks = tasks_from_records(_records())

    selected = filter_tasks(
        tasks,
        instance_ids=parse_instance_ids("django__c,django__a"),
    )

    assert [task.instance_id for task in selected] == ["django__c", "django__a"]


def test_runner_dry_run_accepts_batch_selection_args(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps(_records()), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "swe_rebench.runner",
            "run",
            "--config",
            "swe_rebench/config.example.yaml",
            "--tasks",
            str(tasks_path),
            "--repo",
            "django/django",
            "--sample",
            "1",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Loaded 1 tasks" in result.stderr
    assert "django__a" in result.stderr
    assert "django__c" not in result.stderr


def test_runner_falls_back_to_example_config_when_config_yaml_is_missing(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps(_records()), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "swe_rebench.runner",
            "run",
            "--config",
            "swe_rebench/does-not-exist.yaml",
            "--tasks",
            str(tasks_path),
            "--repo",
            "django/django",
            "--sample",
            "1",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "falling back to example config" in result.stderr
    assert "Loaded 1 tasks" in result.stderr


def test_inspect_trace_flags_missing_task_id_and_tool_spans(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "record_type": "trace_metadata",
                        "trace_format_version": 6,
                    }
                ),
                json.dumps(
                    {
                        "record_type": "span_start",
                        "kind": "llm",
                        "name": "model",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "django__missing")

    assert report["has_llm_span"] is True
    assert report["has_tool_span"] is False
    assert "trace does not contain TASK_INSTANCE_ID" in report["warnings"]
    assert "trace has no tool span/action" in report["warnings"]


def test_inspect_trace_detects_tool_kind_span(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps({"record_type": "trace_metadata", "trace_format_version": 6}),
                json.dumps(
                    {
                        "record_type": "span_start",
                        "kind": "tool",
                        "name": "exec",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "")

    assert report["has_tool_span"] is True
    assert "trace has no tool span/action" not in report["warnings"]


def test_inspect_trace_warns_when_launcher_spans_are_unattributed(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"record_type": "trace_metadata", "trace_format_version": 6})
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "kind": "tool",
                "name": "exec",
                "execution": {"mode": "launcher"},
                "resources": {"attribution_status": "unattributed"},
                "status": {"code": "ok"},
                "output": {"exit_code": None},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "")

    assert report["launcher_tool_span_ends"] == 1
    assert report["unattributed_launcher_tool_span_ends"] == 1
    assert "launcher tool spans have no resource attribution" in report["warnings"]
    assert "launcher tool spans have no cgroup resource samples" in report["warnings"]


def test_inspect_trace_summarizes_cgroup_resource_coverage(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"record_type": "trace_metadata", "trace_format_version": 6})
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "kind": "tool",
                "name": "exec",
                "execution": {"mode": "launcher", "cgroup_path": "/sys/fs/cgroup/claw/exec-1"},
                "resources": {
                    "attribution_status": "attributed",
                    "scope": "cgroup",
                },
                "status": {"code": "ok"},
                "output": {"exit_code": 0},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "")
    summary = _resource_summary([report])

    assert report["cgroup_tool_span_ends"] == 1
    assert report["launcher_cgroup_tool_span_ends"] == 1
    assert report["attributed_tool_span_ends"] == 1
    assert report["launcher_attributed_tool_span_ends"] == 1
    assert "launcher tool spans have no cgroup resource samples" not in report["warnings"]
    assert summary["cgroup_tool_span_ends"] == 1
    assert summary["launcher_cgroup_tool_span_ends"] == 1
    assert summary["cgroup_coverage_ratio"] == 1.0


def test_resource_summary_uses_launcher_cgroup_coverage_ratio(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"record_type": "trace_metadata", "trace_format_version": 6})
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "kind": "tool",
                "name": "exec",
                "execution": {"mode": "launcher"},
                "resources": {"attribution_status": "unattributed"},
                "status": {"code": "ok"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "kind": "tool",
                "name": "read",
                "resources": {
                    "attribution_status": "attributed",
                    "scope": "cgroup",
                },
                "status": {"code": "ok"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "")
    summary = _resource_summary([report])

    assert report["launcher_tool_span_ends"] == 1
    assert report["cgroup_tool_span_ends"] == 1
    assert report["launcher_cgroup_tool_span_ends"] == 0
    assert summary["cgroup_tool_span_ends"] == 1
    assert summary["launcher_cgroup_tool_span_ends"] == 0
    assert summary["cgroup_coverage_ratio"] == 0.0


def test_inspect_trace_warns_when_ok_status_has_failed_exit_code(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"record_type": "trace_metadata", "trace_format_version": 6})
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "kind": "tool",
                "name": "exec",
                "status": {"code": "ok"},
                "output": {"result": {"details": {"exitCode": 1}}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "")

    assert report["failed_tool_span_ends"] == 1
    assert "tool span status disagrees with non-zero exit code" in report["warnings"]


def test_inspect_trace_does_not_treat_result_code_as_exit_code(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"record_type": "trace_metadata", "trace_format_version": 6})
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "kind": "tool",
                "name": "web_fetch",
                "status": {"code": "ok"},
                "output": {"result": {"code": 404, "body": "not found"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = _inspect_trace(trace_path, "")

    assert report["failed_tool_span_ends"] == 0
    assert "tool span status disagrees with non-zero exit code" not in report["warnings"]


def test_entrypoint_uses_runtime_llm_env_and_writes_task_manifest() -> None:
    assert 'AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL="${LLM_UPSTREAM_BASE_URL:-__UPSTREAM__}"' in _ENTRYPOINT_TEMPLATE
    assert 'AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY="${LLM_API_KEY:-__LLM_KEY__}"' in _ENTRYPOINT_TEMPLATE
    assert (
        'AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED="${AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED:-false}"'
        in _ENTRYPOINT_TEMPLATE
    )
    assert 'export OPENCLAW_MODEL_REF="${OPENCLAW_MODEL_REF:-__MODEL_FULL__}"' in _ENTRYPOINT_TEMPLATE
    assert 'task_manifest.json' in _ENTRYPOINT_TEMPLATE
    assert 'agent-cwd.txt' in _ENTRYPOINT_TEMPLATE
    assert 'agent_prompt.txt' in _ENTRYPOINT_TEMPLATE
    assert 'agent-stdout.txt' in _ENTRYPOINT_TEMPLATE
    assert 'sidecar.log' in _ENTRYPOINT_TEMPLATE
    assert 'model.patch' in _ENTRYPOINT_TEMPLATE
    assert 'result_summary.json' in _ENTRYPOINT_TEMPLATE
    assert 'cgroup_probe.json' in _ENTRYPOINT_TEMPLATE
    assert 'tool_resource_preflight.json' in _ENTRYPOINT_TEMPLATE


def test_swe_rebench_plugin_config_uses_managed_wrapper_cgroup() -> None:
    assert _PLUGIN_CONFIG["executionBackend"] == "managed-wrapper"
    assert _PLUGIN_CONFIG["launcherPath"] == "/opt/claw/bin/claw-launch"
    assert _PLUGIN_CONFIG["enableCgroup"] is True
    assert _PLUGIN_CONFIG["securityBoundaryAccepted"] is True
    assert _PLUGIN_CONFIG["recordRawTrace"] is False
    assert _PLUGIN_CONFIG["trace"]["include_raw_events"] is False


def test_entrypoint_installs_stable_launcher_path() -> None:
    assert "cat > /opt/claw/bin/claw-launch" in _ENTRYPOINT_TEMPLATE
    assert 'export PYTHONPATH="/claw/scheduler/src${PYTHONPATH:+:$PYTHONPATH}"' in _ENTRYPOINT_TEMPLATE
    assert "python3 -m agent_scheduler.launcher" in _ENTRYPOINT_TEMPLATE


def test_setup_installs_scheduler_runtime_dependencies() -> None:
    from swe_rebench.prepare import _SETUP_TEMPLATE

    assert "fastapi uvicorn pydantic psutil httpx prometheus-client numpy" in _SETUP_TEMPLATE
    assert "import fastapi, uvicorn, pydantic, psutil, numpy" in _SETUP_TEMPLATE
    assert "python3-bpfcc bpfcc-tools libbpfcc" in _SETUP_TEMPLATE
    assert "clang llvm kmod" in _SETUP_TEMPLATE
    assert 'linux-headers-"$(uname -r)"' in _SETUP_TEMPLATE
    assert "/tmp/.claw_bcc_pythonpath" in _SETUP_TEMPLATE
    assert "import bcc" in _SETUP_TEMPLATE


def test_docker_runner_config_sets_sandbox_container_prefix_placeholder(tmp_path: Path) -> None:
    _write_plugin_config(tmp_path)

    parsed = json.loads((tmp_path / "openclaw-config.json5").read_text(encoding="utf-8"))
    agent_defaults = parsed["agents"]["defaults"]
    docker_cfg = parsed["agents"]["defaults"]["sandbox"]["docker"]

    assert agent_defaults["workspace"] == "/testbed"
    assert agent_defaults["repoRoot"] == "/testbed"
    assert agent_defaults["tools"]["elevated"]["enabled"] is True
    assert docker_cfg["containerPrefix"] == "__SANDBOX_CONTAINER_PREFIX__"
    assert parsed["env"]["CLAW_EXEC_WORKDIR"] == "/testbed"
    assert parsed["env"]["CLAW_SCHEDULER_ENDPOINT"] == "http://127.0.0.1:8765"
    assert "__SANDBOX_CONTAINER_PREFIX__" in _ENTRYPOINT_TEMPLATE
    assert "openclaw config patch --stdin" in _ENTRYPOINT_TEMPLATE


def test_entrypoint_exports_container_runtime_identity_for_launcher() -> None:
    assert 'export CLAW_SCHEDULER_ENDPOINT="http://127.0.0.1:$SIDECAR_PORT"' in _ENTRYPOINT_TEMPLATE
    assert 'export CLAW_EXEC_WORKDIR="/testbed"' in _ENTRYPOINT_TEMPLATE
    assert "AGENT_SCHEDULER_SANDBOX_CONTAINER_ID" in _ENTRYPOINT_TEMPLATE
    assert "CLAW_SANDBOX_CONTAINER_ID" in _ENTRYPOINT_TEMPLATE
    assert '"container_id": os.environ.get("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID")' in _ENTRYPOINT_TEMPLATE
    assert "tool_resource_preflight.json" in _ENTRYPOINT_TEMPLATE
    assert '"stage2_ready"' in _ENTRYPOINT_TEMPLATE
    assert '"clang": shutil.which("clang")' in _ENTRYPOINT_TEMPLATE


def test_runner_config_enables_complete_cgroup_sampling() -> None:
    config = RunnerConfig.from_yaml("swe_rebench/config.yaml")

    assert config.runtime.mode == "container-openclaw"
    assert config.runtime.stage2_required is False
    assert config.docker.privileged is True
    assert config.docker.cgroupns_mode == "host"
    assert config.docker.cgroup_mount_rw is True
    assert config.docker.cgroup_required is False


def test_runner_config_parses_host_openclaw_sandbox_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "host-openclaw-sandbox"
""",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.runtime.mode == "host-openclaw-sandbox"


def test_runner_normalizes_host_openclaw_container_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'runtime:\n  mode: "host-openclaw-container"\n',
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    assert config.runtime.mode == "host-openclaw-sandbox"

    config.runtime.stage2_required = False
    _apply_runtime_overrides(
        config,
        runtime_mode="host-openclaw-container",
        stage2_required=None,
    )
    assert config.runtime.mode == "host-openclaw-sandbox"
    assert config.runtime.stage2_required is True


def test_cli_host_sandbox_override_requires_stage2_by_default(tmp_path: Path) -> None:
    config = RunnerConfig.from_yaml("swe_rebench/config.yaml", repo_root=tmp_path)

    _apply_runtime_overrides(
        config,
        runtime_mode="host-openclaw-sandbox",
        stage2_required=None,
    )

    assert config.runtime.mode == "host-openclaw-sandbox"
    assert config.runtime.stage2_required is True

    _apply_runtime_overrides(
        config,
        runtime_mode="host-openclaw-sandbox",
        stage2_required=False,
    )
    assert config.runtime.stage2_required is False


def test_runner_config_rejects_unknown_runtime_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "mystery"
""",
        encoding="utf-8",
    )

    try:
        RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    except ValueError as exc:
        assert "runtime.mode" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_run_one_dispatches_to_host_sandbox_runtime(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "host-openclaw-sandbox"
output:
  trace_root: "traces"
  report_path: "report.json"
bundle:
  output_dir: "bundle"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="task-1", image="image:latest", problem_statement="fix")
    trace_dir = tmp_path / "traces" / "task-1"
    trace_dir.mkdir(parents=True)
    (trace_dir / "stale.txt").write_text("old", encoding="utf-8")
    called: dict[str, object] = {}

    def fake_host_runner(**kwargs):
        called.update(kwargs)
        assert kwargs["trace_dir"].is_dir()
        assert not (kwargs["trace_dir"] / "stale.txt").exists()
        return ContainerResult(
            task_id="task-1",
            image="image:latest",
            exit_code=0,
            trace_dir=trace_dir,
        )

    import swe_rebench.runner as runner

    monkeypatch.setattr(runner, "run_host_sandbox_task", fake_host_runner)

    result = _run_one(
        client=object(),
        task=task,
        bundle_dir=tmp_path / "bundle",
        trace_dir=trace_dir,
        config=config,
    )

    assert result.exit_code == 0
    assert called["task"] == task
    assert called["config"] == config


def test_run_one_propagates_container_stage2_fallback_mode(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "container-openclaw"
  stage2_required: false
output:
  trace_root: "traces"
  report_path: "report.json"
bundle:
  output_dir: "bundle"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="task-1", image="image:latest", problem_statement="fix")
    trace_dir = tmp_path / "traces" / "task-1"
    called: dict[str, object] = {}

    import swe_rebench.runner as runner

    monkeypatch.setattr(runner, "pull_image", lambda *args, **kwargs: True)

    def fake_run_container(**kwargs):
        called.update(kwargs)
        return ContainerResult(
            task_id="task-1",
            image="image:latest",
            exit_code=0,
            trace_dir=trace_dir,
        )

    monkeypatch.setattr(runner, "run_container", fake_run_container)

    result = _run_one(
        client=object(),
        task=task,
        bundle_dir=tmp_path / "bundle",
        trace_dir=trace_dir,
        config=config,
    )

    assert result.exit_code == 0
    assert called["stage2_required"] is False


def test_host_sandbox_openclaw_config_uses_only_public_top_level_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    raw = _openclaw_config(
        endpoint_host="http://127.0.0.1:8765",
        endpoint_sandbox="http://host.docker.internal:8765",
        workspace=tmp_path / "workspace",
        config=config,
    )
    parsed = json.loads(raw)

    assert set(parsed) == {"agents", "plugins", "env"}
    assert parsed["agents"]["defaults"]["workspace"] == "/workspace"
    assert parsed["agents"]["defaults"]["repoRoot"] == "/workspace"
    docker_cfg = parsed["agents"]["defaults"]["sandbox"]["docker"]
    assert docker_cfg["containerPrefix"] == _sandbox_container_prefix(tmp_path / "workspace")
    assert docker_cfg["workdir"] == "/workspace"
    assert docker_cfg["extraHosts"] == ["host.docker.internal:host-gateway"]
    assert "binds" not in docker_cfg
    assert parsed["agents"]["defaults"]["sandbox"]["workspaceAccess"] == "rw"
    plugin_cfg = parsed["plugins"]["entries"]["agent-scheduler"]["config"]
    assert plugin_cfg["logLevel"] == "warn"
    assert parsed["env"]["CLAW_EXEC_WORKDIR"] == "/workspace"
    assert parsed["env"]["CLAW_SANDBOX_HOST_WORKSPACE"] == str(tmp_path / "workspace")
    assert parsed["env"]["CLAW_SANDBOX_CONTAINER_WORKSPACE"] == "/workspace"
    assert parsed["env"]["CLAW_ENABLE_CGROUP"] == "1"


def test_host_sandbox_container_prefix_is_stable_and_workspace_scoped(tmp_path: Path) -> None:
    first = _sandbox_container_prefix(tmp_path / "workspace-a")
    second = _sandbox_container_prefix(tmp_path / "workspace-a")
    other = _sandbox_container_prefix(tmp_path / "workspace-b")

    assert first == second
    assert first != other
    assert first.startswith("claw-srb-")
    assert first.endswith("-")
    assert len(first) < 32


def test_host_sandbox_cleanup_removes_prefixed_openclaw_containers(monkeypatch, tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    workspace = tmp_path / "workspace"
    prefix = _sandbox_container_prefix(workspace)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if cmd[:3] == ["/usr/bin/docker", "ps", "-aq"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\ndef456\n", stderr="")
        if cmd[:3] == ["/usr/bin/docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="removed\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr("swe_rebench.host_sandbox._require_executable", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _cleanup_openclaw_sandbox_containers(trace_dir, workspace)

    assert calls[0][0] == ["/usr/bin/docker", "ps", "-aq", "--filter", f"name={prefix}"]
    assert calls[1][0] == ["/usr/bin/docker", "rm", "-f", "abc123", "def456"]
    log = (trace_dir / "sandbox-container-cleanup.log").read_text(encoding="utf-8")
    assert prefix in log
    assert "abc123" in log


def test_host_sandbox_discovers_sandbox_container_by_docker_prefix(monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123def456\nnot a container id!\n", stderr="")

    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    assert _docker_sandbox_container_ids("/usr/bin/docker", "claw-srb-test") == ["abc123def456"]
    assert calls[0][0] == ["/usr/bin/docker", "ps", "-q", "--filter", "name=claw-srb-test"]


def test_host_sandbox_openclaw_env_points_workspace_dir_at_task_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OPENCLAW_AGENT_SCHEDULER_TRACE_DIR", "/tmp/plugin-should-not-write")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "stale-token")
    monkeypatch.setenv("OPENCLAW_GATEWAY_PASSWORD", "stale-password")
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:18789")

    env = _openclaw_env(tmp_path / "home", 8765, config, workspace)

    assert env["OPENCLAW_WORKSPACE_DIR"] == str(workspace)
    assert env["CLAW_EXEC_WORKDIR"] == "/workspace"
    assert env["CLAW_SANDBOX_HOST_WORKSPACE"] == str(workspace)
    assert env["CLAW_SANDBOX_CONTAINER_WORKSPACE"] == "/workspace"
    assert env["CLAW_ENABLE_CGROUP"] == "1"
    assert "OPENCLAW_AGENT_SCHEDULER_TRACE_DIR" not in env
    assert "OPENCLAW_GATEWAY_TOKEN" not in env
    assert "OPENCLAW_GATEWAY_PASSWORD" not in env
    assert "OPENCLAW_GATEWAY_URL" not in env


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented on Windows")
def test_host_sandbox_launcher_is_readable_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scheduler_src = tmp_path / "bundle" / "scheduler" / "src"
    package = scheduler_src / "agent_scheduler"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        _install_sandbox_launcher(workspace, tmp_path / "bundle")
    finally:
        os.umask(previous_umask)

    runtime = workspace / ".claw"
    launcher = runtime / "bin" / "claw-launch"
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert runtime.stat().st_mode & 0o055 == 0o055
    assert (runtime / "scheduler").stat().st_mode & 0o055 == 0o055
    assert (runtime / "scheduler" / "src").stat().st_mode & 0o055 == 0o055
    assert (runtime / "scheduler" / "src" / "agent_scheduler" / "__init__.py").stat().st_mode & 0o044 == 0o044


def test_host_sandbox_verifies_mounted_launcher_before_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        "swe_rebench.host_sandbox._require_executable",
        lambda name: "/usr/bin/docker",
    )
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _verify_sandbox_launcher(tmp_path, tmp_path / "workspace")

    command = calls[0][0]
    assert command[:4] == ["/usr/bin/docker", "run", "--rm", "--network"]
    assert command[command.index("--user") + 1] == "65534:65534"
    assert "/workspace/.claw/bin/claw-launch" in command
    assert command[-1] == "--help"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented on Windows")
def test_host_sandbox_makes_sudo_export_writable_without_following_symlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    package = workspace / "package"
    package.mkdir(parents=True, mode=0o700)
    source = package / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    source.chmod(0o600)
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    outside.chmod(0o600)
    (workspace / "outside-link").symlink_to(outside)

    _make_sandbox_workspace_writable(workspace)

    assert workspace.stat().st_mode & 0o007 == 0o007
    assert package.stat().st_mode & 0o007 == 0o007
    assert source.stat().st_mode & 0o006 == 0o006
    assert outside.stat().st_mode & 0o077 == 0


def test_host_sandbox_prompt_uses_relative_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="task-1", image="image:latest", problem_statement="fix")
    trace_dir = tmp_path / "trace"
    workspace = tmp_path / "workspace"

    _write_task_inputs(trace_dir, task, config, workspace)
    prompt = (trace_dir / "agent_prompt.txt").read_text(encoding="utf-8")

    assert "Use relative paths" in prompt
    assert "repository mounted at /workspace" not in prompt


def test_host_sandbox_agent_forces_sandbox_exec_workdir(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    openclaw_home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = TaskDef(instance_id="task-1", image="image:latest", problem_statement="fix")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 123
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("swe_rebench.host_sandbox._require_executable", lambda name: "/usr/bin/openclaw")
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.Popen", fake_popen)

    exit_code = _run_openclaw_agent(
        trace_dir=trace_dir,
        openclaw_home=openclaw_home,
        workspace=workspace,
        sidecar_port=8765,
        task=task,
        config=config,
    )

    assert exit_code == 0
    assert captured["cwd"] == str(tmp_path)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENCLAW_WORKSPACE_DIR"] == str(workspace)
    assert env["CLAW_EXEC_WORKDIR"] == "/workspace"
    assert env["CLAW_SANDBOX_HOST_WORKSPACE"] == str(workspace)
    assert env["CLAW_SANDBOX_CONTAINER_WORKSPACE"] == "/workspace"


def test_host_sandbox_tags_sandbox_image_from_task_image(monkeypatch, tmp_path: Path) -> None:
    task_image = "swebench/swe-rebench-task:latest"
    calls: list[list[str]] = []

    def fake_which(name: str):
        return "/usr/bin/docker" if name == "docker" else None

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return Result(1)
        if cmd[:2] == ["/usr/bin/docker", "tag"]:
            return Result(0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("swe_rebench.host_sandbox.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _ensure_openclaw_sandbox_image(task_image, tmp_path)

    assert calls == [
        ["/usr/bin/docker", "image", "inspect", "openclaw-sandbox:bookworm-slim"],
        ["/usr/bin/docker", "tag", task_image, "openclaw-sandbox:bookworm-slim"],
    ]
    assert (tmp_path / "sandbox-image-build.log").exists()


def test_host_sandbox_builds_plugin_before_install(monkeypatch, tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "package.json").write_text('{"scripts":{"build":"tsc"}}\n', encoding="utf-8")
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_require(name: str) -> str:
        assert name == "npm"
        return "/usr/bin/npm"

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs["cwd"] == str(plugin_dir)
        return Result()

    monkeypatch.setattr("swe_rebench.host_sandbox._require_executable", fake_require)
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _ensure_plugin_built(tmp_path / "trace", plugin_dir)

    assert calls == [["/usr/bin/npm", "run", "build"]]
    assert (tmp_path / "trace" / "plugin-build.log").exists()


def test_host_sandbox_stages_user_owned_plugin_when_running_as_root(monkeypatch, tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "package.json").write_text("{}\n", encoding="utf-8")
    (plugin_dir / "dist.js").write_text("module.exports = {}\n", encoding="utf-8")
    (plugin_dir / "node_modules").mkdir()
    (plugin_dir / "node_modules" / "ignored.txt").write_text("skip\n", encoding="utf-8")
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    monkeypatch.setattr("swe_rebench.host_sandbox.os.geteuid", lambda: 0, raising=False)
    original_stat = Path.stat

    class FakeStat:
        st_uid = 1005

    def fake_stat(path: Path, *args, **kwargs):
        if path == plugin_dir:
            return FakeStat()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    staged = _stage_plugin_for_openclaw_if_needed(
        trace_dir=trace_dir,
        plugin_dir=plugin_dir,
    )

    assert staged == trace_dir / "openclaw-plugin-root-owned"
    assert (staged / "package.json").exists()
    assert (staged / "dist.js").exists()
    assert not (staged / "node_modules").exists()


def test_host_sandbox_sidecar_enables_docker_exec_observer(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    workspace = tmp_path / "workspace"
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    captured: dict[str, object] = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.Popen", fake_popen)
    monkeypatch.setattr("swe_rebench.host_sandbox._wait_ready", lambda port: None)

    process = _start_sidecar(
        trace_dir=trace_dir,
        port=8765,
        config=config,
        workspace=workspace,
    )

    assert isinstance(process, FakeProcess)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["AGENT_SCHEDULER_DOCKER_EXEC_OBSERVER"] == "true"
    assert env["AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED"] == "false"
    assert env["AGENT_SCHEDULER_DOCKER_EXEC_CONTAINER_PREFIX"] == _sandbox_container_prefix(workspace)
    assert str(tmp_path / "services" / "scheduler" / "src") in env["PYTHONPATH"]


def test_host_sandbox_writes_tool_resource_preflight(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    captured: dict[str, object] = {}

    class FakeRunResult:
        stdout = '{"mode": "host-openclaw-sandbox"}\n'
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs["env"]
        return FakeRunResult()

    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _write_host_tool_resource_preflight(trace_dir, config)

    preflight = trace_dir / "tool_resource_preflight_host.json"
    assert preflight.read_text(encoding="utf-8") == '{"mode": "host-openclaw-sandbox"}\n'
    env = captured["env"]
    assert isinstance(env, dict)
    assert str(tmp_path / "services" / "scheduler" / "src") in env["PYTHONPATH"]


def test_host_sandbox_required_stage2_fails_fast_on_preflight(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw-sandbox\n  stage2_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    class FakeRunResult:
        stdout = json.dumps(
            {
                "mode": "host-openclaw-sandbox",
                "stage2_ready": False,
                "stage2_disabled_reason": "ValueError: clause telemetry requires root",
            }
        )
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        "swe_rebench.host_sandbox.subprocess.run",
        lambda *args, **kwargs: FakeRunResult(),
    )

    with pytest.raises(RuntimeError, match=r"requires root.*sudo -E"):
        _write_host_tool_resource_preflight(trace_dir, config)

    recorded = json.loads(
        (trace_dir / "tool_resource_preflight_host.json").read_text(encoding="utf-8")
    )
    assert recorded["stage2_ready"] is False


def test_host_sandbox_cleans_only_untracked_runtime_artifacts(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".claw").mkdir()
    (workspace / ".local").mkdir()
    (workspace / "AGENTS.md").write_text("tracked instructions\n", encoding="utf-8")
    (workspace / "HEARTBEAT.md").write_text("runtime\n", encoding="utf-8")
    (workspace / "openclaw-workspace-state.json").write_text("{}\n", encoding="utf-8")
    seen: list[str] = []

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        relative_path = cmd[-1]
        seen.append(relative_path)
        return Result(0 if relative_path == "AGENTS.md" else 1)

    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _cleanup_runtime_artifacts(workspace)

    assert (workspace / "AGENTS.md").exists()
    assert not (workspace / ".claw").exists()
    assert not (workspace / ".local").exists()
    assert not (workspace / "HEARTBEAT.md").exists()
    assert not (workspace / "openclaw-workspace-state.json").exists()
    assert "AGENTS.md" in seen


def test_host_sandbox_workspace_reset_falls_back_to_docker_on_permission_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspaces" / "task-1"
    workspace.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_rmtree(path, **kwargs):
        raise PermissionError(str(path))

    def fake_which(name: str):
        return "/usr/bin/docker" if name == "docker" else None

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return Result()

    monkeypatch.setattr("swe_rebench.host_sandbox.shutil.rmtree", fake_rmtree)
    monkeypatch.setattr("swe_rebench.host_sandbox.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _reset_directory(workspace, docker_cleanup_image="task-image:latest")

    assert calls
    cleanup_cmd = calls[0]
    assert cleanup_cmd[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert f"TARGET={workspace.name}" in cleanup_cmd
    assert str(workspace.parent.resolve()) + ":/host_parent" in cleanup_cmd
    assert "task-image:latest" in cleanup_cmd


def test_runner_trace_reset_repairs_readonly_stale_artifacts(tmp_path: Path) -> None:
    trace_root = tmp_path / "traces"
    trace_dir = trace_root / "task-1"
    artifact_dir = trace_dir / "tool-resource"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "clause-kb.json"
    artifact.write_text("{}\n", encoding="utf-8")
    artifact.chmod(0o400)
    artifact_dir.chmod(0o500)

    try:
        _reset_task_trace_dir(trace_root, trace_dir)
    finally:
        artifact_dir.chmod(0o700) if artifact_dir.exists() else None
        artifact.chmod(0o600) if artifact.exists() else None

    assert trace_dir.exists()
    assert list(trace_dir.iterdir()) == []


def test_runner_trace_reset_falls_back_to_docker_on_permission_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace_root = tmp_path / "traces"
    trace_dir = trace_root / "task-1"
    trace_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_rmtree(path, **kwargs):
        raise PermissionError(str(path / "tool-resource"))

    def fake_which(name: str):
        return "/usr/bin/docker" if name == "docker" else None

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return Result()

    monkeypatch.setattr("swe_rebench.runner.shutil.rmtree", fake_rmtree)
    monkeypatch.setattr("swe_rebench.host_sandbox.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _reset_task_trace_dir(trace_root, trace_dir, docker_cleanup_image="task-image:latest")

    assert calls
    cleanup_cmd = calls[0]
    assert cleanup_cmd[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert f"TARGET={trace_dir.name}" in cleanup_cmd
    assert str(trace_dir.parent.resolve()) + ":/host_parent" in cleanup_cmd
    assert "task-image:latest" in cleanup_cmd


def test_runner_trace_reset_falls_back_to_docker_on_directory_not_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace_root = tmp_path / "traces"
    trace_dir = trace_root / "task-1"
    trace_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_rmtree(path, **kwargs):
        raise OSError(39, "Directory not empty", str(path / "tool-resource"))

    def fake_which(name: str):
        return "/usr/bin/docker" if name == "docker" else None

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return Result()

    monkeypatch.setattr("swe_rebench.runner.shutil.rmtree", fake_rmtree)
    monkeypatch.setattr("swe_rebench.host_sandbox.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_sandbox.subprocess.run", fake_run)

    _reset_task_trace_dir(trace_root, trace_dir, docker_cleanup_image="task-image:latest")

    assert calls
    cleanup_cmd = calls[0]
    assert cleanup_cmd[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert f"TARGET={trace_dir.name}" in cleanup_cmd
    assert str(trace_dir.parent.resolve()) + ":/host_parent" in cleanup_cmd
    assert "task-image:latest" in cleanup_cmd


def test_runner_config_parses_docker_bool_strings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
docker:
  privileged: "false"
  cgroup_mount_rw: "true"
  cgroup_required: "true"
""",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.docker.privileged is False
    assert config.docker.cgroup_mount_rw is True
    assert config.docker.cgroup_required is True


def test_task_artifacts_summarizes_patch_and_result_summary(tmp_path: Path) -> None:
    (tmp_path / "model.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
    (tmp_path / "agent-cwd.txt").write_text("/testbed\n", encoding="utf-8")
    (tmp_path / "agent-stdout.txt").write_text("done\n", encoding="utf-8")
    (tmp_path / "sidecar.log").write_text("ready\n", encoding="utf-8")
    (tmp_path / "sidecar-stderr.txt").write_text("bcc diagnostics\n", encoding="utf-8")
    (tmp_path / "container.log").write_text("container done\n", encoding="utf-8")
    (tmp_path / "tool_resource_preflight.json").write_text('{"stage2_ready": true}\n', encoding="utf-8")
    (tmp_path / "tool_resource_preflight_host.json").write_text('{"bcc_import": {"ok": true}}\n', encoding="utf-8")
    (tmp_path / "result_summary.json").write_text(
        json.dumps({"has_patch": True, "patch_bytes": 19}),
        encoding="utf-8",
    )

    artifacts = _task_artifacts(tmp_path)

    assert artifacts["model.patch"]["has_diff"] is True
    assert artifacts["agent-cwd.txt"]["preview"] == "/testbed\n"
    assert artifacts["agent-stdout.txt"]["preview"] == "done\n"
    assert artifacts["sidecar.log"]["bytes"] == 7
    assert artifacts["sidecar-stderr.txt"]["preview"] == "bcc diagnostics\n"
    assert artifacts["container.log"]["bytes"] == 16
    assert artifacts["tool_resource_preflight.json"]["bytes"] > 0
    assert artifacts["tool_resource_preflight_host.json"]["bytes"] > 0
    assert artifacts["result_summary.json"]["summary"]["has_patch"] is True


def test_reset_task_trace_dir_removes_stale_artifacts(tmp_path: Path) -> None:
    trace_root = tmp_path / "traces"
    trace_dir = trace_root / "task-a"
    trace_dir.mkdir(parents=True)
    (trace_dir / "model.patch").write_text("stale diff\n", encoding="utf-8")

    _reset_task_trace_dir(trace_root, trace_dir)

    assert trace_dir.is_dir()
    assert not (trace_dir / "model.patch").exists()


def test_reset_task_trace_dir_refuses_outside_trace_root(tmp_path: Path) -> None:
    trace_root = tmp_path / "traces"
    outside = tmp_path / "outside-task"

    try:
        _reset_task_trace_dir(trace_root, outside)
    except ValueError as exc:
        assert "outside trace root" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_smoke_summary_reports_no_patch_as_unsuccessful() -> None:
    summary = _smoke_summary(
        {
            "agent-cwd.txt": {"preview": "/testbed\n"},
            "result_summary.json": {
                "summary": {
                    "agent_exit_code": 0,
                    "testbed_exists": True,
                    "patch_bytes": 0,
                    "has_patch": False,
                }
            },
        }
    )

    assert summary["success"] is False
    assert summary["reason"] == "no patch produced"
    assert summary["agent_cwd"] == "/testbed"


def test_runner_config_reads_api_key_from_default_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    key_file = tmp_path / "swe_rebench" / "llm_api_key.txt"
    key_file.parent.mkdir()
    key_file.write_text("sk-real-from-file\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: "${LLM_API_KEY}"
""",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.llm.api_key == "sk-real-from-file"


def test_runner_config_env_api_key_takes_precedence_over_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-real-from-env")
    key_file = tmp_path / "swe_rebench" / "llm_api_key.txt"
    key_file.parent.mkdir()
    key_file.write_text("sk-real-from-file\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: "${LLM_API_KEY}"
""",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.llm.api_key == "sk-real-from-env"


def test_docker_cli_uses_wait_exit_code_with_rm_container(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "run", "--detach"]:
            return Result(stdout="abc123\n")
        if cmd == ["docker", "wait", "abc123"]:
            return Result(stdout="7\n")
        if cmd == ["docker", "logs", "abc123"]:
            return Result(stdout="container output\n")
        if cmd == ["docker", "rm", "-f", "abc123"]:
            return Result(stdout="abc123\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    result = run_container(
        client=None,
        image="image:latest",
        task_id="task-1",
        bundle_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
        env_extra={"AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED": "true"},
    )

    assert result.exit_code == 7
    assert not any(call[:2] == ["docker", "inspect"] for call in calls)
    docker_run = calls[0]
    expected_prefix = sandbox_container_prefix("docker:task-1")
    assert f"AGENT_SCHEDULER_DOCKER_EXEC_CONTAINER_PREFIX={expected_prefix}" in docker_run
    assert "AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=false" in docker_run
    assert "CLAW_CGROUP_REQUIRED=0" in docker_run
    assert (tmp_path / "trace" / "container.log").read_text(encoding="utf-8") == "container output\n"


def test_docker_cli_sets_required_cgroup_only_when_configured(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, stdout: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "run", "--detach"]:
            return Result(stdout="abc123\n")
        if cmd == ["docker", "wait", "abc123"]:
            return Result(stdout="0\n")
        if cmd == ["docker", "logs", "abc123"]:
            return Result(stdout="")
        if cmd == ["docker", "rm", "-f", "abc123"]:
            return Result(stdout="abc123\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
docker:
  cgroup_required: true
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    run_container(
        client=None,
        image="image:latest",
        task_id="task-1",
        bundle_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
        stage2_required=True,
    )

    assert "CLAW_CGROUP_REQUIRED=1" in calls[0]
    assert "AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=true" in calls[0]


def test_require_llm_api_key_reports_default_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: "${LLM_API_KEY}"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    try:
        _require_llm_api_key(config)
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError("expected SystemExit")

    assert "LLM API key is not configured" in message
    assert "swe_rebench" in message
    assert "llm_api_key.txt" in message


def test_entrypoint_generation_does_not_embed_api_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: "sk-secret"
bundle:
  output_dir: "bundle"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    _write_entrypoint(bundle_dir, config)

    assert "sk-secret" not in (bundle_dir / "entrypoint.sh").read_text(encoding="utf-8")


def test_prepare_rebuilds_plugin_dist_after_removing_stale_files(monkeypatch, tmp_path: Path) -> None:
    plugin_dir = tmp_path / "packages" / "openclaw-plugin"
    dist_dir = plugin_dir / "dist"
    dist_dir.mkdir(parents=True)
    (plugin_dir / "package.json").write_text('{"scripts":{"build":"tsc"}}\n', encoding="utf-8")
    (dist_dir / "stale.js").write_text("old runtime\n", encoding="utf-8")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_which(name: str):
        return "/usr/bin/npm" if name == "npm" else None

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs["cwd"] == str(plugin_dir)
        dist_dir.mkdir()
        (dist_dir / "index.js").write_text("fresh runtime\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr("swe_rebench.prepare.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.prepare.subprocess.run", fake_run)

    _build_plugin_dist(tmp_path, bundle_dir, config)

    assert calls == [["/usr/bin/npm", "run", "build"]]
    assert not (dist_dir / "stale.js").exists()
    assert (dist_dir / "index.js").read_text(encoding="utf-8") == "fresh runtime\n"
    assert (bundle_dir / "plugin-build.log").exists()


def test_bundle_stale_check_ignores_dist_but_tracks_source(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "packages" / "openclaw-plugin"
    scheduler_dir = tmp_path / "services" / "scheduler"
    bundle_dir = tmp_path / "bundle"
    (plugin_dir / "src").mkdir(parents=True)
    (plugin_dir / "dist").mkdir()
    scheduler_dir.mkdir(parents=True)
    bundle_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    marker = bundle_dir / "entrypoint.sh"
    marker.write_text("built\n", encoding="utf-8")
    future = marker.stat().st_mtime + 1000
    os.utime(marker, (future, future))
    now = marker.stat().st_mtime
    old = now - 100
    for path in (
        plugin_dir / "src" / "index.ts",
        plugin_dir / "dist" / "index.js",
        scheduler_dir / "pyproject.toml",
    ):
        path.write_text("old\n", encoding="utf-8")
        os.utime(path, (old, old))

    assert bundle_needs_rebuild(config, bundle_dir) is False

    source = plugin_dir / "src" / "index.ts"
    new = now + 100
    os.utime(source, (new, new))

    assert bundle_needs_rebuild(config, bundle_dir) is True
