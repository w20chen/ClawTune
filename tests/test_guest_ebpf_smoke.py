from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "check_guest_ebpf.py"
    spec = importlib.util.spec_from_file_location("check_guest_ebpf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(*, host_pid: int = 101) -> dict:
    return {
        "cleanup": "ok",
        "telemetry_loss_total": {"total": 0},
        "collector": {"health": "healthy"},
        "calls": [
            {
                "eligible_for_kb": True,
                "invalid_reasons": [],
                "clauses": [
                    {
                        "host_pid": host_pid,
                        "peak_cpu_cores": 0.5,
                        "sampled_peak_rss_mb": 2.0,
                    }
                ],
            }
        ],
    }


def test_validate_artifact_accepts_scoped_zero_loss_result() -> None:
    assert _module()._validate_artifact(_artifact(), unrelated_pid=202) == []


def test_validate_artifact_rejects_unrelated_pid_and_loss() -> None:
    artifact = _artifact(host_pid=202)
    artifact["telemetry_loss_total"]["total"] = 1
    errors = _module()._validate_artifact(artifact, unrelated_pid=202)
    assert "collector loss=1" in errors
    assert "unrelated pid 202 leaked into artifact" in errors


def test_validate_artifact_rejects_unrelated_command_across_pid_namespaces() -> None:
    module = _module()
    artifact = _artifact()
    artifact["calls"][0]["clauses"].append(
        {
            "host_pid": 600_202,
            "argv": module.UNRELATED_ARGV,
            "peak_cpu_cores": 0.1,
            "sampled_peak_rss_mb": 1.0,
        }
    )

    errors = module._validate_artifact(artifact, unrelated_pid=202)

    assert f"unrelated command {module.UNRELATED_ARGV!r} leaked into artifact" in errors
