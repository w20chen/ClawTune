from __future__ import annotations

import errno
import copy
import ctypes
import json
from types import SimpleNamespace
import pytest

import clawtune_sidecar.monitoring.pmu as pmu_module
from clawtune_sidecar.monitoring.pmu import EVENT_SPECS, PmuCollector
from clawtune_sidecar.predictors.tool_resource import _quality_gated_pmu_metrics
from tool_resource.runtime_kb import CompletedCall, RuntimeToolResourceKB, ToolCallQuery


class FakePerfBackend:
    def __init__(self, *, ratio: float = 1.0, unsupported: set[str] | None = None,
                 deny_kernel: bool = False) -> None:
        self.ratio = ratio
        self.unsupported = unsupported or set()
        self.deny_kernel = deny_kernel
        self.next_fd = 10
        self.names: dict[int, str] = {}
        self.opens: list[tuple[str, int, int, bool, bool]] = []
        self.closed: list[int] = []
        self.disabled: list[int] = []
        self.values = {
            "cycles": 2_000_000,
            "instructions": 1_000_000,
            "llc_read_misses": 1_000,
            "llc_read_accesses": 10_000,
        }

    def open_event(self, spec, pid, group_fd, *, leader, exclude_kernel):
        self.opens.append((spec.name, pid, group_fd, leader, exclude_kernel))
        if self.deny_kernel and not exclude_kernel:
            raise OSError(errno.EACCES, "denied")
        if spec.name in self.unsupported:
            raise OSError(errno.EOPNOTSUPP, "unsupported")
        fd = self.next_fd
        self.next_fd += 1
        self.names[fd] = spec.name
        return fd

    def disable_group(self, leader_fd):
        self.disabled.append(leader_fd)

    def read_event(self, fd):
        enabled = 1_000_000_000
        running = round(enabled * self.ratio)
        return self.values[self.names[fd]], enabled, running

    def close(self, fd):
        self.closed.append(fd)


@pytest.mark.parametrize("signal,exit_code", [(None, 124), (15, None), (9, None)])
def test_authenticated_timeout_exit_keeps_valid_pmu(signal, exit_code, tmp_path):
    from fastapi.testclient import TestClient
    from clawtune_sidecar.api.app import create_app
    from clawtune_sidecar.api.dependencies import build_state
    from clawtune_sidecar.config import SidecarConfig
    state = build_state(SidecarConfig(trace_dir=tmp_path / "trace"))
    state.pmu_collector = PmuCollector(backend=FakePerfBackend())
    with TestClient(create_app(state)) as client:
        registered = client.post("/v2/executions", json={
            "execution_id": "timeout", "tool_call_id": "call-timeout", "run_id": "run",
            "session_key_hash": None, "runtime_id": "runtime", "gateway_id": "test",
            "command_digest": "sha256:" + "a" * 64, "command": "sleep 30", "backend": "managed-wrapper",
        })
        assert registered.status_code == 200, registered.text
        claimed = client.post("/v2/executions/claim", json={"execution_id": "timeout",
            "token": registered.json()["one_time_token"], "launcher_pid": 1234})
        assert claimed.status_code == 200, claimed.text
        state.pmu_collector.begin("timeout", 4242)
        response = client.post("/v2/executions/timeout/exited", json={
            "update_token": claimed.json()["update_token"], "exit_code": exit_code, "signal": signal})
        assert response.status_code == 200, response.text
        profile = state.pmu_collector.take("timeout")
        assert profile.coverage.status == "reliable"
        assert _quality_gated_pmu_metrics(profile.to_dict())["eligible"]
        assert state.executions.get("timeout").signal == signal
        assert state.executions.get("timeout").exit_code == exit_code


