from __future__ import annotations

from types import SimpleNamespace

from tools.check_stage2 import run_preflight


def _fake_telemetry(*, smoke_error: Exception | None = None) -> SimpleNamespace:
    def smoke() -> dict[str, object]:
        if smoke_error is not None:
            raise smoke_error
        return {
            "ok": True,
            "exec_boundary_count": 1,
            "loss_counts": {},
        }

    return SimpleNamespace(
        _bpf_runtime_diagnostics=lambda: {
            "kernel_headers": ["/fake/kernel/build"],
            "cgroup_v2": True,
        },
        _ensure_bcc_importable=lambda: SimpleNamespace(
            __name__="bpfcc",
            __file__="/usr/lib/python3/site-packages/bpfcc/__init__.py",
        ),
        validate_clause_telemetry_runtime=lambda **_kwargs: None,
        validate_clause_telemetry_smoke=smoke,
    )


def test_preflight_reports_semantic_success(monkeypatch) -> None:
    monkeypatch.setattr("tools.check_stage2.shutil.which", lambda name: f"/usr/bin/{name}")

    report = run_preflight(_fake_telemetry())

    assert report["stage2_ready"] is True
    assert report["bcc_import"]["module"] == "bpfcc"
    assert report["semantic_smoke"]["exec_boundary_count"] == 1
    assert report["error"] is None


def test_preflight_fails_closed_on_semantic_error(monkeypatch) -> None:
    monkeypatch.setattr("tools.check_stage2.shutil.which", lambda name: f"/usr/bin/{name}")

    report = run_preflight(
        _fake_telemetry(smoke_error=RuntimeError("no successful exec boundary")),
    )

    assert report["stage2_ready"] is False
    assert "no successful exec boundary" in report["error"]


def test_preflight_requires_docker(monkeypatch) -> None:
    monkeypatch.setattr("tools.check_stage2.shutil.which", lambda _name: None)

    report = run_preflight(_fake_telemetry())

    assert report["stage2_ready"] is False
    assert report["error"] == "RuntimeError: docker is not available on PATH"
