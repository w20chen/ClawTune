from __future__ import annotations

import pytest

from agent_scheduler.config import SchedulerConfig


def test_scheduler_config_loads_env_file_and_resolves_paths(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AGENT_SCHEDULER_TRACE_DIR=data/traces",
                "AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://example.test/v1",
                "AGENT_SCHEDULER_LLM_PROXY_ENABLED=false",
                "AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY=legacy-key",
                "AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE=override-key",
                "AGENT_SCHEDULER_LLM_PROXY_DEBUG_DUMP=true",
                "AGENT_SCHEDULER_SANDBOX_CGROUP_PATH=/sys/fs/cgroup/openclaw/session-1",
                "AGENT_SCHEDULER_SANDBOX_CONTAINER_ID=container-1",
                "AGENT_SCHEDULER_SANDBOX_ROOT_PID=1234",
                "AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=false",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_SCHEDULER_ENV_FILE", str(env_file))
    monkeypatch.delenv("AGENT_SCHEDULER_TRACE_DIR", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_LLM_PROXY_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_LLM_PROXY_DEBUG_DUMP", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_SANDBOX_CGROUP_PATH", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_SANDBOX_ROOT_PID", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_TRACES", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_REQUIRED", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED", raising=False)

    config = SchedulerConfig.from_env()

    assert config.trace_dir == tmp_path / "data" / "traces"
    assert config.llm_proxy_enabled is True
    assert config.llm_proxy_upstream_base_url == "https://example.test/v1"
    assert config.llm_proxy_upstream_api_key == "override-key"
    assert config.llm_proxy_debug_dump is True
    assert config.sandbox_cgroup_path == "/sys/fs/cgroup/openclaw/session-1"
    assert config.sandbox_container_id == "container-1"
    assert config.sandbox_root_pid == 1234
    assert config.tool_resource_stage2_required is False


def test_scheduler_config_requires_stage2_by_default() -> None:
    assert SchedulerConfig().tool_resource_stage2_required is True


def test_scheduler_config_loads_dynamic_cpu_capacity_overrides(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SCHEDULER_CPU_RESERVE_RATIO", "0.125")
    monkeypatch.setenv("AGENT_SCHEDULER_CPU_RESERVE_CORES", "7")
    monkeypatch.setenv("AGENT_SCHEDULER_CPU_BUDGET_CORES", "42.5")

    config = SchedulerConfig.from_env()

    assert config.cpu_reserve_ratio == 0.125
    assert config.cpu_reserve_cores == 7
    assert config.cpu_budget_cores == 42.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_SCHEDULER_CPU_RESERVE_RATIO", "1.01"),
        ("AGENT_SCHEDULER_CPU_RESERVE_CORES", "-1"),
        ("AGENT_SCHEDULER_CPU_BUDGET_CORES", "nan"),
    ],
)
def test_scheduler_config_rejects_unsafe_cpu_capacity_values(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        SchedulerConfig.from_env()


def test_scheduler_config_accepts_user_facing_ebpf_required(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_REQUIRED", "false")
    monkeypatch.setenv("AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED", "true")

    config = SchedulerConfig.from_env()

    assert config.tool_resource_stage2_required is False


def test_scheduler_config_accepts_user_facing_ebpf_trace_paths(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_TRACES",
        "data/ebpf-a,data/ebpf-b",
    )
    monkeypatch.setenv(
        "AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_TRACES",
        "data/legacy",
    )

    config = SchedulerConfig.from_env()

    assert config.tool_resource_stage2_trace_paths == (
        tmp_path / "data" / "ebpf-a",
        tmp_path / "data" / "ebpf-b",
    )
