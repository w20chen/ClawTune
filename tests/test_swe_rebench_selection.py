import builtins
import json
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from swe_rebench.config import AgentConfig, RunnerConfig, _as_str_list, _load_yaml_safe
from swe_rebench.docker import (
    ContainerCleanupError,
    ContainerResult,
    _container_kernel_header_volumes,
    _container_tracefs_volumes,
    local_image_available,
)
from swe_rebench.host_openclaw import (
    KnowledgeBaseSyncError,
    _SANDBOX_TASK_PATH,
    _cleanup_openclaw_sandbox_containers,
    _cleanup_runtime_artifacts,
    _collect_runtime_ebpf_artifacts,
    _configure_openclaw,
    _openclaw_agent_argv,
    _openclaw_uses_agent_flag,
    _docker_sandbox_container_ids,
    _ensure_openclaw_sandbox_image,
    _export_testbed_from_image,
    _ensure_plugin_built,
    _install_sandbox_runtime,
    _make_sandbox_workspace_writable,
    _openclaw_config,
    _openclaw_env,
    _prepare_batch_tool_resource_kb,
    _publish_tool_resource_kb,
    _run_openclaw_agent,
    run_host_openclaw_task,
    _reset_directory,
    _sandbox_container_prefix,
    _seed_runtime_tool_resource_kb,
    _stage_plugin_for_openclaw_if_needed,
    _start_sidecar,
    _verify_sandbox_launcher,
    _verify_sandbox_task_environment,
    _write_host_tool_resource_preflight,
    _write_task_inputs,
)
from swe_rebench.task_source import TaskDef
from swe_rebench.docker import run_container
from swe_rebench.prepare import (
    _ENTRYPOINT_TEMPLATE,
    _PLUGIN_CONFIG,
    _build_plugin_dist,
    _restore_sudo_user_ownership,
    _write_entrypoint,
    _write_runtime_assets_fingerprint,
    _write_plugin_config,
    _write_setup_script,
    runtime_assets_need_rebuild,
)
from swe_rebench.sandbox import sandbox_container_prefix
from swe_rebench.task_source import (
    filter_tasks,
    infer_repo_from_instance_id,
    parse_instance_ids,
    task_repo_key,
    tasks_from_records,
)
from swe_rebench.runner import (
    BatchReport,
    _apply_batch_overrides,
    _apply_runtime_overrides,
    _container_image_ready,
    _default_agent_test_bench_tasks,
    _inspect_trace,
    _print_report_json,
    _resource_summary,
    _run_one,
    _require_llm_api_key,
    _reset_task_trace_dir,
    _smoke_summary,
    _task_artifacts,
    run_batch,
)


def test_container_missing_pull_policy_reuses_matching_cli_image(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "linux/amd64\n", "")

    monkeypatch.setattr("swe_rebench.docker.subprocess.run", fake_run)

    assert local_image_available(None, "task:latest", "linux/amd64") is True
    assert calls == [[
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Os}}/{{.Architecture}}",
        "task:latest",
    ]]


def test_container_cached_image_does_not_contact_registry(monkeypatch) -> None:
    monkeypatch.setattr(
        "swe_rebench.runner.local_image_available",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "swe_rebench.runner.pull_image",
        lambda *_args: pytest.fail("cached image must not be pulled"),
    )

    assert _container_image_ready(None, "task:latest", "missing", "linux/amd64")


def test_container_cached_image_must_match_requested_platform(monkeypatch) -> None:
    class Images:
        @staticmethod
        def get(_image):
            return type("Image", (), {"attrs": {"Os": "linux", "Architecture": "arm64"}})()

    client = type("Client", (), {"images": Images()})()

    assert local_image_available(client, "task:latest", "linux/amd64") is False


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


def test_filter_tasks_sample_32_preserves_dataset_order() -> None:
    tasks = [
        TaskDef(
            instance_id=f"owner__repo-{index}",
            image=f"image:{index}",
            repo="owner/repo",
        )
        for index in range(40)
    ]

    selected = filter_tasks(tasks, sample=32)

    assert [task.instance_id for task in selected] == [
        f"owner__repo-{index}" for index in range(32)
    ]


@pytest.mark.parametrize(
    "instance_id",
    [
        "12rambau__sepal_ui-411",
        "12rambau__sepal_ui-501",
        "12rambau__sepal_ui-516",
    ],
)
def test_task_repo_fallback_groups_instances_from_the_same_repo(
    instance_id: str,
) -> None:
    [task] = tasks_from_records(
        [{"instance_id": instance_id, "docker_image": "image:latest"}]
    )

    assert infer_repo_from_instance_id(instance_id) == "12rambau/sepal_ui"
    assert task.repo == "12rambau/sepal_ui"
    assert task_repo_key(task) == "12rambau/sepal_ui"


def test_task_repo_fallback_handles_hyphenated_repo_names() -> None:
    instance_id = "scikit-learn__scikit-learn-123"

    assert infer_repo_from_instance_id(instance_id) == (
        "scikit-learn/scikit-learn"
    )


def test_task_repo_key_prefers_explicit_repo_and_isolates_unparseable_ids() -> None:
    explicit = TaskDef(
        instance_id="wrong__repo-1",
        image="image:latest",
        repo=" 12rambau/sepal_ui ",
    )
    first_unknown = TaskDef(instance_id="custom-task-a", image="image:latest")
    second_unknown = TaskDef(instance_id="custom-task-b", image="image:latest")

    assert task_repo_key(explicit) == "12rambau/sepal_ui"
    assert task_repo_key(first_unknown) == "instance:custom-task-a"
    assert task_repo_key(second_unknown) == "instance:custom-task-b"
    assert task_repo_key(first_unknown) != task_repo_key(second_unknown)


def test_filter_tasks_matches_repo_inferred_from_instance_id() -> None:
    tasks = tasks_from_records(
        [
            {
                "instance_id": "12rambau__sepal_ui-411",
                "docker_image": "image:411",
            },
            {
                "instance_id": "other__project-1",
                "docker_image": "image:other",
            },
            {
                "instance_id": "12rambau__sepal_ui-501",
                "docker_image": "image:501",
            },
        ]
    )

    selected = filter_tasks(tasks, repo="12rambau/sepal_ui")

    assert [task.instance_id for task in selected] == [
        "12rambau__sepal_ui-411",
        "12rambau__sepal_ui-501",
    ]