@pytest.mark.parametrize("failure_kind", ["overall-timeout", "no-output-timeout"])
def test_openclaw_timeout_closes_normal_counting_window(failure_kind, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from clawtune_sidecar.api.app import create_app, _completion_reports_timeout
    from clawtune_sidecar.api.dependencies import build_state
    from clawtune_sidecar.config import SidecarConfig
    from clawtune_kb.contracts import validate
    state = build_state(SidecarConfig(trace_dir=tmp_path / "trace"))
    backend = FakePerfBackend()
    state.pmu_collector = PmuCollector(backend=backend)
    monkeypatch.setattr(state.tool_monitor, "complete", lambda *a, **kw: None)
    with TestClient(create_app(state)) as client:
        registered = client.post("/v2/executions", json={
            "execution_id": "timeout", "tool_call_id": "call-timeout", "run_id": "run",
            "session_key_hash": None, "runtime_id": "runtime", "gateway_id": "test",
            "command_digest": "sha256:" + "a" * 64, "command": "sleep 30", "backend": "managed-wrapper",
        })
        assert registered.status_code == 200
        state.pmu_collector.begin("timeout", 4242)
        body = {"schema_version": "clawtune.v1", "event_id": "timeout-event",
            "occurred_at": "2026-10-14T12:00:00Z", "plugin_version": "0.1.0",
            "run_id": "run", "session_id": None, "session_key": None, "agent_id": None,
            "gateway_id": "test", "runtime_id": "runtime", "tool_call_id": "call-timeout",
            "decision_id": None, "lease_id": None, "execution_id": "timeout", "tool_name": "exec",
            "duration_ms": 8000, "succeeded": False, "error_type": "timeout", "error_digest": None,
            "raw_result": {"details": {"status": "error", "failureKind": failure_kind}}}
        response = client.post("/v1/events/tool-completed", json=body)
        assert response.status_code == 200, response.text
        result = state.pmu_collector.take("timeout")
        assert result.coverage.status == "reliable"
        assert result.coverage.reason == "tool_timeout"
        assert _quality_gated_pmu_metrics(result.to_dict())["eligible"]
        validate(json.loads(json.dumps(result.to_dict())), "pmu-profile.schema.json")
        assert len(backend.closed) == 4
        assert client.post("/v1/events/tool-completed", json=body).json() == {"stored": False}
        assert len(backend.closed) == 4
    assert not _completion_reports_timeout({"content": "overall-timeout"})
    assert not _completion_reports_timeout({"details": {"status": "running", "failureKind": failure_kind}})


def test_timeout_does_not_override_counter_quality():
    collector = PmuCollector(backend=FakePerfBackend(ratio=.5))
    collector.begin("timeout", 4242)
    result = collector.finish("timeout", reason="tool_timeout")
    assert result.coverage.status == "multiplexed"
    assert not _quality_gated_pmu_metrics(result.to_dict())["eligible"]


def test_native_perf_attributes_preserve_inheritance_compatible_read_format():
    backend = pmu_module.LinuxPerfBackend.__new__(pmu_module.LinuxPerfBackend)
    backend._syscall_number = 298
    captured = []
    def syscall(number, pointer, pid, cpu, group, flags):
        attr = ctypes.cast(pointer, ctypes.POINTER(pmu_module._PerfEventAttr)).contents
        captured.append((attr.size, attr.read_format, attr.flags, pid.value, cpu.value, group.value, flags.value))
        return 10
    backend._libc = SimpleNamespace(syscall=syscall)
    backend.open_event(EVENT_SPECS[0], 123, -1, leader=True, exclude_kernel=False)
    backend.open_event(EVENT_SPECS[1], 123, 10, leader=False, exclude_kernel=False)
    assert captured[0] == (112, 3, (1 << 0) | (1 << 1) | (1 << 6) | (1 << 12), 123, -1, -1, 8)
    assert captured[1] == (112, 3, (1 << 1) | (1 << 6), 123, -1, 10, 8)


def test_counting_group_attributes_four_events_and_derives_metrics() -> None:
    backend = FakePerfBackend()
    collector = PmuCollector(max_active=8, backend=backend)

    assert collector.begin("exec-1", 4242) is None
    profile = collector.finish("exec-1")

    assert [item[0] for item in backend.opens] == [spec.name for spec in EVENT_SPECS]
    assert all(item[1] == 4242 for item in backend.opens)
    assert backend.opens[0][2:] == (-1, True, False)
    assert all(item[2] == 10 for item in backend.opens[1:])
    assert len(backend.closed) == 4
    assert profile.coverage.status == "reliable"
    assert profile.coverage.eligible_for_kb is True
    assert profile.events["cycles"].raw_count == 2_000_000
    assert profile.derived == {"ipc": 0.5, "llc_mpki": 1.0, "llc_miss_rate": 0.1,
                               "llc_read_accesses_per_cpu_second": 10000.,
                               "llc_read_misses_per_cpu_second": 1000.}
    assert profile.llc_semantics_confirmed is True
    assert "CACHE_LL:READ" in profile.events["llc_read_misses"].semantics


def test_multiplexing_is_scaled_but_never_marked_reliable() -> None:
    collector = PmuCollector(max_active=1, backend=FakePerfBackend(ratio=0.5))
    collector.begin("exec", 12)
    profile = collector.finish("exec")

    assert profile.events["cycles"].raw_count == 2_000_000
    assert profile.events["cycles"].scaled_count == 4_000_000
    assert profile.coverage.status == "multiplexed"
    assert profile.coverage.running_ratio == 0.5
    assert profile.coverage.eligible_for_kb is False
    assert _quality_gated_pmu_metrics(profile.to_dict())["eligible"] is False

    lightly_multiplexed = PmuCollector(
        max_active=1, backend=FakePerfBackend(ratio=0.99)
    )
    lightly_multiplexed.begin("light", 13)
    assert lightly_multiplexed.finish("light").coverage.status == "multiplexed"


def test_unsupported_llc_never_falls_back_to_generic_cache_misses() -> None:
    backend = FakePerfBackend(unsupported={"llc_read_misses", "llc_read_accesses"})
    collector = PmuCollector(max_active=1, backend=backend)
    collector.begin("exec", 12)
    profile = collector.finish("exec")

    assert profile.coverage.status == "partial"
    assert profile.llc_semantics_confirmed is False
    assert profile.events["llc_read_misses"].raw_count is None
    assert profile.derived["llc_mpki"] is None
    assert not any(spec.event_type == 0 and spec.config == 3 for spec in EVENT_SPECS)


def test_permission_fallback_is_user_only_with_partial_quality() -> None:
    backend = FakePerfBackend(deny_kernel=True)
    collector = PmuCollector(max_active=1, backend=backend)
    collector.begin("exec", 12)
    profile = collector.finish("exec")

    assert any(not item[-1] for item in backend.opens)
    assert all(item[-1] for item in backend.opens[-4:])
    assert profile.coverage.status == "partial"
    assert profile.coverage.reason == "kernel_excluded"
    assert profile.coverage.eligible_for_kb is False


def test_global_concurrency_budget_bounds_fd_growth_without_raising() -> None:
    backend = FakePerfBackend()
    collector = PmuCollector(max_active=2, max_fds=8, backend=backend)

    assert collector.begin("exec-1", 101) is None
    assert collector.begin("exec-2", 102) is None
    rejected = [collector.begin(f"exec-{index}", 100 + index) for index in range(3, 100)]

    assert len(backend.opens) == 8
    assert all(profile is not None for profile in rejected)
    assert all(profile.coverage.reason == "resource_budget" for profile in rejected if profile)
    assert collector.diagnostics()["active"] == 2


def test_next_begin_reaps_a_dead_root_after_a_lost_exit_callback(monkeypatch) -> None:
    identities = {101: "start-a", 102: "start-b"}
    monkeypatch.setattr(pmu_module, "_process_identity", identities.get)
    backend = FakePerfBackend()
    collector = PmuCollector(max_active=1, backend=backend)
    assert collector.begin("exec-1", 101) is None
    identities.pop(101)

    assert collector.begin("exec-2", 102) is None
    recovered = collector.take("exec-1")

    assert recovered is not None
    assert recovered.coverage.reason == "root_exited_before_callback"
    assert collector.diagnostics()["active"] == 1


def test_parallel_begin_preserves_a_queued_exit_report(monkeypatch) -> None:
    identities = {101: "start-a", 102: "start-b"}
    monkeypatch.setattr(pmu_module, "_process_identity", identities.get)
    collector = PmuCollector(max_active=2, backend=FakePerfBackend())
    collector.begin("exec-1", 101)
    identities.pop(101)
    collector.begin("exec-2", 102)
    # There is no capacity pressure: /exited is allowed to finish its profile
    # even if a concurrent /started was processed first on the HTTP server.
    assert collector.take("exec-1") is None
    profile = collector.finish("exec-1", reason="execution_exited")
    assert profile.coverage.status == "reliable"
    assert profile.coverage.eligible_for_kb
    collector.finish("exec-2")


def test_consumed_profile_remains_idempotent_for_a_delayed_exit() -> None:
    collector = PmuCollector(max_active=1, backend=FakePerfBackend())
    collector.begin("exec", 12)
    original = collector.finish("exec")

    assert collector.take("exec") is original
    assert collector.finish("exec") is original


def test_reliable_pmu_metrics_enter_online_kb_and_roundtrip() -> None:
    profile = PmuCollector(max_active=1, backend=FakePerfBackend())
    profile.begin("exec", 12)
    metrics = _quality_gated_pmu_metrics(profile.finish("exec").to_dict())
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall(
        "repo", "exec", "python job.py", 0, 1,
        pmu_ipc=metrics["ipc"],
        pmu_llc_mpki=metrics["llc_mpki"],
        pmu_llc_miss_rate=metrics["llc_miss_rate"],
        pmu_eligible=metrics["eligible"],
    ))

    evidence = kb.predict_pmu_samples(
        ToolCallQuery("repo", "exec", "python job.py", 2)
    )
    assert evidence["ipc"]["values"] == (0.5,)
    assert evidence["llc_mpki"]["values"] == (1.0,)
    assert evidence["llc_miss_rate"]["values"] == (0.1,)

    restored = RuntimeToolResourceKB.from_json_obj(kb.to_json_obj())
    restored_evidence = restored.predict_pmu_samples(
        ToolCallQuery("repo", "exec", "python job.py", 2)
    )
    assert restored_evidence == evidence


