from __future__ import annotations

import pytest

from clawtune_sidecar.config import SidecarConfig


def test_scheduler_config_loads_env_file_and_resolves_paths(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CLAWTUNE_TRACE_DIR=data/traces",
                "CLAWTUNE_LLM_UPSTREAM_BASE_URL=https://example.test/v1",
                "CLAWTUNE_LLM_PROXY_ENABLED=false",
                "CLAWTUNE_LLM_UPSTREAM_API_KEY=legacy-key",
                "CLAWTUNE_LLM_UPSTREAM_API_KEY_OVERRIDE=override-key",
                "CLAWTUNE_LLM_PROXY_DEBUG_DUMP=true",
                "CLAWTUNE_SANDBOX_CGROUP_PATH=/sys/fs/cgroup/openclaw/session-1",
                "CLAWTUNE_SANDBOX_CONTAINER_ID=container-1",
                "CLAWTUNE_SANDBOX_ROOT_PID=1234",
                "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWTUNE_ENV_FILE", str(env_file))
    monkeypatch.delenv("CLAWTUNE_TRACE_DIR", raising=False)
    monkeypatch.delenv("CLAWTUNE_LLM_UPSTREAM_BASE_URL", raising=False)
    monkeypatch.delenv("CLAWTUNE_LLM_PROXY_ENABLED", raising=False)
    monkeypatch.delenv("CLAWTUNE_LLM_UPSTREAM_API_KEY", raising=False)
    monkeypatch.delenv("CLAWTUNE_LLM_UPSTREAM_API_KEY_OVERRIDE", raising=False)
    monkeypatch.delenv("CLAWTUNE_LLM_PROXY_DEBUG_DUMP", raising=False)
    monkeypatch.delenv("CLAWTUNE_SANDBOX_CGROUP_PATH", raising=False)
    monkeypatch.delenv("CLAWTUNE_SANDBOX_CONTAINER_ID", raising=False)
    monkeypatch.delenv("CLAWTUNE_SANDBOX_ROOT_PID", raising=False)
    monkeypatch.delenv("CLAWTUNE_TOOL_RESOURCE_EBPF_TRACES", raising=False)
    monkeypatch.delenv("CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED", raising=False)
    monkeypatch.delenv("CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED", raising=False)

    config = SidecarConfig.from_env()

    assert config.trace_dir == tmp_path / "data" / "traces"
    assert config.llm_proxy_enabled is True
    assert config.llm_proxy_upstream_base_url == "https://example.test/v1"
    assert config.llm_proxy_upstream_api_key == "override-key"
    assert config.llm_proxy_debug_dump is True
    assert config.sandbox_cgroup_path == "/sys/fs/cgroup/openclaw/session-1"
    assert config.sandbox_container_id == "container-1"
    assert config.sandbox_root_pid == 1234
    assert config.tool_resource_ebpf_required is False


def test_scheduler_config_requires_ebpf_by_default() -> None:
    assert SidecarConfig().tool_resource_ebpf_required is True


def test_scheduler_config_loads_dynamic_cpu_capacity_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CLAWTUNE_CPU_RESERVE_RATIO", "0.125")
    monkeypatch.setenv("CLAWTUNE_CPU_RESERVE_CORES", "7")
    monkeypatch.setenv("CLAWTUNE_CPU_BUDGET_CORES", "42.5")

    config = SidecarConfig.from_env()

    assert config.cpu_reserve_ratio == 0.125
    assert config.cpu_reserve_cores == 7
    assert config.cpu_budget_cores == 42.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLAWTUNE_CPU_RESERVE_RATIO", "1.01"),
        ("CLAWTUNE_CPU_RESERVE_CORES", "-1"),
        ("CLAWTUNE_CPU_BUDGET_CORES", "nan"),
    ],
)
def test_scheduler_config_rejects_unsafe_cpu_capacity_values(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        SidecarConfig.from_env()


def test_sidecar_config_reads_ebpf_required(monkeypatch) -> None:
    monkeypatch.setenv("CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED", "false")

    config = SidecarConfig.from_env()

    assert config.tool_resource_ebpf_required is False


def test_sidecar_config_reads_ebpf_trace_paths(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "CLAWTUNE_TOOL_RESOURCE_EBPF_TRACES",
        "data/ebpf-a,data/ebpf-b",
    )
    config = SidecarConfig.from_env()

    assert config.tool_resource_ebpf_trace_paths == (
        tmp_path / "data" / "ebpf-a",
        tmp_path / "data" / "ebpf-b",
    )


def test_ttl_by_bucket_s_defaults_to_none() -> None:
    config = SidecarConfig()
    assert config.tool_resource_ttl_by_bucket_s is None
    assert config.tool_resource_miss_penalty_s is None


def test_ttl_by_bucket_s_env_parsing(monkeypatch) -> None:
    monkeypatch.setenv(
        "CLAWTUNE_TOOL_RESOURCE_TTL_BY_BUCKET_S",
        "0.5,2.0,5.0",
    )
    monkeypatch.setenv(
        "CLAWTUNE_TOOL_RESOURCE_MISS_PENALTY_S",
        "3.0",
    )

    config = SidecarConfig.from_env()

    assert config.tool_resource_ttl_by_bucket_s == (0.5, 2.0, 5.0)
    assert config.tool_resource_miss_penalty_s == 3.0


def test_ttl_by_bucket_s_env_empty_string(monkeypatch) -> None:
    monkeypatch.setenv(
        "CLAWTUNE_TOOL_RESOURCE_TTL_BY_BUCKET_S",
        "",
    )

    config = SidecarConfig.from_env()

    # Empty env var → None (not an empty tuple)
    assert config.tool_resource_ttl_by_bucket_s is None


def test_miss_penalty_s_rejects_negative(monkeypatch) -> None:
    monkeypatch.setenv(
        "CLAWTUNE_TOOL_RESOURCE_MISS_PENALTY_S",
        "-0.1",
    )

    with pytest.raises(ValueError, match="CLAWTUNE_TOOL_RESOURCE_MISS_PENALTY_S"):
        SidecarConfig.from_env()