def test_default_task_source_prefers_full_sibling_dataset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "ClawTune"
    bundled = repo_root / "swe_rebench" / "tasks.json"
    sibling = (
        tmp_path
        / "agent-test-bench"
        / "data"
        / "swe-rebench"
        / "tasks.json"
    )
    bundled.parent.mkdir(parents=True)
    sibling.parent.mkdir(parents=True)
    bundled.write_text(
        json.dumps(
            [
                {"instance_id": f"smoke__repo-{index}", "image": "smoke"}
                for index in range(4)
            ]
        ),
        encoding="utf-8",
    )
    sibling.write_text(
        json.dumps(
            [
                {"instance_id": f"full__repo-{index}", "image": "full"}
                for index in range(40)
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_TEST_BENCH_ROOT", raising=False)

    selected = _default_agent_test_bench_tasks(repo_root)

    assert selected == sibling


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


def test_runner_refuses_to_silently_undershoot_positive_sample(
    tmp_path: Path,
) -> None:
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
            "--sample",
            "32",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "--sample 32 requires 32 matching tasks" in result.stderr
    assert "AGENT_TEST_BENCH_ROOT" in result.stderr
    assert "Loaded" not in result.stderr


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
                "execution": {"mode": "launcher", "cgroup_path": "/sys/fs/cgroup/clawtune/exec-1"},
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
    assert 'CLAWTUNE_LLM_UPSTREAM_BASE_URL="${LLM_UPSTREAM_BASE_URL:-__UPSTREAM__}"' in _ENTRYPOINT_TEMPLATE
    assert 'CLAWTUNE_LLM_UPSTREAM_API_KEY="${LLM_API_KEY:-__LLM_KEY__}"' in _ENTRYPOINT_TEMPLATE
    assert (
        'CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED="${CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED:-false}"'
        in _ENTRYPOINT_TEMPLATE
    )
    assert 'export OPENCLAW_MODEL_REF="${OPENCLAW_MODEL_REF:-__MODEL_FULL__}"' in _ENTRYPOINT_TEMPLATE
    assert 'task_manifest.json' in _ENTRYPOINT_TEMPLATE
    assert 'agent-cwd.txt' in _ENTRYPOINT_TEMPLATE
    assert 'agent_prompt.txt' in _ENTRYPOINT_TEMPLATE
    assert 'agent-stdout.txt' in _ENTRYPOINT_TEMPLATE
    assert '> >(tee "$TRACE_DIR/agent-stdout.txt")' in _ENTRYPOINT_TEMPLATE
    assert '> >(tee "$TRACE_DIR/agent-stderr.txt" >&2)' in _ENTRYPOINT_TEMPLATE
    assert 'sidecar.log' in _ENTRYPOINT_TEMPLATE
    assert 'model.patch' in _ENTRYPOINT_TEMPLATE
    assert 'result_summary.json' in _ENTRYPOINT_TEMPLATE
    assert 'cgroup_probe.json' in _ENTRYPOINT_TEMPLATE
    assert 'tool_resource_preflight.json' in _ENTRYPOINT_TEMPLATE


def test_swe_rebench_plugin_config_uses_managed_wrapper_cgroup() -> None:
    assert _PLUGIN_CONFIG["executionBackend"] == "managed-wrapper"
    assert _PLUGIN_CONFIG["launcherPath"] == "/opt/clawtune/bin/clawtune-launch"
    assert _PLUGIN_CONFIG["enableCgroup"] is True
    assert _PLUGIN_CONFIG["reportTimeoutMs"] == 10000
    assert _PLUGIN_CONFIG["securityBoundaryAccepted"] is True
    assert _PLUGIN_CONFIG["trace"]["include_raw_events"] is False
    assert _PLUGIN_CONFIG["trace"]["include_raw_events"] is False


def test_entrypoint_installs_stable_launcher_path() -> None:
    assert "cat > /opt/clawtune/bin/clawtune-launch" in _ENTRYPOINT_TEMPLATE
    assert 'export CLAWTUNE_LAUNCHER_PYTHONPATH="/clawtune/sidecar/src"' in _ENTRYPOINT_TEMPLATE
    assert "python3 -m clawtune_sidecar.launcher" in _ENTRYPOINT_TEMPLATE
    assert 'for PIP_NAME in pip pip3; do' in _ENTRYPOINT_TEMPLATE
    assert 'exec "$CLAWTUNE_TASK_PYTHON" -m pip "$@"' in _ENTRYPOINT_TEMPLATE


def test_setup_installs_scheduler_runtime_dependencies() -> None:
    from swe_rebench.prepare import _SETUP_TEMPLATE

    assert (
        "fastapi uvicorn pydantic psutil httpx prometheus-client numpy typing-extensions"
        in _SETUP_TEMPLATE
    )
    assert (
        "import fastapi, uvicorn, pydantic, psutil, numpy, typing_extensions"
        in _SETUP_TEMPLATE
    )
    assert "python3-bpfcc bpfcc-tools libbpfcc libelf1" in _SETUP_TEMPLATE
    assert "clang llvm kmod" in _SETUP_TEMPLATE
    assert 'linux-headers-"$(uname -r)"' in _SETUP_TEMPLATE
    assert "/tmp/.clawtune_bcc_pythonpath" in _SETUP_TEMPLATE
    assert "import bcc" in _SETUP_TEMPLATE
    assert "ensure_compatible_adapter" in _SETUP_TEMPLATE
    assert "with MvdanClient(built_path)" in _SETUP_TEMPLATE
    assert 'SETUP_REVISION="2:mvdan-protocol-3:mvdan-v3.13.1"' in _SETUP_TEMPLATE
    assert 'MVDAN_STATUS="/tmp/.clawtune_mvdan_adapter_status.json"' in _SETUP_TEMPLATE
    assert 'mv -f "$SETUP_DONE.$$" "$SETUP_DONE"' in _SETUP_TEMPLATE
    assert 'touch "$SETUP_DONE"' not in _SETUP_TEMPLATE
    assert _SETUP_TEMPLATE.index("# Detect python:") < _SETUP_TEMPLATE.index(
        'if [ -f "$SETUP_DONE" ]'
    )
    assert _SETUP_TEMPLATE.index("BCC Python binding") < _SETUP_TEMPLATE.index(
        "building/verifying pinned mvdan adapter"
    )
    assert _SETUP_TEMPLATE.index(
        "building/verifying pinned mvdan adapter"
    ) < _SETUP_TEMPLATE.index('mv -f "$SETUP_DONE.$$" "$SETUP_DONE"')
    assert '"source": "docker-unix-socket"' in _ENTRYPOINT_TEMPLATE


def test_setup_repairs_libelf_payload_removed_from_minimized_apt_image() -> None:
    from swe_rebench.prepare import _SETUP_TEMPLATE

    assert 'if [ "$PKG_MGR" = "apt" ]; then' in _SETUP_TEMPLATE
    assert '*"libelf.so.1"*)' in _SETUP_TEMPLATE
    assert "_claw_apt install -y -q --reinstall libelf1" in _SETUP_TEMPLATE
    assert "libelf1 reinstall repaired the BCC runtime" in _SETUP_TEMPLATE
    assert "BCC remains unavailable after container repair probes" in _SETUP_TEMPLATE
    assert "libelf1 reinstall failed (eBPF telemetry will remain unavailable)" in _SETUP_TEMPLATE


def test_container_setup_bounds_and_exposes_apt_network_work() -> None:
    from swe_rebench.prepare import _SETUP_TEMPLATE

    assert "CLAWTUNE_SETUP_COMMAND_TIMEOUT_SECONDS:-300" in _SETUP_TEMPLATE
    assert "Acquire::http::Timeout=20" in _SETUP_TEMPLATE
    assert "Acquire::https::Timeout=20" in _SETUP_TEMPLATE
    assert "refreshing apt metadata" in _SETUP_TEMPLATE
    assert "apt metadata ready" in _SETUP_TEMPLATE
    assert "apt-get update -qq" not in _SETUP_TEMPLATE


def test_container_bcc_repair_scopes_system_libstdcxx_to_sidecar() -> None:
    from swe_rebench.prepare import _ENTRYPOINT_TEMPLATE, _SETUP_TEMPLATE

    assert '*"libstdc++.so.6"*GLIBCXX_*"not found"*)' in _SETUP_TEMPLATE
    assert "ldconfig -p" in _SETUP_TEMPLATE
    assert '$1 == "libstdc++.so.6"' in _SETUP_TEMPLATE
    assert "/tmp/.clawtune_bcc_ld_preload" in _SETUP_TEMPLATE
    assert 'rm -f -- "$_CLAWTUNE_BCC_PRELOAD_FILE" || true' in _SETUP_TEMPLATE
    assert 'chmod 0600 "$_CLAWTUNE_BCC_PRELOAD_FILE"' in _SETUP_TEMPLATE
    assert "sidecar deps and BCC OK with system libstdc++" in _SETUP_TEMPLATE
    assert "disabling the eBPF preload" in _SETUP_TEMPLATE
    assert "export LD_PRELOAD" not in _SETUP_TEMPLATE
    assert "LD_LIBRARY_PATH" not in _SETUP_TEMPLATE

    assert "CLAWTUNE_BCC_RUNTIME_ENV=()" in _ENTRYPOINT_TEMPLATE
    assert _ENTRYPOINT_TEMPLATE.count(
        'env "${CLAWTUNE_BCC_RUNTIME_ENV[@]}"'
    ) == 2
    assert '"bcc_ld_preload": os.environ.get("LD_PRELOAD")' in _ENTRYPOINT_TEMPLATE
    assert "export LD_PRELOAD" not in _ENTRYPOINT_TEMPLATE
    launcher = _ENTRYPOINT_TEMPLATE.split(
        "cat > /opt/clawtune/bin/clawtune-launch <<'EOF_LAUNCHER'\n", 1
    )[1].split("\nEOF_LAUNCHER", 1)[0]
    assert "LD_PRELOAD" not in launcher


def test_entrypoint_writer_renders_default_config(tmp_path: Path) -> None:
    config = RunnerConfig.from_yaml(
        Path(__file__).parents[1] / "swe_rebench" / "config.yaml"
    )
    _write_entrypoint(tmp_path, config)
    entrypoint_path = tmp_path / "entrypoint.sh"
    entrypoint = entrypoint_path.read_text(encoding="utf-8")

    assert config.llm.openclaw_model_ref in entrypoint
    assert "__MODEL_FULL__" not in entrypoint
    assert os.access(entrypoint_path, os.X_OK)


def test_setup_resolves_current_node_archive_for_detected_architecture() -> None:
    from swe_rebench.prepare import _SETUP_TEMPLATE

    assert "node-v24.15.0" not in _SETUP_TEMPLATE
    assert 'NODE_BASE_URL="https://nodejs.org/dist/latest-v24.x"' in _SETUP_TEMPLATE
    assert 'awk -v arch="$NODE_ARCH"' in _SETUP_TEMPLATE
    assert "no Node.js 24 archive for architecture $NODE_ARCH" in _SETUP_TEMPLATE


def test_setup_writer_emits_template(tmp_path: Path) -> None:
    from swe_rebench.prepare import _SETUP_TEMPLATE

    _write_setup_script(tmp_path)
    setup_script = tmp_path / "setup.sh"

    assert setup_script.read_text(encoding="utf-8") == _SETUP_TEMPLATE
    assert os.access(setup_script, os.X_OK)


def test_docker_runner_config_sets_sandbox_container_prefix_placeholder(tmp_path: Path) -> None:
    _write_plugin_config(tmp_path)

    parsed = json.loads((tmp_path / "openclaw-config.json5").read_text(encoding="utf-8"))
    agent_defaults = parsed["agents"]["defaults"]
    docker_cfg = parsed["agents"]["defaults"]["sandbox"]["docker"]

    assert agent_defaults["workspace"] == "/testbed"
    assert agent_defaults["repoRoot"] == "/testbed"
    assert "tools" not in agent_defaults
    assert parsed["tools"]["exec"]["pathPrepend"][0] == "/opt/clawtune/bin"
    assert docker_cfg["containerPrefix"] == "__SANDBOX_CONTAINER_PREFIX__"
    assert parsed["env"]["CLAWTUNE_EXEC_WORKDIR"] == "/testbed"
    assert parsed["env"]["CLAWTUNE_ENDPOINT"] == "http://127.0.0.1:8765"
    plugin_entry = parsed["plugins"]["entries"]["clawtune"]
    assert plugin_entry["hooks"] == {"allowConversationAccess": True}
    assert "hooks" not in plugin_entry["config"]
    assert "__SANDBOX_CONTAINER_PREFIX__" in _ENTRYPOINT_TEMPLATE
    assert "openclaw config patch --stdin" in _ENTRYPOINT_TEMPLATE


def test_openclaw_config_writer_emits_expected_config(tmp_path: Path) -> None:
    _write_plugin_config(tmp_path)

    generated = json.loads(
        (tmp_path / "openclaw-config.json5").read_text(encoding="utf-8")
    )
    plugin = generated["plugins"]["entries"]["clawtune"]

    assert plugin["enabled"] is True
    assert plugin["config"] == _PLUGIN_CONFIG


def test_entrypoint_exports_container_runtime_identity_for_launcher() -> None:
    assert 'export CLAWTUNE_ENDPOINT="http://127.0.0.1:$SIDECAR_PORT"' in _ENTRYPOINT_TEMPLATE
    assert 'export CLAWTUNE_EXEC_WORKDIR="/testbed"' in _ENTRYPOINT_TEMPLATE
    assert 'export CLAWTUNE_TASK_PYTHON="$_CLW_PYTHON"' in _ENTRYPOINT_TEMPLATE
    assert 'export PATH="/opt/clawtune/bin:$(dirname "$CLAWTUNE_TASK_PYTHON"):$PATH"' in _ENTRYPOINT_TEMPLATE
    assert "CLAWTUNE_SANDBOX_CONTAINER_ID" in _ENTRYPOINT_TEMPLATE
    assert "CLAWTUNE_SANDBOX_CONTAINER_ID" in _ENTRYPOINT_TEMPLATE
    assert '"container_id": os.environ.get("CLAWTUNE_SANDBOX_CONTAINER_ID")' in _ENTRYPOINT_TEMPLATE
    assert "tool_resource_preflight.json" in _ENTRYPOINT_TEMPLATE
    assert '"ebpf_ready"' in _ENTRYPOINT_TEMPLATE
    assert '"clang": shutil.which("clang")' in _ENTRYPOINT_TEMPLATE
    assert '"tracefs": tracefs' in _ENTRYPOINT_TEMPLATE
    assert '"mvdan_adapter": mvdan_adapter' in _ENTRYPOINT_TEMPLATE
    assert "with mvdan_client.MvdanClient(binary_path)" in _ENTRYPOINT_TEMPLATE
    assert 'mvdan_adapter.get("ok") is True' in _ENTRYPOINT_TEMPLATE
    assert "required eBPF preflight failed" in _ENTRYPOINT_TEMPLATE
    assert 'candidate / "events/sched/sched_process_exit/id"' in _ENTRYPOINT_TEMPLATE
    assert 'tracefs.get("sched_process_exit") is True' in _ENTRYPOINT_TEMPLATE
    assert 'tracefs.get("kprobe_events_writable") is True' in _ENTRYPOINT_TEMPLATE
    preflight_end = _ENTRYPOINT_TEMPLATE.index(
        "print(json.dumps(preflight, indent=2))"
    )
    required_gate = _ENTRYPOINT_TEMPLATE.index(
        'case "${CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED,,}"'
    )
    sidecar_start = _ENTRYPOINT_TEMPLATE.index(
        '"$_CLW_PYTHON" -m clawtune_sidecar.main'
    )
    assert preflight_end < required_gate < sidecar_start


def test_runner_config_enables_complete_cgroup_sampling() -> None:
    config = RunnerConfig.from_yaml("swe_rebench/config.yaml")

    assert config.runtime.mode == "host-openclaw"
    assert config.runtime.ebpf_required is True
    assert config.docker.privileged is True
    assert config.docker.cgroupns_mode == "host"
    assert config.docker.cgroup_mount_rw is True
    assert config.docker.cgroup_required is True


def test_runner_config_parses_host_openclaw_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "host-openclaw"
""",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.runtime.mode == "host-openclaw"


def test_runner_applies_host_openclaw_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'runtime:\n  mode: "host-openclaw"\n',
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    assert config.runtime.mode == "host-openclaw"

    config.runtime.ebpf_required = False
    _apply_runtime_overrides(
        config,
        runtime_mode="host-openclaw",
        ebpf_required=None,
    )
    assert config.runtime.mode == "host-openclaw"
    assert config.runtime.ebpf_required is True


def test_runner_reads_ebpf_required(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  ebpf_required: true\n",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.runtime.ebpf_required is True


def test_cli_host_openclaw_override_requires_ebpf_by_default(tmp_path: Path) -> None:
    config = RunnerConfig.from_yaml("swe_rebench/config.yaml", repo_root=tmp_path)

    _apply_runtime_overrides(
        config,
        runtime_mode="host-openclaw",
        ebpf_required=None,
    )

    assert config.runtime.mode == "host-openclaw"
    assert config.runtime.ebpf_required is True

    _apply_runtime_overrides(
        config,
        runtime_mode="host-openclaw",
        ebpf_required=False,
    )
    assert config.runtime.ebpf_required is False


def test_cli_task_timeout_override(tmp_path: Path) -> None:
    config = RunnerConfig.from_yaml("swe_rebench/config.yaml", repo_root=tmp_path)

    _apply_batch_overrides(config, task_timeout_seconds=600)
    assert config.batch.task_timeout_seconds == 600

    _apply_batch_overrides(config, task_timeout_seconds=0)
    assert config.batch.task_timeout_seconds == 0

    _apply_batch_overrides(config, agent_timeout_seconds=90)
    assert config.batch.agent_timeout_seconds == 90

    _apply_batch_overrides(config, agent_timeout_seconds=0)
    assert config.batch.agent_timeout_seconds == 0


def test_cli_task_timeout_override_rejects_negative_value(tmp_path: Path) -> None:
    config = RunnerConfig.from_yaml("swe_rebench/config.yaml", repo_root=tmp_path)

    with pytest.raises(ValueError, match="must be >= 0"):
        _apply_batch_overrides(config, task_timeout_seconds=-1)

    with pytest.raises(ValueError, match="agent-timeout-seconds must be >= 0"):
        _apply_batch_overrides(config, agent_timeout_seconds=-1)


def test_runner_config_reads_separate_task_and_agent_timeouts(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "batch:\n  task_timeout_seconds: 600\n  agent_timeout_seconds: 420\n",
        encoding="utf-8",
    )

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.batch.task_timeout_seconds == 600
    assert config.batch.agent_timeout_seconds == 420


def test_full_report_json_is_opt_in(capsys) -> None:
    report = BatchReport(config_path="config.yaml", total_tasks=1, completed=1)

    _print_report_json(report, enabled=False)
    assert capsys.readouterr().out == ""

    _print_report_json(report, enabled=True)
    output = capsys.readouterr().out
    assert json.loads(output)["completed"] == 1


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


def test_run_one_dispatches_to_host_openclaw_runtime(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "host-openclaw"
output:
  trace_root: "traces"
  report_path: "report.json"
runtime_assets:
  output_dir: "assets"
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

    monkeypatch.setattr(runner, "run_host_openclaw_task", fake_host_runner)
    monkeypatch.setattr(
        runner,
        "run_container",
        lambda **kwargs: pytest.fail("host runtime dispatched into container-openclaw"),
    )

    result = _run_one(
        client=object(),
        task=task,
        runtime_assets_dir=tmp_path / "assets",
        trace_dir=trace_dir,
        config=config,
    )

    assert result.exit_code == 0
    assert called["task"] == task
    assert called["config"] == config


def test_run_one_passes_shared_kb_through_host_runtime_and_publishes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "host-openclaw"
output:
  trace_root: "traces"
  report_path: "report.json"
runtime_assets:
  output_dir: "assets"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(
        instance_id="12rambau__sepal_ui-411",
        image="image:latest",
    )
    trace_dir = tmp_path / "traces" / task.instance_id
    shared_kb_dir = tmp_path / "kb-batches" / "batch-1"
    _write_test_kb_pair(shared_kb_dir, "shared-before-task")
    called: dict[str, object] = {}

    def fake_host_runner(**kwargs):
        called.update(kwargs)
        # In host-openclaw mode, _run_one delegates KB seeding to
        # run_host_openclaw_task (manage_sidecar=True path).  The fake
        # must seed so that the subsequent publish has valid input.
        _seed_runtime_tool_resource_kb(
            trace_dir,
            config,
            source_dir=shared_kb_dir,
        )
        _write_test_kb_pair(trace_dir / "tool-resource", "task-update")
        return ContainerResult(
            task_id=task.instance_id,
            image=task.image,
            exit_code=0,
            trace_dir=trace_dir,
        )

    import swe_rebench.runner as runner

    monkeypatch.setattr(runner, "run_host_openclaw_task", fake_host_runner)
    monkeypatch.setattr(
        runner,
        "run_container",
        lambda **kwargs: pytest.fail("host runtime dispatched into container-openclaw"),
    )

    result = _run_one(
        client=object(),
        task=task,
        runtime_assets_dir=tmp_path / "assets",
        trace_dir=trace_dir,
        config=config,
        shared_kb_dir=shared_kb_dir,
    )

    assert result.exit_code == 0
    assert called["shared_kb_dir"] == shared_kb_dir
    # _run_one publishes KB after a per-task sidecar run (shared_sidecar_port
    # is None → should_publish is True).
    assert _kb_pair_markers(shared_kb_dir) == {
        "runtime-tool-resource-kb.json": "task-update",
        "clause-resource-kb.json": "task-update",
        "clause-lattice-time-kb.json": "task-update",
    }


def test_run_one_does_not_publish_when_task_writer_exit_is_unconfirmed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw\n"
        "output:\n  trace_root: traces\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="owner__repo-1", image="image:latest")
    trace_dir = tmp_path / "traces" / task.instance_id
    shared_kb_dir = tmp_path / "kb-batches" / "batch-1"
    _write_test_kb_pair(shared_kb_dir, "last-good")

    def fail_without_confirming_cleanup(**_kwargs):
        _write_test_kb_pair(trace_dir / "tool-resource", "unsafe-update")
        raise ContainerCleanupError("writer exit unconfirmed")

    import swe_rebench.runner as runner

    monkeypatch.setattr(
        runner,
        "run_host_openclaw_task",
        fail_without_confirming_cleanup,
    )

    with pytest.raises(ContainerCleanupError, match="writer exit unconfirmed"):
        _run_one(
            client=object(),
            task=task,
            runtime_assets_dir=tmp_path / "assets",
            trace_dir=trace_dir,
            config=config,
            shared_kb_dir=shared_kb_dir,
        )

    assert _kb_pair_markers(shared_kb_dir) == {
        "runtime-tool-resource-kb.json": "last-good",
        "clause-resource-kb.json": "last-good",
        "clause-lattice-time-kb.json": "last-good",
    }


def test_host_openclaw_propagates_unconfirmed_agent_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw\n"
        "batch:\n  task_timeout_seconds: 0\n"
        "output:\n  trace_root: traces\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="owner__repo-1", image="image:latest")
    trace_dir = tmp_path / "traces" / task.instance_id

    no_op_names = (
        "_write_host_tool_resource_preflight",
        "_export_testbed_from_image",
        "_make_sandbox_workspace_writable",
        "_install_sandbox_runtime",
        "_write_task_inputs",
        "_ensure_openclaw_sandbox_image",
        "_verify_sandbox_launcher",
        "_verify_sandbox_task_environment",
        "_seed_runtime_tool_resource_kb",
        "_configure_openclaw",
        "_cleanup_openclaw_sandbox_containers",
    )
    for name in no_op_names:
        monkeypatch.setattr(
            f"swe_rebench.host_openclaw.{name}",
            lambda *_args, **_kwargs: None,
        )

    class StoppedSidecar:
        def poll(self):
            return 0

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._start_sidecar",
        lambda **_kwargs: StoppedSidecar(),
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._run_openclaw_agent",
        lambda **_kwargs: (_ for _ in ()).throw(
            ContainerCleanupError("agent exit unconfirmed")
        ),
    )

    with pytest.raises(ContainerCleanupError, match="agent exit unconfirmed"):
        run_host_openclaw_task(
            task=task,
            trace_dir=trace_dir,
            config=config,
            runtime_assets_dir=tmp_path / "assets",
        )


def test_shared_sidecar_trace_snapshot_survives_drain_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw\n"
        "batch:\n  task_timeout_seconds: 0\n"
        "output:\n  trace_root: traces\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="owner__repo-1", image="image:latest")
    trace_dir = tmp_path / "traces" / task.instance_id
    shared_trace_dir = tmp_path / "shared-sidecar"
    shared_trace_dir.mkdir()

    import swe_rebench.host_openclaw as host_openclaw

    workspace = host_openclaw._task_workspace(config, task)
    runtime_id = host_openclaw._runtime_id(workspace)
    source = shared_trace_dir / f"{runtime_id}__session_run.jsonl"
    source.write_bytes(
        b'{"schema_version":6,"record_type":"trace_metadata"}\n'
        b'{"schema_version":6,"record_type":"span_start"}'
    )

    for name in (
        "_write_host_tool_resource_preflight",
        "_reset_directory",
        "_export_testbed_from_image",
        "_make_sandbox_workspace_writable",
        "_install_sandbox_runtime",
        "_write_task_inputs",
        "_verify_sandbox_launcher",
        "_verify_sandbox_task_environment",
        "_configure_openclaw",
        "_cleanup_openclaw_sandbox_containers",
        "_cleanup_runtime_artifacts",
        "_collect_patch",
        "_write_result_summary",
    ):
        monkeypatch.setattr(
            f"swe_rebench.host_openclaw.{name}",
            lambda *_args, **_kwargs: None,
        )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._ensure_openclaw_sandbox_image",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._run_openclaw_agent",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._drain_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("runtime drain failed")
        ),
    )
    deleted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._delete_runtime_scope",
        lambda port, identity, **_kwargs: deleted.append((port, identity)),
    )

    with pytest.raises(RuntimeError, match="runtime drain failed"):
        run_host_openclaw_task(
            task=task,
            trace_dir=trace_dir,
            config=config,
            runtime_assets_dir=tmp_path / "assets",
            sidecar_port=19090,
            shared_sidecar_trace_dir=shared_trace_dir,
            manage_sidecar=False,
        )

    task_label = task.instance_id.replace("/", "_").replace(":", "_")
    destination = trace_dir / f"{task_label}__{source.name}"
    assert destination.read_bytes() == (
        b'{"schema_version":6,"record_type":"trace_metadata"}\n'
    )
    assert deleted == [(19090, runtime_id)]


def test_collect_runtime_ebpf_artifacts_copies_only_referenced_clause_files(
    tmp_path: Path,
) -> None:
    shared_kb_dir = tmp_path / "shared-kb"
    shared_kb_dir.mkdir()
    trace_dir = tmp_path / "task-trace"
    trace_dir.mkdir()

    clause = {
        "mode": "clause",
        "schema": "clause_telemetry_v2",
        "calls": [],
    }
    (shared_kb_dir / "exec-good.json").write_text(
        json.dumps(clause),
        encoding="utf-8",
    )
    (shared_kb_dir / "exec-corrupt.json").write_text(
        "{not-json\n",
        encoding="utf-8",
    )
    # exec-missing.json is referenced by the trace but deliberately absent
    # from the shared KB directory; collection must skip it.
    (shared_kb_dir / "exec-nonclause.json").write_text(
        json.dumps({"mode": "other"}),
        encoding="utf-8",
    )
    (shared_kb_dir / "clause-resource-kb.json").write_text(
        '{"snapshot": true}',
        encoding="utf-8",
    )

    trace = trace_dir / "runtime.jsonl"
    lines = []
    for name in (
        "exec-good.json",
        "exec-corrupt.json",
        "exec-missing.json",
        "exec-nonclause.json",
        "clause-resource-kb.json",
    ):
        lines.append(
            json.dumps(
                {
                    "record_type": "span_end",
                    "execution": {
                        "execution_id": name.removesuffix(".json"),
                        "tool_resource": {
                            "artifact_path": str(shared_kb_dir / name),
                        },
                    },
                }
            )
        )
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")

    copied = _collect_runtime_ebpf_artifacts(shared_kb_dir, trace_dir)

    artifact_dir = trace_dir / "tool-resource"
    assert [path.name for path in copied] == ["exec-good.json"]
    assert (
        json.loads((artifact_dir / "exec-good.json").read_text(encoding="utf-8"))
        == clause
    )
    # Corrupt payloads, non-clause payloads, and missing sources are skipped.
    for name in ("exec-corrupt.json", "exec-missing.json", "exec-nonclause.json"):
        assert not (artifact_dir / name).exists()
    # An unreferenced shared-KB snapshot is never copied into a task trace.
    assert not (artifact_dir / "clause-resource-kb.json").exists()


def test_collect_runtime_ebpf_artifacts_noop_without_shared_kb(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "task-trace"
    trace_dir.mkdir()
    (trace_dir / "runtime.jsonl").write_text('{"x": 1}\n', encoding="utf-8")

    copied = _collect_runtime_ebpf_artifacts(None, trace_dir)

    assert copied == []
    assert not (trace_dir / "tool-resource").exists()


def test_shared_sidecar_ebpf_artifacts_collected_into_task_trace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw\n"
        "batch:\n  task_timeout_seconds: 0\n"
        "output:\n  trace_root: traces\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(instance_id="owner__repo-1", image="image:latest")
    trace_dir = tmp_path / "traces" / task.instance_id
    shared_trace_dir = tmp_path / "shared-sidecar"
    shared_trace_dir.mkdir()
    shared_kb_dir = tmp_path / "shared-kb"
    shared_kb_dir.mkdir()

    import swe_rebench.host_openclaw as host_openclaw

    workspace = host_openclaw._task_workspace(config, task)
    runtime_id = host_openclaw._runtime_id(workspace)
    artifact = {
        "mode": "clause",
        "schema": "clause_telemetry_v2",
        "execution_id": "exec-abc123",
        "calls": [
            {
                "tool_call_id": "call-1",
                "tool_trace_ref": "call-1",
            }
        ],
    }
    (shared_kb_dir / "exec-abc123.json").write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )
    (shared_kb_dir / "clause-resource-kb.json").write_text(
        '{"snapshot": "unreferenced"}',
        encoding="utf-8",
    )
    source = shared_trace_dir / f"{runtime_id}__session_run.jsonl"
    source.write_text(
        json.dumps({"schema_version": 6, "record_type": "trace_metadata"})
        + "\n"
        + json.dumps(
            {
                "record_type": "span_end",
                "execution": {
                    "execution_id": "exec-abc123",
                    "tool_resource": {
                        "artifact_path": str(
                            shared_kb_dir / "exec-abc123.json"
                        ),
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    for name in (
        "_write_host_tool_resource_preflight",
        "_reset_directory",
        "_export_testbed_from_image",
        "_make_sandbox_workspace_writable",
        "_install_sandbox_runtime",
        "_write_task_inputs",
        "_verify_sandbox_launcher",
        "_verify_sandbox_task_environment",
        "_configure_openclaw",
        "_cleanup_openclaw_sandbox_containers",
        "_cleanup_runtime_artifacts",
        "_collect_patch",
        "_write_result_summary",
    ):
        monkeypatch.setattr(
            f"swe_rebench.host_openclaw.{name}",
            lambda *_args, **_kwargs: None,
        )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._ensure_openclaw_sandbox_image",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._run_openclaw_agent",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._drain_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._delete_runtime_scope",
        lambda *_args, **_kwargs: None,
    )

    result = run_host_openclaw_task(
        task=task,
        trace_dir=trace_dir,
        config=config,
        runtime_assets_dir=tmp_path / "assets",
        sidecar_port=19090,
        shared_kb_dir=shared_kb_dir,
        shared_sidecar_trace_dir=shared_trace_dir,
        manage_sidecar=False,
    )

    assert result.exit_code == 0
    collected = trace_dir / "tool-resource" / "exec-abc123.json"
    assert json.loads(collected.read_text(encoding="utf-8")) == artifact
    assert not (trace_dir / "tool-resource" / "clause-resource-kb.json").exists()
    task_label = task.instance_id.replace("/", "_").replace(":", "_")
    assert (trace_dir / f"{task_label}__{source.name}").exists()


def test_export_traces_does_not_double_prefix_case_labeled_traces(
    tmp_path: Path,
) -> None:
    from swe_rebench.runner import BatchReport, _export_traces

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "output:\n  flat_export_dir: export\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    export_dir = config.output.flat_export_dir
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()

    task_id = "owner__repo-1"
    # Collected host-openclaw trace already carries the case label:
    # <task_id>__<runtime_id>__<session>_<run>__<digest>.jsonl
    labeled = trace_dir / f"{task_id}__clawtune-srb-abc__s1_r1__digest.jsonl"
    labeled.write_text('{"schema_version": 6}\n', encoding="utf-8")
    # Container-mode trace: unattributed basename, still needs the prefix.
    unattributed = trace_dir / "clawtune-srb-xyz__s2_r2__digest.jsonl"
    unattributed.write_text('{"schema_version": 6}\n', encoding="utf-8")

    report = BatchReport(
        config_path="config.yaml",
        total_tasks=1,
        results=[
            {
                "task_id": task_id,
                "trace_files": [str(labeled), str(unattributed)],
            }
        ],
    )

    _export_traces(config, report)

    assert (
        export_dir / f"{task_id}__clawtune-srb-abc__s1_r1__digest.jsonl"
    ).exists()
    # Unattributed traces still get the task id prefixed.
    assert (
        export_dir / f"{task_id}_clawtune-srb-xyz__s2_r2__digest.jsonl"
    ).exists()
    # No redundant "<task_id>_<task_id>__" double prefix.
    assert not (
        export_dir / f"{task_id}_{task_id}__clawtune-srb-abc__s1_r1__digest.jsonl"
    ).exists()



def test_run_one_propagates_container_ebpf_fallback_mode(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  mode: "container-openclaw"
  ebpf_required: false
output:
  trace_root: "traces"
  report_path: "report.json"
runtime_assets:
  output_dir: "assets"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(
        instance_id="task-1",
        image="image:latest",
        problem_statement="fix",
        repo="12rambau/sepal_ui",
        extra_env={"CLAWTUNE_TOOL_RESOURCE_REPO": "wrong/repo"},
    )
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
        runtime_assets_dir=tmp_path / "assets",
        trace_dir=trace_dir,
        config=config,
    )

    assert result.exit_code == 0
    assert called["ebpf_required"] is False
    env_extra = called["env_extra"]
    assert isinstance(env_extra, dict)
    assert env_extra["CLAWTUNE_TOOL_RESOURCE_REPO"] == (
        "12rambau/sepal_ui"
    )


def test_host_openclaw_openclaw_config_uses_only_public_top_level_keys(tmp_path: Path) -> None:
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

    assert set(parsed) == {"agents", "tools", "plugins", "env"}
    assert parsed["agents"]["defaults"]["workspace"] == str(tmp_path / "workspace")
    assert parsed["agents"]["defaults"]["repoRoot"] == str(tmp_path / "workspace")
    docker_cfg = parsed["agents"]["defaults"]["sandbox"]["docker"]
    assert docker_cfg["containerPrefix"] == _sandbox_container_prefix(tmp_path / "workspace")
    assert docker_cfg["workdir"] == "/workspace"
    assert docker_cfg["extraHosts"] == ["host.docker.internal:host-gateway"]
    assert "binds" not in docker_cfg
    assert parsed["agents"]["defaults"]["sandbox"]["workspaceAccess"] == "rw"
    plugin_entry = parsed["plugins"]["entries"]["clawtune"]
    assert plugin_entry["hooks"] == {"allowConversationAccess": True}
    assert "hooks" not in plugin_entry["config"]
    plugin_cfg = plugin_entry["config"]
    assert plugin_cfg["logLevel"] == "warn"
    assert plugin_cfg["reportTimeoutMs"] == 10000
    assert plugin_cfg["autoStartSidecar"] is False
    assert plugin_cfg["sidecarCommand"] == ""
    assert parsed["env"]["CLAWTUNE_EXEC_WORKDIR"] == "/workspace"
    assert parsed["env"]["CLAWTUNE_SANDBOX_HOST_WORKSPACE"] == str(tmp_path / "workspace")
    assert parsed["env"]["CLAWTUNE_SANDBOX_CONTAINER_WORKSPACE"] == "/workspace"
    assert parsed["env"]["CLAWTUNE_ENABLE_CGROUP"] == "1"
    assert parsed["env"]["CLAWTUNE_CGROUP_REQUIRED"] == "0"
    assert parsed["env"]["CLAWTUNE_LAUNCH_MODE"] == "fork-exec"
    assert "PATH" not in parsed["env"]
    assert parsed["tools"]["deny"] == ["process"]
    assert parsed["tools"]["exec"]["pathPrepend"] == _SANDBOX_TASK_PATH.split(":")


def test_host_openclaw_openclaw_config_omits_unsupported_docker_platform(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "docker:\n  platform: linux/amd64\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    raw = _openclaw_config(
        endpoint_host="http://127.0.0.1:8765",
        endpoint_sandbox="http://host.docker.internal:8765",
        workspace=tmp_path / "workspace",
        config=config,
    )
    docker_cfg = json.loads(raw)["agents"]["defaults"]["sandbox"]["docker"]

    assert "platform" not in docker_cfg


def test_host_openclaw_propagates_required_cgroup_to_sandbox(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "docker:\n  cgroup_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    workspace = tmp_path / "workspace"
    parsed = json.loads(
        _openclaw_config(
            endpoint_host="http://127.0.0.1:8765",
            endpoint_sandbox="http://host.docker.internal:8765",
            workspace=workspace,
            config=config,
        )
    )

    assert parsed["env"]["CLAWTUNE_CGROUP_REQUIRED"] == "1"
    assert (
        _openclaw_env(tmp_path / "home", 8765, config, workspace)[
            "CLAWTUNE_CGROUP_REQUIRED"
        ]
        == "1"
    )


def test_host_openclaw_container_prefix_is_stable_and_workspace_scoped(tmp_path: Path) -> None:
    first = _sandbox_container_prefix(tmp_path / "workspace-a")
    second = _sandbox_container_prefix(tmp_path / "workspace-a")
    other = _sandbox_container_prefix(tmp_path / "workspace-b")

    assert first == second
    assert first != other
    assert first.startswith("clawtune-srb-")
    assert first.endswith("-")
    assert len(first) < 32


def test_host_openclaw_cleanup_removes_prefixed_openclaw_containers(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("swe_rebench.host_openclaw._require_executable", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _cleanup_openclaw_sandbox_containers(trace_dir, workspace)

    assert calls[0][0] == ["/usr/bin/docker", "ps", "-aq", "--filter", f"name={prefix}"]
    assert calls[1][0] == ["/usr/bin/docker", "rm", "-f", "abc123", "def456"]
    log = (trace_dir / "sandbox-container-cleanup.log").read_text(encoding="utf-8")
    assert prefix in log
    assert "abc123" in log


def test_host_openclaw_discovers_sandbox_container_by_docker_prefix(monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123def456\nnot a container id!\n", stderr="")

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    assert _docker_sandbox_container_ids("/usr/bin/docker", "clawtune-srb-test") == ["abc123def456"]
    assert calls[0][0] == ["/usr/bin/docker", "ps", "-q", "--filter", "name=clawtune-srb-test"]


def test_host_openclaw_openclaw_env_points_workspace_dir_at_task_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("CLAWTUNE_TRACE_DIR", "/tmp/plugin-should-not-write")
    monkeypatch.setenv("CLAWTUNE_PLUGIN_TRACE_DIR", "/tmp/plugin-fallback")
    monkeypatch.setenv("CLAWTUNE_AUTO_START_SIDECAR", "true")
    monkeypatch.setenv("CLAWTUNE_SIDECAR_COMMAND", "start-stale-sidecar")
    monkeypatch.setenv("CLAWTUNE_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.setenv("CLAWTUNE_LAUNCHER_ENDPOINT", "http://stale.invalid:9999")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "stale-token")
    monkeypatch.setenv("OPENCLAW_GATEWAY_PASSWORD", "stale-password")
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:18789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_AUTH_TOKEN", "stale-auth-token")

    env = _openclaw_env(tmp_path / "home", 8765, config, workspace)

    assert env["OPENCLAW_WORKSPACE_DIR"] == str(workspace)
    assert env["CLAWTUNE_EXEC_WORKDIR"] == "/workspace"
    assert env["CLAWTUNE_SANDBOX_HOST_WORKSPACE"] == str(workspace)
    assert env["CLAWTUNE_SANDBOX_CONTAINER_WORKSPACE"] == "/workspace"
    assert env["CLAWTUNE_ENABLE_CGROUP"] == "1"
    assert env["CLAWTUNE_CGROUP_REQUIRED"] == "0"
    assert env["CLAWTUNE_LAUNCH_MODE"] == "fork-exec"
    assert env["CLAWTUNE_GATEWAY_ID"] == "swe-rebench"
    assert env["CLAWTUNE_RUNTIME_ID"].startswith("clawtune-srb-")
    assert "CLAWTUNE_TRACE_DIR" not in env
    assert "CLAWTUNE_PLUGIN_TRACE_DIR" not in env
    assert "CLAWTUNE_AUTO_START_SIDECAR" not in env
    assert "CLAWTUNE_SIDECAR_COMMAND" not in env
    assert env["CLAWTUNE_ENDPOINT"] == "http://127.0.0.1:8765"
    assert env["CLAWTUNE_LAUNCHER_ENDPOINT"] == "http://host.docker.internal:8765"
    assert "OPENCLAW_GATEWAY_TOKEN" not in env
    assert "OPENCLAW_GATEWAY_PASSWORD" not in env
    assert "OPENCLAW_GATEWAY_URL" not in env
    assert "OPENCLAW_GATEWAY_AUTH_TOKEN" not in env


def test_host_openclaw_openclaw_env_uses_platform_and_local_proxy_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "llm:\n  api_key: sk-upstream-secret\n"
        "docker:\n  platform: linux/amd64\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    monkeypatch.setenv("CLAWTUNE_LLM_UPSTREAM_API_KEY", "stale-secret")

    env = _openclaw_env(
        tmp_path / "home",
        8765,
        config,
        tmp_path / "workspace",
    )

    assert env["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"
    assert env["VLLM_API_KEY"].startswith("clawtune-runtime.clawtune-srb-")
    assert env["LLM_API_KEY"] == env["VLLM_API_KEY"]
    assert "sk-upstream-secret" not in env.values()
    assert "stale-secret" not in env.values()


def test_host_openclaw_onboard_does_not_put_upstream_key_in_argv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "llm:\n  api_key: sk-upstream-secret\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda _name: "openclaw",
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._ensure_plugin_built",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._stage_plugin_for_openclaw_if_needed",
        lambda **kwargs: kwargs["plugin_dir"],
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._run_logged",
        lambda command, *_args: commands.append(list(command)),
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    _configure_openclaw(
        trace_dir=trace_dir,
        openclaw_home=tmp_path / "home",
        sidecar_port=8765,
        workspace=tmp_path / "workspace",
        config=config,
    )

    argv = [item for command in commands for item in command]
    assert "sk-upstream-secret" not in argv
    assert any(
        item.startswith("clawtune-runtime.clawtune-srb-") for item in argv
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented on Windows")
def test_host_openclaw_launcher_is_readable_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    sidecar_src = tmp_path / "assets" / "sidecar" / "src"
    package = sidecar_src / "clawtune_sidecar"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        _install_sandbox_runtime(workspace, tmp_path / "assets")
    finally:
        os.umask(previous_umask)

    runtime = workspace / ".clawtune"
    launcher = runtime / "bin" / "clawtune-launch"
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert (runtime / "bin" / "pip").stat().st_mode & 0o777 == 0o755
    assert (runtime / "bin" / "pip3").stat().st_mode & 0o777 == 0o755
    assert runtime.stat().st_mode & 0o055 == 0o055
    assert (runtime / "sidecar").stat().st_mode & 0o055 == 0o055
    assert (runtime / "sidecar" / "src").stat().st_mode & 0o055 == 0o055
    assert (runtime / "sidecar" / "src" / "clawtune_sidecar" / "__init__.py").stat().st_mode & 0o044 == 0o044


def test_host_openclaw_installs_testbed_pip_wrappers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    package = tmp_path / "assets" / "sidecar" / "src" / "clawtune_sidecar"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    _install_sandbox_runtime(workspace, tmp_path / "assets")

    for name in ("pip", "pip3"):
        wrapper = workspace / ".clawtune" / "bin" / name
        assert wrapper.read_text(encoding="utf-8") == (
            '#!/bin/sh\nexec python3 -m pip "$@"\n'
        )


def test_host_openclaw_launcher_exports_testbed_path_but_uses_system_python(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    package = tmp_path / "assets" / "sidecar" / "src" / "clawtune_sidecar"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    _install_sandbox_runtime(workspace, tmp_path / "assets")

    launcher = (workspace / ".clawtune" / "bin" / "clawtune-launch").read_text(
        encoding="utf-8"
    )
    assert f'export PATH="{_SANDBOX_TASK_PATH}"\n' in launcher
    assert "/usr/bin/python3 /usr/local/bin/python3" in launcher
    assert "command -v python3" in launcher
    assert "sys.version_info >= (3, 10)" in launcher


def _write_test_kb_pair(directory: Path, marker: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        "runtime-tool-resource-kb.json": {
            "schema": "runtime_tool_resource_kb_v1",
            "quantile": 0.9,
            "max_prefix_depth": 4,
            "public": {
                "latency_ms": [],
                "peak_cpu_cores": [],
                "peak_memory_mb": [],
            },
            "repo": {},
            "pending": [],
            "last_query_ts": None,
            "marker": marker,
        },
        "clause-resource-kb.json": {
            "schema": "runtime_clause_resource_kb_v4",
            "max_prefix_depth": 4,
            "public": {
                "latency_ms": [],
                "peak_cpu_cores": [],
                "sampled_peak_rss_mb": [],
            },
            "repo": {},
            "pending": [],
            "last_query_ts": None,
            "marker": marker,
        },
        "clause-lattice-time-kb.json": {
            "schema": "clause_lattice_time_kb_v1",
            "node_generation": {
                "mode": "bounded",
                "max_optional_features": 6,
                "min_partial_support": 1,
                "max_nodes_per_signature": 4_096,
                "node_occurrence_budget": 20_000,
                "max_shrinkage_candidates": 512,
            },
            "observations": [],
            "pending": [],
            "last_query_ts": None,
            "marker": marker,
        },
    }
    for filename, payload in payloads.items():
        (directory / filename).write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _kb_pair_markers(directory: Path) -> dict[str, str]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))["marker"]
        for path in (
            directory / "runtime-tool-resource-kb.json",
            directory / "clause-resource-kb.json",
            directory / "clause-lattice-time-kb.json",
        )
    }


def test_host_openclaw_seeds_runtime_and_clause_predictor_kbs(tmp_path: Path) -> None:
    source_dir = tmp_path / "traces" / "tool-resource"
    _write_test_kb_pair(source_dir, "tracked-seed")
    runtime_payload = (source_dir / "runtime-tool-resource-kb.json").read_text(
        encoding="utf-8"
    )
    clause_payload = (source_dir / "clause-resource-kb.json").read_text(
        encoding="utf-8"
    )
    lattice_payload = (source_dir / "clause-lattice-time-kb.json").read_text(
        encoding="utf-8"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    assert config.config_path == config_path.resolve()
    trace_dir = tmp_path / "task-trace"

    _seed_runtime_tool_resource_kb(trace_dir, config)

    seeded_dir = trace_dir / "tool-resource"
    assert (seeded_dir / "runtime-tool-resource-kb.json").read_text(
        encoding="utf-8"
    ) == runtime_payload
    assert (seeded_dir / "clause-resource-kb.json").read_text(
        encoding="utf-8"
    ) == clause_payload
    assert (seeded_dir / "clause-lattice-time-kb.json").read_text(
        encoding="utf-8"
    ) == lattice_payload


def test_batch_shared_kb_prepare_copy_in_and_publish_reaches_next_task(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    tracked_seed = tmp_path / "traces" / "tool-resource"
    shared_kb_dir = tmp_path / "kb-batches" / "batch-1"
    _write_test_kb_pair(tracked_seed, "tracked-seed")

    _prepare_batch_tool_resource_kb(shared_kb_dir, config)

    expected_seed = {
        "runtime-tool-resource-kb.json": "tracked-seed",
        "clause-resource-kb.json": "tracked-seed",
        "clause-lattice-time-kb.json": "tracked-seed",
    }
    assert _kb_pair_markers(shared_kb_dir) == expected_seed

    task_a_trace = tmp_path / "task-a"
    _seed_runtime_tool_resource_kb(
        task_a_trace,
        config,
        source_dir=shared_kb_dir,
    )
    assert _kb_pair_markers(task_a_trace / "tool-resource") == expected_seed

    _write_test_kb_pair(task_a_trace / "tool-resource", "task-a-update")
    _publish_tool_resource_kb(task_a_trace, shared_kb_dir)

    task_b_trace = tmp_path / "task-b"
    _seed_runtime_tool_resource_kb(
        task_b_trace,
        config,
        source_dir=shared_kb_dir,
    )

    expected_update = {
        "runtime-tool-resource-kb.json": "task-a-update",
        "clause-resource-kb.json": "task-a-update",
        "clause-lattice-time-kb.json": "task-a-update",
    }
    assert _kb_pair_markers(shared_kb_dir) == expected_update
    assert _kb_pair_markers(task_b_trace / "tool-resource") == expected_update
    assert _kb_pair_markers(tracked_seed) == expected_seed


def test_batch_shared_kb_invalid_pair_does_not_overwrite_last_good_generation(
    tmp_path: Path,
) -> None:
    shared_kb_dir = tmp_path / "shared-kb"
    task_trace = tmp_path / "task"
    task_kb_dir = task_trace / "tool-resource"
    _write_test_kb_pair(shared_kb_dir, "last-good")
    _write_test_kb_pair(task_kb_dir, "new-generation")
    (task_kb_dir / "clause-resource-kb.json").write_text(
        "{not-json\n",
        encoding="utf-8",
    )
    before = {
        path.name: path.read_bytes()
        for path in (
            shared_kb_dir / "runtime-tool-resource-kb.json",
            shared_kb_dir / "clause-resource-kb.json",
            shared_kb_dir / "clause-lattice-time-kb.json",
        )
    }

    with pytest.raises(RuntimeError, match="invalid KB snapshot"):
        _publish_tool_resource_kb(task_trace, shared_kb_dir)

    after = {
        path.name: path.read_bytes()
        for path in (
            shared_kb_dir / "runtime-tool-resource-kb.json",
            shared_kb_dir / "clause-resource-kb.json",
            shared_kb_dir / "clause-lattice-time-kb.json",
        )
    }
    assert after == before


def test_batch_shared_kb_rejects_schema_only_snapshot_as_unloadable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    tracked_seed = tmp_path / "traces" / "tool-resource"
    _write_test_kb_pair(tracked_seed, "tracked-seed")
    (tracked_seed / "runtime-tool-resource-kb.json").write_text(
        json.dumps(
            {
                "schema": "runtime_tool_resource_kb_v1",
                "max_prefix_depth": 4,
                "public": {},
                "repo": {},
                "pending": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeBaseSyncError, match="scheduler rejected"):
        _prepare_batch_tool_resource_kb(tmp_path / "shared-kb", config)


def test_batch_shared_kb_rejects_invalid_lattice_observation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    tracked_seed = tmp_path / "traces" / "tool-resource"
    _write_test_kb_pair(tracked_seed, "tracked-seed")
    lattice_path = tracked_seed / "clause-lattice-time-kb.json"
    payload = json.loads(lattice_path.read_text(encoding="utf-8"))
    payload["observations"] = [
        {
            "repo": "org/repo",
            "bin": "python",
            "argv": ["python", "task.py"],
            "ts_start": 1.0,
            "ts_end": 2.0,
            "latency_ms": 1000.0,
            "unexpected": True,
        }
    ]
    lattice_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(KnowledgeBaseSyncError, match="unknown fields"):
        _prepare_batch_tool_resource_kb(tmp_path / "shared-kb", config)


def test_batch_shared_kb_second_replace_failure_rolls_back_whole_pair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shared_kb_dir = tmp_path / "shared-kb"
    task_trace = tmp_path / "task"
    _write_test_kb_pair(shared_kb_dir, "last-good")
    _write_test_kb_pair(task_trace / "tool-resource", "new-generation")
    real_replace = os.replace
    failed_once = False

    def fail_second_commit(source, destination):
        nonlocal failed_once
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed_once
            and source_path.parent.name == "staged"
            and destination_path
            == shared_kb_dir / "clause-resource-kb.json"
        ):
            failed_once = True
            raise OSError("simulated second snapshot replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr("swe_rebench.host_openclaw.os.replace", fail_second_commit)

    with pytest.raises(RuntimeError, match="was rolled back"):
        _publish_tool_resource_kb(task_trace, shared_kb_dir)

    assert failed_once is True
    assert _kb_pair_markers(shared_kb_dir) == {
        "runtime-tool-resource-kb.json": "last-good",
        "clause-resource-kb.json": "last-good",
        "clause-lattice-time-kb.json": "last-good",
    }


def test_run_batch_aborts_before_next_task_on_kb_sync_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: container-openclaw\n"
        "output:\n  trace_root: traces\n  report_path: report.json\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    tasks = [
        TaskDef(instance_id="owner__repo-1", image="image:1", repo="owner/repo"),
        TaskDef(instance_id="owner__repo-2", image="image:2", repo="owner/repo"),
    ]
    attempted: list[str] = []

    import swe_rebench.runner as runner

    monkeypatch.setattr(runner, "get_docker_client", lambda _config: None)
    monkeypatch.setattr(
        runner,
        "_pre_pull_images",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runner,
        "_prepare_batch_tool_resource_kb",
        lambda *_args, **_kwargs: None,
    )

    def fail_first_task(_client, task, *_args, **_kwargs):
        attempted.append(task.instance_id)
        raise KnowledgeBaseSyncError("simulated publish failure")

    monkeypatch.setattr(runner, "_run_one", fail_first_task)

    report = run_batch(config, tasks, tmp_path / "assets")

    assert attempted == ["owner__repo-1"]
    assert report.aborted is True
    assert report.abort_reason == "simulated publish failure"
    assert report.completed == 0
    assert report.failed == 1
    assert len(report.results) == 1


def test_host_openclaw_verifies_mounted_launcher_before_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "mode": "fork-exec",
                    "fork_supported": True,
                    "ready": True,
                    "payload_python3": "/opt/miniconda3/envs/testbed/bin/python3",
                    "payload_pip": "/workspace/.clawtune/bin/pip",
                    "payload_pip3": "/workspace/.clawtune/bin/pip3",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda name: "/usr/bin/docker",
    )
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "setup.py").write_text("", encoding="utf-8")

    _verify_sandbox_launcher(tmp_path, workspace, "linux/amd64")

    command = calls[0][0]
    assert command[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert command[3] == "--name"
    assert command[4].startswith(_sandbox_container_prefix(workspace))
    assert command[5:7] == ["--platform", "linux/amd64"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--env") + 1] == "CLAWTUNE_LAUNCH_MODE=fork-exec"
    assert not any(part.startswith("PATH=") for part in command)
    assert command[command.index("--user") + 1] == "65534:65534"
    assert command[command.index("--entrypoint") + 1] == "/bin/sh"
    assert command[-2] == "/workspace/.clawtune/bin/clawtune-launch"
    assert command[-1] == "diagnose"


def test_host_openclaw_launcher_preflight_rejects_system_python(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "setup.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda name: "/usr/bin/docker",
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "mode": "fork-exec",
                    "fork_supported": True,
                    "ready": True,
                    "payload_python3": "/usr/bin/python3",
                    "payload_pip": None,
                    "payload_pip3": None,
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="payload_environment_failed"):
        _verify_sandbox_launcher(tmp_path, workspace)


def test_host_openclaw_verifies_python_task_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "setup.py").write_text("", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda name: "/usr/bin/docker",
    )
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _verify_sandbox_task_environment(tmp_path, workspace, "linux/amd64")

    command = calls[0][0]
    assert command[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert command[3] == "--name"
    assert command[4].startswith(_sandbox_container_prefix(workspace))
    assert command[5:7] == ["--platform", "linux/amd64"]
    assert any(
        part.startswith(
            "PATH=/workspace/.clawtune/bin:/opt/miniconda3/envs/testbed/bin:"
        )
        for part in command
    )
    assert "python3 -m pip --version" in command[-1]
    assert "pip --version" in command[-1]
    assert "pip3 --version" in command[-1]
    assert (tmp_path / "sandbox-runtime-preflight.log").exists()


def test_host_openclaw_exports_testbed_with_docker_platform(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return Result(1)
        if cmd[:2] == ["/usr/bin/docker", "pull"]:
            return Result(0)
        if cmd[:2] == ["/usr/bin/docker", "create"]:
            return Result(0, stdout="container-id\n")
        if cmd[:2] == ["/usr/bin/docker", "rm"]:
            return Result(0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda name: "/usr/bin/docker",
    )
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)
    monkeypatch.setattr("swe_rebench.host_openclaw._run_checked", lambda cmd, phase: calls.append(list(cmd)))

    _export_testbed_from_image("image:latest", tmp_path / "workspace", "always", "linux/amd64")

    assert ["/usr/bin/docker", "pull", "--platform", "linux/amd64", "image:latest"] in calls
    assert ["/usr/bin/docker", "create", "--platform", "linux/amd64", "image:latest"] in calls


def test_host_openclaw_missing_pull_policy_pulls_when_platform_is_set(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["/usr/bin/docker", "create"]:
            return Result(0, stdout="container-id\n")
        if cmd[:2] == ["/usr/bin/docker", "rm"]:
            return Result(0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda name: "/usr/bin/docker",
    )
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)
    monkeypatch.setattr("swe_rebench.host_openclaw._run_checked", lambda cmd, phase: calls.append(list(cmd)))

    _export_testbed_from_image("image:latest", tmp_path / "workspace", "missing", "linux/amd64")

    assert ["/usr/bin/docker", "pull", "--platform", "linux/amd64", "image:latest"] in calls
    assert not any(call[:3] == ["/usr/bin/docker", "image", "inspect"] for call in calls)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not represented on Windows")
def test_host_openclaw_makes_sudo_export_writable_without_following_symlinks(
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


def test_host_openclaw_prompt_uses_relative_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    task = TaskDef(
        instance_id="12rambau__sepal_ui-411",
        image="image:latest",
        problem_statement="fix",
    )
    trace_dir = tmp_path / "trace"
    workspace = tmp_path / "workspace"
    runtime_assets_dir = tmp_path / "assets"
    shared_kb_dir = tmp_path / "kb-batches" / "batch-1"
    runtime_assets_dir.mkdir()
    fingerprint = {
        "schema": "swe_rebench_runtime_assets_source_fingerprint_v1",
        "digest": "sha256:" + "a" * 64,
        "file_count": 1,
        "files": ["swe_rebench/runner.py"],
    }
    (runtime_assets_dir / "runtime-assets-source-fingerprint.json").write_text(
        json.dumps(fingerprint),
        encoding="utf-8",
    )

    _write_task_inputs(
        trace_dir,
        task,
        config,
        workspace,
        runtime_assets_dir,
        shared_kb_dir=shared_kb_dir,
    )
    prompt = (trace_dir / "agent_prompt.txt").read_text(encoding="utf-8")
    manifest = json.loads(
        (trace_dir / "task_manifest.json").read_text(encoding="utf-8")
    )

    assert "Use relative paths" in prompt
    assert "repository mounted at /workspace" not in prompt
    assert manifest["repo"] == "12rambau/sepal_ui"
    assert manifest["shared_kb_dir"] == str(shared_kb_dir)
    assert manifest["runner_config"] == str(config_path.resolve())
    assert manifest["runtime_assets_source_fingerprint"] == fingerprint


def test_host_openclaw_agent_forces_sandbox_exec_workdir(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("swe_rebench.host_openclaw._require_executable", lambda name: "/usr/bin/openclaw")
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.Popen", fake_popen)

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
    assert env["CLAWTUNE_EXEC_WORKDIR"] == "/workspace"
    assert env["CLAWTUNE_ENDPOINT"] == "http://127.0.0.1:8765"
    assert env["CLAWTUNE_LAUNCHER_ENDPOINT"] == "http://host.docker.internal:8765"
    assert env["CLAWTUNE_SANDBOX_HOST_WORKSPACE"] == str(workspace)
    assert env["CLAWTUNE_SANDBOX_CONTAINER_WORKSPACE"] == "/workspace"


def test_openclaw_uses_agent_flag_detects_flag_syntax(monkeypatch) -> None:
    class FakeResult:
        # Flag-style build: `agent main --help` answers with the parent
        # `agent` usage, which does not mention `agent main`.
        stdout = "Usage: openclaw agent [options]\n  --agent <id>  Agent id\n"
        stderr = ""
        returncode = 0

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return FakeResult()

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    assert _openclaw_uses_agent_flag("/usr/bin/openclaw") is True
    assert calls == [["/usr/bin/openclaw", "agent", "main", "--help"]]


def test_openclaw_uses_agent_flag_ignores_error_hint_on_stderr(monkeypatch) -> None:
    class FakeResult:
        # Regression: a flag-style build answers `agent main --help` with the
        # parent `agent` usage on stdout AND the "Too many arguments ... Try:
        # openclaw agent main --help" hint on stderr.  The stderr hint contains
        # the literal text "agent main", which the old substring check misread
        # as a positional build, then invoked `openclaw agent main ...` and
        # reproduced the very error it was trying to avoid.
        stdout = "Usage: openclaw agent [options]\n  --agent <id>  Agent id\n"
        stderr = (
            "Too many arguments for this command.\n"
            "Try: openclaw agent main --help\n"
        )
        returncode = 1

    def fake_run(cmd, **kwargs):
        return FakeResult()

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    assert _openclaw_uses_agent_flag("/usr/bin/openclaw") is True


def test_openclaw_uses_agent_flag_requires_usage_line_on_stdout(monkeypatch) -> None:
    class FakeResult:
        # Even a flag build that (confusingly) exits 0 must not be treated as
        # positional unless the real subcommand usage line appears on stdout.
        stdout = "Usage: openclaw agent [options]\n  --agent <id>  Agent id\n"
        stderr = "Try: openclaw agent main --help\n"
        returncode = 0

    def fake_run(cmd, **kwargs):
        return FakeResult()

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    assert _openclaw_uses_agent_flag("/usr/bin/openclaw") is True


def test_openclaw_uses_agent_flag_detects_positional_syntax(monkeypatch) -> None:
    class FakeResult:
        # Positional build: `agent main --help` answers with the subcommand's
        # own usage line, which contains `agent main`.
        stdout = (
            "Usage: openclaw agent main [options]\n"
            "  --local       Run the embedded agent locally\n"
            "  --model <id>  Model override\n"
        )
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        return FakeResult()

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    assert _openclaw_uses_agent_flag("/usr/bin/openclaw") is False


def test_openclaw_uses_agent_flag_falls_back_when_probe_fails(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    assert _openclaw_uses_agent_flag("/usr/bin/openclaw") is True


def test_openclaw_agent_argv_flag_syntax(monkeypatch) -> None:
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._openclaw_uses_agent_flag",
        lambda _openclaw: True,
    )
    argv = _openclaw_agent_argv(
        "/usr/bin/openclaw",
        model_ref="vllm/deepseek-v4-flash",
        prompt_path=Path("/tmp/agent_prompt.txt"),
        extra_args=[],
    )
    assert argv == [
        "/usr/bin/openclaw",
        "agent",
        "--local",
        "--agent",
        "main",
        "--model",
        "vllm/deepseek-v4-flash",
        "--message-file",
        str(Path("/tmp/agent_prompt.txt")),
    ]


def test_openclaw_agent_argv_positional_syntax(monkeypatch) -> None:
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._openclaw_uses_agent_flag",
        lambda _openclaw: False,
    )
    argv = _openclaw_agent_argv(
        "/usr/bin/openclaw",
        model_ref="vllm/deepseek-v4-flash",
        prompt_path=Path("/tmp/agent_prompt.txt"),
        extra_args=["--verbose", "on"],
    )
    assert argv == [
        "/usr/bin/openclaw",
        "agent",
        "main",
        "--local",
        "--model",
        "vllm/deepseek-v4-flash",
        "--message-file",
        str(Path("/tmp/agent_prompt.txt")),
        "--verbose",
        "on",
    ]


@pytest.mark.parametrize(
    ("task_budget_seconds", "agent_budget_seconds", "expected_scope"),
    [
        (None, 30, "agent"),
        (0.25, 30, "task"),
    ],
)
def test_host_openclaw_agent_uses_smallest_timeout_and_kills_process(
    monkeypatch,
    tmp_path: Path,
    task_budget_seconds: float | None,
    agent_budget_seconds: int,
    expected_scope: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    config.batch.agent_timeout_seconds = agent_budget_seconds
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = TaskDef(instance_id="task-1", image="image:latest", problem_statement="fix")
    waits: list[float | None] = []

    class FakeProcess:
        pid = 123
        stdout = io.StringIO("")
        stderr = io.StringIO("")
        killed = False

        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("openclaw", timeout)
            return 0

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda _name: "/usr/bin/openclaw",
    )
    monkeypatch.setattr(
        "swe_rebench.host_openclaw.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    task_deadline = (
        time.monotonic() + task_budget_seconds
        if task_budget_seconds is not None
        else None
    )

    exit_code = _run_openclaw_agent(
        trace_dir=trace_dir,
        openclaw_home=tmp_path / "home",
        workspace=workspace,
        sidecar_port=8765,
        task=task,
        config=config,
        task_deadline=task_deadline,
    )

    assert exit_code == 124
    assert process.killed is True
    assert waits[0] is not None
    if task_budget_seconds is not None:
        assert 0 < float(waits[0]) <= task_budget_seconds
    else:
        assert 0 < float(waits[0]) <= agent_budget_seconds
    timeout_record = json.loads(
        (trace_dir / "task-timeout.json").read_text(encoding="utf-8")
    )
    assert timeout_record["scope"] == expected_scope


def test_host_openclaw_cleanup_timeout_is_bounded_and_strict(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    workspace = tmp_path / "workspace"

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(
        "swe_rebench.host_openclaw._require_executable",
        lambda _name: "/usr/bin/docker",
    )
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    with pytest.raises(ContainerCleanupError, match="timed out listing"):
        _cleanup_openclaw_sandbox_containers(
            trace_dir,
            workspace,
            timeout_seconds=0.01,
            strict=True,
        )

    assert "docker_ps_timed_out" in (
        trace_dir / "sandbox-container-cleanup.log"
    ).read_text(encoding="utf-8")


def test_host_openclaw_tags_sandbox_image_from_task_image(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("swe_rebench.host_openclaw.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    sandbox_image = _ensure_openclaw_sandbox_image(task_image, tmp_path)

    assert calls == [
        ["/usr/bin/docker", "tag", task_image, sandbox_image],
    ]
    assert sandbox_image.startswith("clawtune-sandbox:image-")
    assert (tmp_path / "sandbox-image-build.log").exists()


def test_host_openclaw_builds_plugin_before_install(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("swe_rebench.host_openclaw._require_executable", fake_require)
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _ensure_plugin_built(tmp_path / "trace", plugin_dir)

    assert calls == [["/usr/bin/npm", "run", "build"]]
    assert (tmp_path / "trace" / "plugin-build.log").exists()


def test_host_openclaw_stages_user_owned_plugin_when_running_as_root(monkeypatch, tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "package.json").write_text("{}\n", encoding="utf-8")
    (plugin_dir / "dist.js").write_text("module.exports = {}\n", encoding="utf-8")
    (plugin_dir / "node_modules").mkdir()
    (plugin_dir / "node_modules" / "ignored.txt").write_text("skip\n", encoding="utf-8")
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    monkeypatch.setattr("swe_rebench.host_openclaw.os.geteuid", lambda: 0, raising=False)
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

    assert staged == trace_dir / "clawtune-plugin-root-owned"
    assert (staged / "package.json").exists()
    assert (staged / "dist.js").exists()
    assert not (staged / "node_modules").exists()


def test_host_openclaw_sidecar_enables_docker_exec_observer(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.setenv("CLAWTUNE_TOOL_RESOURCE_REPO", "stale/openclaw")
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

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.Popen", fake_popen)
    monkeypatch.setattr("swe_rebench.host_openclaw._wait_ready", lambda port, **kw: None)

    process = _start_sidecar(
        trace_dir=trace_dir,
        port=8765,
        config=config,
        workspace=workspace,
        repo="12rambau/sepal_ui",
    )

    assert isinstance(process, FakeProcess)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CLAWTUNE_DOCKER_EXEC_OBSERVER"] == "true"
    assert env["CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED"] == "true"
    assert env["CLAWTUNE_TOOL_RESOURCE_REPO"] == "12rambau/sepal_ui"
    assert env["CLAWTUNE_DOCKER_EXEC_CONTAINER_PREFIX"] == _sandbox_container_prefix(workspace)
    assert str(tmp_path / "services" / "sidecar" / "src") in env["PYTHONPATH"]
    assert "LD_PRELOAD" not in env


def test_host_openclaw_sidecar_readiness_failure_stops_unreturned_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    captured: dict[str, object] = {}

    class FakeProcess:
        terminated = False
        waited = False

        def poll(self):
            return 0 if self.waited else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

    process = FakeProcess()

    def fake_popen(_cmd, **kwargs):
        captured["stdout"] = kwargs["stdout"]
        captured["stderr"] = kwargs["stderr"]
        return process

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "swe_rebench.host_openclaw._wait_ready",
        lambda _port, **kw: (_ for _ in ()).throw(RuntimeError("not ready")),
    )

    with pytest.raises(RuntimeError, match="not ready"):
        _start_sidecar(
            trace_dir=trace_dir,
            port=8765,
            config=config,
            workspace=tmp_path / "workspace",
            repo="12rambau/sepal_ui",
        )

    assert process.terminated is True
    assert process.waited is True
    assert getattr(captured["stdout"], "closed") is True
    assert getattr(captured["stderr"], "closed") is True


def test_host_openclaw_writes_tool_resource_preflight(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    captured: dict[str, object] = {}

    class FakeRunResult:
        stdout = '{"mode": "host-openclaw", "ebpf_ready": true}\n'
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs["env"]
        return FakeRunResult()

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _write_host_tool_resource_preflight(trace_dir, config)

    preflight = trace_dir / "tool_resource_preflight_host.json"
    assert json.loads(preflight.read_text(encoding="utf-8")) == {
        "mode": "host-openclaw",
        "ebpf_ready": True,
    }
    env = captured["env"]
    assert isinstance(env, dict)
    assert str(tmp_path / "services" / "sidecar" / "src") in env["PYTHONPATH"]
    command = captured["cmd"]
    assert isinstance(command, list)
    assert "ensure_compatible_adapter" in command[-1]


def test_host_openclaw_preserves_bpf_compiler_stderr(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    config.runtime.ebpf_required = False
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    class FakeRunResult:
        stdout = '{"mode": "host-openclaw", "ebpf_ready": false}\n'
        stderr = "/virtual/main.c:137: error: incompatible rss_stat layout\n"
        returncode = 0

    monkeypatch.setattr(
        "swe_rebench.host_openclaw.subprocess.run",
        lambda *args, **kwargs: FakeRunResult(),
    )

    _write_host_tool_resource_preflight(trace_dir, config)

    recorded = json.loads(
        (trace_dir / "tool_resource_preflight_host.json").read_text(encoding="utf-8")
    )
    assert recorded["preflight_stderr"] == (
        "/virtual/main.c:137: error: incompatible rss_stat layout"
    )


def test_host_openclaw_required_ebpf_fails_fast_on_preflight(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw\n  ebpf_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    class FakeRunResult:
        stdout = json.dumps(
            {
                "mode": "host-openclaw",
                "ebpf_ready": False,
                "ebpf_disabled_reason": "ValueError: clause telemetry requires root",
            }
        )
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        "swe_rebench.host_openclaw.subprocess.run",
        lambda *args, **kwargs: FakeRunResult(),
    )

    with pytest.raises(RuntimeError, match=r"requires root.*sudo -E"):
        _write_host_tool_resource_preflight(trace_dir, config)

    recorded = json.loads(
        (trace_dir / "tool_resource_preflight_host.json").read_text(encoding="utf-8")
    )
    assert recorded["ebpf_ready"] is False


def test_host_openclaw_cleans_only_untracked_runtime_artifacts(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".clawtune").mkdir()
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

    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _cleanup_runtime_artifacts(workspace)

    assert (workspace / "AGENTS.md").exists()
    assert not (workspace / ".clawtune").exists()
    assert not (workspace / ".local").exists()
    assert not (workspace / "HEARTBEAT.md").exists()
    assert not (workspace / "openclaw-workspace-state.json").exists()
    assert "AGENTS.md" in seen


def test_host_openclaw_workspace_reset_falls_back_to_docker_on_permission_error(
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

    monkeypatch.setattr("swe_rebench.host_openclaw.shutil.rmtree", fake_rmtree)
    monkeypatch.setattr("swe_rebench.host_openclaw.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _reset_directory(
        workspace,
        docker_cleanup_image="task-image:latest",
        docker_platform="linux/amd64",
    )

    assert calls
    cleanup_cmd = calls[0]
    assert cleanup_cmd[:5] == ["/usr/bin/docker", "run", "--rm", "--platform", "linux/amd64"]
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
    monkeypatch.setattr("swe_rebench.host_openclaw.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

    _reset_task_trace_dir(
        trace_root,
        trace_dir,
        docker_cleanup_image="task-image:latest",
        docker_platform="linux/amd64",
    )

    assert calls
    cleanup_cmd = calls[0]
    assert cleanup_cmd[:5] == ["/usr/bin/docker", "run", "--rm", "--platform", "linux/amd64"]
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
    monkeypatch.setattr("swe_rebench.host_openclaw.shutil.which", fake_which)
    monkeypatch.setattr("swe_rebench.host_openclaw.subprocess.run", fake_run)

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


def test_minimal_yaml_fallback_keeps_section_across_blank_lines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
docker:
  host: "unix:///var/run/docker.sock"

  # Comments and blank lines should not end the docker section.
  privileged: true
  cgroupns_mode: "host"
  cgroup_mount_rw: true
""",
        encoding="utf-8",
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    raw = _load_yaml_safe(config_path)

    assert raw["docker"]["privileged"] == "true"
    assert raw["docker"]["cgroupns_mode"] == "host"
    assert raw["docker"]["cgroup_mount_rw"] == "true"


def test_minimal_yaml_fallback_keeps_extra_args_as_empty_list(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression: with PyYAML unavailable the fallback parser must keep
    # `extra_args: []` an empty list.  Previously it stored the literal
    # string "[]" and `list("[]")` unpacked it to ['[', ']'], which were
    # appended to the `openclaw agent` argv as stray positionals and caused
    # "Too many arguments for this command."
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agent:
  extra_args: []
""",
        encoding="utf-8",
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.agent.extra_args == []


def test_minimal_yaml_fallback_parses_nonempty_extra_args(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agent:
  extra_args: ["--verbose", "on"]
""",
        encoding="utf-8",
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.agent.extra_args == ["--verbose", "on"]


def test_agent_extra_args_coerces_string_bracket_form() -> None:
    # Defensive layer: even if a loader hands us the literal string "[]",
    # AgentConfig must not unpack it into bracket characters.
    assert AgentConfig.from_dict({"extra_args": "[]"}).extra_args == []
    assert AgentConfig.from_dict({"extra_args": ["--verbose", "on"]}).extra_args == [
        "--verbose",
        "on",
    ]
    assert _as_str_list("[]") == []
    assert _as_str_list("['--verbose', 'on']") == ["--verbose", "on"]
    assert _as_str_list(None) == []
    assert _as_str_list("") == []


def test_runner_config_reads_docker_platform_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SWE_REBENCH_DOCKER_PLATFORM", "linux/amd64")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("docker:\n  platform: linux/arm64\n", encoding="utf-8")

    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert config.docker.platform == "linux/amd64"


def test_task_artifacts_summarizes_patch_and_result_summary(tmp_path: Path) -> None:
    (tmp_path / "model.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
    (tmp_path / "agent-cwd.txt").write_text("/testbed\n", encoding="utf-8")
    (tmp_path / "agent-stdout.txt").write_text("done\n", encoding="utf-8")
    sidecar_log = "ready\n"
    (tmp_path / "sidecar.log").write_text(sidecar_log, encoding="utf-8")
    (tmp_path / "sidecar-stderr.txt").write_text("bcc diagnostics\n", encoding="utf-8")
    container_log = "container done\n"
    (tmp_path / "container.log").write_text(container_log, encoding="utf-8")
    (tmp_path / "tool_resource_preflight.json").write_text('{"ebpf_ready": true}\n', encoding="utf-8")
    (tmp_path / "tool_resource_preflight_host.json").write_text('{"bcc_import": {"ok": true}}\n', encoding="utf-8")
    (tmp_path / "result_summary.json").write_text(
        json.dumps({"has_patch": True, "patch_bytes": 19}),
        encoding="utf-8",
    )

    artifacts = _task_artifacts(tmp_path)

    assert artifacts["model.patch"]["has_diff"] is True
    assert artifacts["agent-cwd.txt"]["preview"] == "/testbed\n"
    assert artifacts["agent-stdout.txt"]["preview"] == "done\n"
    assert artifacts["sidecar.log"]["bytes"] == (tmp_path / "sidecar.log").stat().st_size
    assert artifacts["sidecar-stderr.txt"]["preview"] == "bcc diagnostics\n"
    assert artifacts["container.log"]["bytes"] == (tmp_path / "container.log").stat().st_size
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


def test_reset_task_trace_dir_is_safe_during_parallel_root_creation(tmp_path: Path) -> None:
    trace_root = tmp_path / "traces"
    barrier = threading.Barrier(16)
    failures: list[BaseException] = []

    def reset(index: int) -> None:
        try:
            barrier.wait()
            _reset_task_trace_dir(trace_root, trace_root / f"task-{index}")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    workers = [threading.Thread(target=reset, args=(index,)) for index in range(16)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert failures == []
    assert all((trace_root / f"task-{index}").is_dir() for index in range(16))


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
        runtime_assets_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
        env_extra={"CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED": "true"},
    )

    assert result.exit_code == 7
    assert not any(call[:2] == ["docker", "inspect"] for call in calls)
    docker_run = calls[0]
    expected_prefix = sandbox_container_prefix("docker:task-1")
    assert f"CLAWTUNE_DOCKER_EXEC_CONTAINER_PREFIX={expected_prefix}" in docker_run
    assert "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false" in docker_run
    assert "CLAWTUNE_CGROUP_REQUIRED=0" in docker_run
    assert (tmp_path / "trace" / "container.log").read_text(encoding="utf-8") == "container output\n"


def test_docker_cli_wait_failure_stops_container_before_return(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    wait_calls = 0

    class Result:
        def __init__(self, stdout: str = "", stderr: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        nonlocal wait_calls
        command = list(cmd)
        calls.append(command)
        if command[:3] == ["docker", "run", "--detach"]:
            return Result(stdout="abc123\n")
        if command == ["docker", "wait", "abc123"]:
            wait_calls += 1
            if kwargs.get("check") is True:
                raise subprocess.CalledProcessError(
                    1,
                    command,
                    stderr="wait failed",
                )
            return Result(stdout="137\n")
        if command == ["docker", "kill", "abc123"]:
            return Result(stdout="abc123\n")
        if command == ["docker", "logs", "abc123"]:
            return Result(stdout="final output\n")
        if command == ["docker", "rm", "-f", "abc123"]:
            return Result(stdout="abc123\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "swe_rebench.docker._stream_cli_container_logs",
        lambda *_args: (None, None),
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    result = run_container(
        client=None,
        image="image:latest",
        task_id="task-wait-failure",
        runtime_assets_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
    )

    assert result.exit_code == -1
    assert "wait failed" in (result.error or "")
    assert wait_calls == 2
    assert calls.index(["docker", "kill", "abc123"]) < calls.index(
        ["docker", "logs", "abc123"]
    )
    assert calls[-1] == ["docker", "rm", "-f", "abc123"]


def test_docker_sdk_remove_failure_is_not_reported_as_quiesced(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeContainer:
        id = "abc123"

        def wait(self, timeout=None):
            return {"StatusCode": 0}

        def remove(self, force=False):
            raise RuntimeError("remove failed")

    class FakeContainers:
        def run(self, **_kwargs):
            return FakeContainer()

    client = type("FakeClient", (), {"containers": FakeContainers()})()
    monkeypatch.setattr(
        "swe_rebench.docker._stream_sdk_container_logs",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "swe_rebench.docker._write_sdk_container_log",
        lambda *_args: None,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    with pytest.raises(ContainerCleanupError, match="remove failed"):
        run_container(
            client=client,
            image="image:latest",
            task_id="task-sdk-cleanup",
            runtime_assets_dir=tmp_path,
            trace_dir=tmp_path / "trace",
            problem_statement="fix",
            config=config.docker,
            llm_api_key="sk-test",
            llm_upstream_url="https://example.invalid",
            timeout_seconds=10,
        )


def test_container_kernel_headers_mount_exact_host_paths_read_only(
    monkeypatch, tmp_path: Path
) -> None:
    modules_root = tmp_path / "lib" / "modules"
    module_dir = modules_root / "5.15.0-test"
    module_dir.mkdir(parents=True)
    header_root = tmp_path / "usr" / "src"
    header_dir = header_root / "linux-headers-5.15.0-test"
    header_dir.mkdir(parents=True)

    monkeypatch.setattr("swe_rebench.docker.sys.platform", "linux")
    monkeypatch.setattr(
        "swe_rebench.docker._resolve_kernel_build", lambda _build: header_dir
    )

    volumes = _container_kernel_header_volumes(
        "unix:///var/run/docker.sock",
        kernel_release="5.15.0-test",
        modules_root=modules_root,
        header_root=header_root,
    )

    assert volumes == {
        str(module_dir): {"bind": str(module_dir), "mode": "ro"},
        str(header_dir): {"bind": str(header_dir), "mode": "ro"},
    }


def test_container_kernel_headers_reject_unsafe_or_remote_mounts(
    monkeypatch, tmp_path: Path
) -> None:
    modules_root = tmp_path / "lib" / "modules"
    module_dir = modules_root / "5.15.0-test"
    module_dir.mkdir(parents=True)
    header_root = tmp_path / "usr" / "src"
    header_root.mkdir(parents=True)
    unsafe_target = tmp_path / "etc"
    unsafe_target.mkdir()

    monkeypatch.setattr("swe_rebench.docker.sys.platform", "linux")
    monkeypatch.setattr(
        "swe_rebench.docker._resolve_kernel_build", lambda _build: unsafe_target
    )

    kwargs = {
        "kernel_release": "5.15.0-test",
        "modules_root": modules_root,
        "header_root": header_root,
    }
    assert _container_kernel_header_volumes(
        "unix:///var/run/docker.sock", **kwargs
    ) == {}
    assert _container_kernel_header_volumes(
        "tcp://docker.example:2376", **kwargs
    ) == {}


def test_container_tracefs_mounts_scheduler_tracepoint_root_read_write(
    monkeypatch, tmp_path: Path
) -> None:
    missing_root = tmp_path / "missing-tracefs"
    tracefs_root = tmp_path / "sys" / "kernel" / "tracing"
    tracepoint_id = tracefs_root / "events" / "sched" / "sched_process_exit" / "id"
    tracepoint_id.parent.mkdir(parents=True)
    tracepoint_id.write_text("314\n", encoding="utf-8")
    (tracefs_root / "kprobe_events").write_text("", encoding="utf-8")

    monkeypatch.setattr("swe_rebench.docker.sys.platform", "linux")

    volumes = _container_tracefs_volumes(
        "unix:///var/run/docker.sock",
        tracefs_roots=(missing_root, tracefs_root),
    )

    assert volumes == {
        str(tracefs_root): {"bind": str(tracefs_root), "mode": "rw"},
    }


def test_container_tracefs_skips_missing_tracepoint_remote_and_non_linux(
    monkeypatch, tmp_path: Path
) -> None:
    empty_root = tmp_path / "sys" / "kernel" / "tracing"
    empty_root.mkdir(parents=True)
    monkeypatch.setattr("swe_rebench.docker.sys.platform", "linux")

    assert _container_tracefs_volumes(
        "unix:///var/run/docker.sock", tracefs_roots=(empty_root,)
    ) == {}
    assert _container_tracefs_volumes(
        "tcp://docker.example:2376", tracefs_roots=(empty_root,)
    ) == {}

    monkeypatch.setattr("swe_rebench.docker.sys.platform", "win32")
    assert _container_tracefs_volumes(
        "unix:///var/run/docker.sock", tracefs_roots=(empty_root,)
    ) == {}


def test_docker_cli_adds_discovered_kernel_header_mounts(
    monkeypatch, tmp_path: Path
) -> None:
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

    header_volumes = {
        "/lib/modules/5.15.0-test": {
            "bind": "/lib/modules/5.15.0-test",
            "mode": "ro",
        },
        "/usr/src/linux-headers-5.15.0-test": {
            "bind": "/usr/src/linux-headers-5.15.0-test",
            "mode": "ro",
        },
    }
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "swe_rebench.docker._container_kernel_header_volumes",
        lambda _host: header_volumes,
    )
    monkeypatch.setattr(
        "swe_rebench.docker._container_tracefs_volumes",
        lambda _host: {
            "/sys/kernel/tracing": {
                "bind": "/sys/kernel/tracing",
                "mode": "rw",
            }
        },
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    run_container(
        client=None,
        image="image:latest",
        task_id="task-headers",
        runtime_assets_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
    )

    docker_run = calls[0]
    assert "/lib/modules/5.15.0-test:/lib/modules/5.15.0-test:ro" in docker_run
    assert (
        "/usr/src/linux-headers-5.15.0-test:"
        "/usr/src/linux-headers-5.15.0-test:ro"
    ) in docker_run
    assert "/sys/kernel/tracing:/sys/kernel/tracing:rw" in docker_run


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
        runtime_assets_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
        ebpf_required=True,
    )

    assert "CLAWTUNE_CGROUP_REQUIRED=1" in calls[0]
    assert "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=true" in calls[0]


def test_docker_cli_passes_configured_platform(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, stdout: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:5] == ["docker", "run", "--detach", "--platform", "linux/amd64"]:
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
  platform: linux/amd64
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    run_container(
        client=None,
        image="image:latest",
        task_id="task-1",
        runtime_assets_dir=tmp_path,
        trace_dir=tmp_path / "trace",
        problem_statement="fix",
        config=config.docker,
        llm_api_key="sk-test",
        llm_upstream_url="https://example.invalid",
        timeout_seconds=10,
    )

    assert calls[0][:5] == ["docker", "run", "--detach", "--platform", "linux/amd64"]


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
runtime_assets:
  output_dir: "assets"
""",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    runtime_assets_dir = tmp_path / "assets"
    runtime_assets_dir.mkdir()

    _write_entrypoint(runtime_assets_dir, config)

    assert "sk-secret" not in (runtime_assets_dir / "entrypoint.sh").read_text(encoding="utf-8")


def test_prepare_rebuilds_plugin_dist_after_removing_stale_files(monkeypatch, tmp_path: Path) -> None:
    plugin_dir = tmp_path / "packages" / "clawtune-plugin"
    dist_dir = plugin_dir / "dist"
    dist_dir.mkdir(parents=True)
    (plugin_dir / "package.json").write_text('{"scripts":{"build":"tsc"}}\n', encoding="utf-8")
    (dist_dir / "stale.js").write_text("old runtime\n", encoding="utf-8")
    runtime_assets_dir = tmp_path / "assets"
    runtime_assets_dir.mkdir()
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

    _build_plugin_dist(tmp_path, runtime_assets_dir, config)

    assert calls == [["/usr/bin/npm", "run", "build"]]
    assert not (dist_dir / "stale.js").exists()
    assert (dist_dir / "index.js").read_text(encoding="utf-8") == "fresh runtime\n"
    assert (runtime_assets_dir / "plugin-build.log").exists()


def test_prepare_restores_root_built_dist_to_sudo_caller(monkeypatch, tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    nested = dist_dir / "trace"
    nested.mkdir(parents=True)
    generated = nested / "index.js"
    generated.write_text("generated\n", encoding="utf-8")
    calls: list[tuple[Path, int, int, bool]] = []

    monkeypatch.setenv("SUDO_UID", "1001")
    monkeypatch.setenv("SUDO_GID", "1002")
    monkeypatch.setattr("swe_rebench.prepare.os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "swe_rebench.prepare.os.chown",
        lambda path, uid, gid, *, follow_symlinks: calls.append(
            (Path(path), uid, gid, follow_symlinks)
        ),
        raising=False,
    )

    _restore_sudo_user_ownership(dist_dir)

    assert {item[0] for item in calls} == {dist_dir, nested, generated}
    assert all(item[1:] == (1001, 1002, False) for item in calls)


def test_runtime_assets_stale_check_ignores_dist_but_tracks_source(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "packages" / "clawtune-plugin"
    sidecar_dir = tmp_path / "services" / "sidecar"
    runtime_assets_dir = tmp_path / "assets"
    (plugin_dir / "src").mkdir(parents=True)
    (plugin_dir / "dist").mkdir()
    sidecar_dir.mkdir(parents=True)
    runtime_assets_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    marker = runtime_assets_dir / "entrypoint.sh"
    marker.write_text("built\n", encoding="utf-8")
    future = marker.stat().st_mtime + 1000
    os.utime(marker, (future, future))
    now = marker.stat().st_mtime
    old = now - 100
    for path in (
        plugin_dir / "src" / "index.ts",
        plugin_dir / "dist" / "index.js",
        sidecar_dir / "pyproject.toml",
    ):
        path.write_text("old\n", encoding="utf-8")
        os.utime(path, (old, old))
    _write_runtime_assets_fingerprint(config, runtime_assets_dir)

    assert runtime_assets_need_rebuild(config, runtime_assets_dir) is False

    source = plugin_dir / "src" / "index.ts"
    new = now + 100
    os.utime(source, (new, new))

    assert runtime_assets_need_rebuild(config, runtime_assets_dir) is True


def test_runtime_assets_stale_check_rebuilds_when_fingerprint_is_missing(tmp_path: Path) -> None:
    runtime_assets_dir = tmp_path / "assets"
    runtime_assets_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    (runtime_assets_dir / "entrypoint.sh").write_text("built\n", encoding="utf-8")

    assert runtime_assets_need_rebuild(config, runtime_assets_dir) is True


def test_runtime_assets_stale_check_tracks_content_even_when_mtime_is_old(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "packages" / "clawtune-plugin"
    sidecar_dir = tmp_path / "services" / "sidecar"
    runtime_assets_dir = tmp_path / "assets"
    (plugin_dir / "src").mkdir(parents=True)
    sidecar_dir.mkdir(parents=True)
    runtime_assets_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    source = plugin_dir / "src" / "index.ts"
    source.write_text("old\n", encoding="utf-8")
    marker = runtime_assets_dir / "entrypoint.sh"
    marker.write_text("built\n", encoding="utf-8")
    _write_runtime_assets_fingerprint(config, runtime_assets_dir)
    now = marker.stat().st_mtime
    source.write_text("new\n", encoding="utf-8")
    old = now - 100
    os.utime(source, (old, old))

    assert runtime_assets_need_rebuild(config, runtime_assets_dir) is True