def test_quality_gated_pmu_evidence_has_complete_prediction_output() -> None:
    from clawtune_sidecar.contracts.load_prediction import summarize_pmu_evidence

    prediction = summarize_pmu_evidence({
        "ipc": {"values": (0.5, 1.5), "context": ("repo", "exact")},
    })

    assert set(prediction.targets) == {spec.name for spec in pmu_module.EVENT_SPECS} | {"ipc", "llc_mpki", "llc_miss_rate", "llc_read_accesses_per_cpu_second", "llc_read_misses_per_cpu_second"}
    assert prediction.targets["ipc"].p50 == 1.0
    assert prediction.targets["ipc"].p90 == 1.5
    assert prediction.targets["ipc"].evidence_count == 2
    assert prediction.targets["llc_mpki"].status == "unavailable"
    assert prediction.targets["llc_mpki"].unavailable_reason == "no_compatible_quality_gated_pmu_evidence"


def test_reliable_metrics_are_gated_independently_when_a_ratio_is_undefined() -> None:
    backend = FakePerfBackend()
    backend.values["llc_read_accesses"] = 0
    backend.values["llc_read_misses"] = 0
    collector = PmuCollector(max_active=1, backend=backend)
    collector.begin("exec", 12)

    metrics = _quality_gated_pmu_metrics(collector.finish("exec").to_dict())

    assert metrics == {
        "cycles": 2000000, "instructions": 1000000, "llc_read_accesses": 0, "llc_read_misses": 0,
        "ipc": 0.5,
        "llc_mpki": 0.0,
        "llc_miss_rate": None,
        "eligible": True,
        "llc_read_accesses_per_cpu_second": 0.,
        "llc_read_misses_per_cpu_second": 0.,
    }


def test_online_kb_rejects_generic_cache_miss_relabeling() -> None:
    collector = PmuCollector(max_active=1, backend=FakePerfBackend())
    collector.begin("exec", 12)
    profile = collector.finish("exec").to_dict()
    profile["events"]["llc_read_misses"]["semantics"] = "PERF_COUNT_HW_CACHE_MISSES"

    assert _quality_gated_pmu_metrics(profile)["eligible"] is False


@pytest.mark.parametrize("reason", ["aborted", "signal_terminated", "completion_fallback", "root_exited_before_callback"])
def test_incomplete_lifecycle_keeps_partial_quality_diagnostic(reason):
    collector = PmuCollector(backend=FakePerfBackend())
    collector.begin("exec", 12)
    profile = collector.finish("exec", reason=reason)
    assert profile.coverage.status == "partial"
    assert not _quality_gated_pmu_metrics(profile.to_dict())["eligible"]


def test_failed_group_disable_is_not_reliable():
    backend = FakePerfBackend()
    def fail(fd):
        raise OSError(errno.EIO, "failure")
    backend.disable_group = fail
    collector = PmuCollector(backend=backend)
    collector.begin("exec", 12)
    profile = collector.finish("exec")
    assert profile.coverage.status == "partial"
    assert profile.collector_errors and len(backend.closed) == 4


def test_running_time_cannot_exceed_enabled_time():
    collector = PmuCollector(backend=FakePerfBackend(ratio=1.1))
    collector.begin("exec", 12)
    profile = collector.finish("exec")
    assert not profile.coverage.eligible_for_kb
    assert profile.derived["ipc"] is None


def test_kb_rechecks_raw_evidence_and_recomputes_ratios():
    collector = PmuCollector(backend=FakePerfBackend())
    collector.begin("exec", 12)
    profile = collector.finish("exec").to_dict()
    profile["derived"] = {"ipc": 999, "llc_mpki": 999, "llc_miss_rate": 999}
    assert not _quality_gated_pmu_metrics(profile, execution_id="another-execution")["eligible"]
    assert _quality_gated_pmu_metrics(profile) == {
        "cycles": 2000000, "instructions": 1000000, "llc_read_accesses": 10000, "llc_read_misses": 1000,
        "ipc": .5, "llc_mpki": 1., "llc_miss_rate": .1, "eligible": True,
        "llc_read_accesses_per_cpu_second": 10000.,
        "llc_read_misses_per_cpu_second": 1000.}
    for field, value in (("raw_count", True), ("time_running_ns", 1), ("error", "EIO")):
        corrupted = copy.deepcopy(profile)
        corrupted["events"]["cycles"][field] = value
        assert not _quality_gated_pmu_metrics(corrupted)["eligible"]
    corrupted = copy.deepcopy(profile)
    corrupted["coverage"]["multiplexed"] = True
    assert not _quality_gated_pmu_metrics(corrupted)["eligible"]


def test_impossible_llc_fraction_is_unavailable_not_clamped():
    backend = FakePerfBackend()
    backend.values["llc_read_misses"] = 20_000
    collector = PmuCollector(backend=backend)
    collector.begin("exec", 12)
    profile = collector.finish("exec")
    assert profile.derived["llc_miss_rate"] is None
    assert profile.coverage.status == "partial"
    assert not _quality_gated_pmu_metrics(profile.to_dict())["eligible"]


def test_llc_intensity_uses_inherited_perf_time_not_reporting_wall_time():
    class TwoCpuSeconds(FakePerfBackend):
        def read_event(self, fd):
            # Two descendants may accumulate two monitored CPU seconds in
            # one wall second. Dividing by wall time would double intensity.
            return self.values[self.names[fd]], 2_000_000_000, 2_000_000_000

    backend = TwoCpuSeconds()
    collector = PmuCollector(backend=backend)
    collector.begin("intensity", 12)
    profile = collector.finish("intensity").to_dict()
    assert len(backend.opens) == len(backend.closed) == 4
    assert profile["derived"]["llc_read_accesses_per_cpu_second"] == 5000.
    assert profile["derived"]["llc_read_misses_per_cpu_second"] == 500.
    profile.update(started_at=10., ended_at=3610.)
    profile["derived"]["llc_read_accesses_per_cpu_second"] = 999999.
    assert _quality_gated_pmu_metrics(profile)["llc_read_accesses_per_cpu_second"] == 5000.


@pytest.mark.parametrize("case,expected", [
    ("multiplex", (10_000 / .99, 1_000 / .99)),
    ("kernel", (10_000., 1_000.)),
    ("missing", (None, 1_000.)),
    ("aborted", (10_000., 1_000.)),
    ("zero_time", (None, None)),
    ("invalid_llc", (10_000., 20_000.)),
])
def test_unreliable_llc_quality_retained_while_available_intensity_trains(case, expected):
    from tool_resource.runtime_kb import _target_values, training_target_provenance
    backend = FakePerfBackend(ratio=.99 if case == "multiplex" else 1.,
                              deny_kernel=case == "kernel",
                              unsupported={"llc_read_accesses"} if case == "missing" else set())
    if case == "zero_time":
        backend.read_event = lambda fd: (100, 0, 0)
    if case == "invalid_llc":
        backend.values["llc_read_misses"] = 20000
    collector = PmuCollector(backend=backend)
    collector.begin("bad", 12)
    profile = collector.finish("bad", reason="aborted" if case == "aborted" else "execution_exited")
    metrics = _quality_gated_pmu_metrics(profile.to_dict())
    assert not profile.coverage.eligible_for_kb
    assert metrics["eligible"] is False
    names = ("llc_read_accesses_per_cpu_second", "llc_read_misses_per_cpu_second")
    for name, value in zip(names, expected):
        # The collector still labels these profiles unreliable. Training keeps
        # computable approximate rates; an absent counter/denominator stays
        # missing until the shared KB policy fills it with a marked zero.
        assert profile.derived[name] is None
        if value is None:
            assert metrics[name] is None
        else:
            assert metrics[name] == pytest.approx(value)
    call = CompletedCall(
        "repo", "exec", "python job.py", 0, 1,
        censored=case == "aborted", pmu_eligible=metrics["eligible"],
        **{"pmu_" + name: metrics[name] for name in names},
    )
    targets = _target_values(call)
    provenance = training_target_provenance(call)
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(call)
    evidence = kb.predict_pmu_samples(ToolCallQuery("repo", "exec", "python job.py", 2))
    for name, value in zip(names, expected):
        expected_value = 0. if value is None else value
        assert targets["pmu_" + name] == pytest.approx(expected_value)
        assert evidence[name]["values"] == pytest.approx((expected_value,))
        assert provenance["pmu_" + name] == (
            "imputed_zero" if value is None else
            "observed_partial" if case == "aborted" else "approximate_observation")


def test_llc_intensity_artifact_schema_and_old_profile_compatibility(tmp_path):
    import json
    from pathlib import Path
    import jsonschema

    collector = PmuCollector(backend=FakePerfBackend())
    collector.begin("artifact", 12)
    profile = collector.finish("artifact").to_dict()
    path = tmp_path / "profile.json"
    assert pmu_module.write_pmu_profile(path, profile)
    exported = json.loads(path.read_text())
    schema_path = Path(__file__).resolve().parents[3] / "contracts/pmu-profile.schema.json"
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(exported, schema)
    assert exported["derived"]["llc_read_accesses_per_cpu_second"] == 10000.
    for name in ("llc_read_accesses_per_cpu_second", "llc_read_misses_per_cpu_second"):
        del exported["derived"][name]
    jsonschema.validate(exported, schema)
    # Older reliable artifacts already contain the required raw evidence.
    assert _quality_gated_pmu_metrics(exported)["llc_read_accesses_per_cpu_second"] == 10000.
