from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clawtune_sidecar.contracts.models import ResourceScope, ToolCompletedEvent
from clawtune_sidecar.monitoring.ebpf_tool import (
    EbpfToolCallMonitor,
    _reduce_events,
    unavailable_observation,
    execution_observation,
    promote_execution_observation,
)
from clawtune_sidecar.monitoring.tool_runtime import RealtimeToolMonitor
from test_tool_runtime_monitor import _request


def _events(start: int, end: int, *, step: int = 50_000_000):
    rows = []
    for index, ts in enumerate(range(start, end + 1, step)):
        rows.append({
            "type": "perf",
            "ts_ns": ts,
            "host_pid": 123,
            "host_tid": 123,
            "cpu_ns": index * 25_000_000,
            "io_read_bytes": index * 100,
            "io_write_bytes": index * 20,
            "rss_pages": 100 + index,
            "mm_ptr": 456,
        })
    return rows


def _validator() -> Draft202012Validator:
    path = Path(__file__).resolve().parents[3] / "contracts" / "tool-resource-observation.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def test_reduced_ebpf_observation_is_schema_valid_and_metric_specific():
    start, end = 1_000_000_000, 2_000_000_000
    result = _reduce_events(
        _events(start, end),
        started_ns=start,
        ended_ns=end,
        shared=False,
        network_delta=(300, 400),
    )

    _validator().validate(result)
    assert result["backend"] == "ebpf"
    assert result["fallback_used"] is False
    assert result["metrics"]["cpu_time"]["eligible"] is True
    assert result["metrics"]["cpu_time"]["average_cores"] == pytest.approx(.5)
    assert result["metrics"]["cpu_peak"]["value_cores"] == pytest.approx(.5)
    assert result["metrics"]["memory_peak"]["eligible"] is True
    assert result["metrics"]["disk_io"]["eligible"] is True
    assert result["metrics"]["network_io"]["eligible"] is True


def test_metric_sampling_gap_and_shared_runtime_are_never_training_eligible():
    start, end = 1_000_000_000, 2_000_000_000
    sparse = _reduce_events(
        _events(start, end, step=1_000_000_000),
        started_ns=start,
        ended_ns=end,
        shared=False,
    )
    shared = _reduce_events(
        _events(start, end),
        started_ns=start,
        ended_ns=end,
        shared=True,
        network_delta=(1, 2),
    )

    _validator().validate(sparse)
    _validator().validate(shared)
    assert sparse["metrics"]["cpu_time"]["available"] is True
    assert sparse["metrics"]["cpu_time"]["eligible"] is True
    assert sparse["metrics"]["disk_io"]["eligible"] is True
    assert sparse["metrics"]["cpu_peak"]["eligible"] is False
    assert sparse["metrics"]["memory_peak"]["reason"] == "sampling_gap"
    assert all(metric["eligible"] is False for metric in shared["metrics"].values())


def test_late_scope_binding_keeps_values_but_disqualifies_all_metrics():
    start, end = 1_000_000_000, 2_000_000_000

    class Window:
        started_ns = start

        def finish(self, _ended_ns, **_kwargs):
            return _reduce_events(
                _events(start, end), started_ns=start, ended_ns=end, shared=False
            )

    monitor = EbpfToolCallMonitor(window_factory=lambda _scope: Window())
    monitor.begin("call", None)
    assert monitor.bind_scope("call", ResourceScope(kind="pid", pid=123, root_pid=123))
    result = monitor.complete("call")

    assert result["window"]["late_scope_binding"] is True
    assert result["metrics"]["memory_peak"]["available"] is True
    assert all(metric["eligible"] is False for metric in result["metrics"].values())
    assert {metric["reason"] for metric in result["metrics"].values() if metric["available"]} == {
        "scope_bound_after_action_start"
    }
    from clawtune_sidecar.monitoring.ebpf_tool import observation_sample_fields
    assert observation_sample_fields(result)["cpu_peak_cores"] == pytest.approx(.5)
    assert result["metrics"]["cpu_peak"]["eligible"] is False


def test_realtime_default_path_uses_ebpf_without_sampling_fallback(monkeypatch):
    class Sampler:
        def snapshot(self, *_args, **_kwargs):
            pytest.fail("default eBPF path called the legacy sampler")

    class Ebpf:
        def begin(self, key, scope):
            self.key = key

        def complete(self, key, **_kwargs):
            assert key == self.key
            return unavailable_observation("ebpf_scope_unavailable_at_start")

        def discard(self, _key):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(
        "clawtune_sidecar.monitoring.tool_runtime.ProcessResourceSampler", Sampler
    )
    monitor = RealtimeToolMonitor(ebpf_monitor=Ebpf())
    request = _request()
    monitor.begin(request, "unknown")
    data = {
        key: value for key, value in request.model_dump().items()
        if key in ToolCompletedEvent.model_fields
    }
    data.update(
        event_id="end",
        occurred_at="2026-07-16T03:23:01Z",
        duration_ms=1000,
        succeeded=True,
        decision_id=None,
        lease_id=None,
        error_type=None,
        error_digest=None,
    )
    try:
        sample = monitor.complete(ToolCompletedEvent.model_validate(data))
    finally:
        monitor.stop()

    assert sample.monitor_source == "ebpf-task-lineage"
    assert sample.resource_observation["fallback_used"] is False
    assert sample.cpu_time_delta_s is None


def test_missing_action_clock_never_creates_action_labels():
    class Window:
        started_ns = 0

        def finish(self, *_args, **_kwargs):
            return _reduce_events(_events(0, 1_000_000_000), started_ns=0,
                                  ended_ns=1_000_000_000, shared=False)

    monitor = EbpfToolCallMonitor(lambda _: Window())
    monitor.begin("call", ResourceScope(kind="pid", pid=123))
    result = monitor.complete("call")
    assert result["window"]["action_clock_unusable"]
    assert not any(m["eligible"] for m in result["metrics"].values())


def test_before_hook_prefix_can_be_excluded_without_rejecting_complete_counters():
    class Window:
        started_ns = 100

        def finish(self, ended_ns, *, started_ns=None):
            assert (started_ns, ended_ns) == (100, 1_000_000_100)
            return _reduce_events(
                _events(100, 1_000_000_100),
                started_ns=100,
                ended_ns=1_000_000_100,
                shared=False,
            )

    monitor = EbpfToolCallMonitor(lambda _: Window())
    monitor.begin("call", ResourceScope(kind="pid", pid=123))
    result = monitor.complete(
        "call", action_start_ns=0, action_end_ns=1_000_000_100
    )
    _validator().validate(result)
    assert result["window"]["kind"] == "collector"
    assert result["window"]["hook_prefix_excluded"] is True
    assert result["window"]["tool_action_start_ns"] == "0"
    assert result["metrics"]["cpu_time"]["eligible"] is True
    assert result["metrics"]["disk_io"]["eligible"] is True


def test_duplicate_begin_releases_every_lease():
    released = []
    issued = []

    def factory(_):
        issued.append(len(issued) + 1)
        return SimpleNamespace(started_ns=0, lease_id=issued[-1],
                               source=SimpleNamespace(release=released.append))

    monitor = EbpfToolCallMonitor(factory)
    scope = ResourceScope(kind="pid", pid=123)
    monitor.begin("same", scope)
    monitor.begin("same", scope)
    monitor.stop()
    assert issued == released == [1, 2]


def test_lifetime_counters_survive_sleep_but_peaks_do_not():
    rows = _events(0, 10_000_000_000, step=10_000_000_000)
    rows[0]["type"], rows[1]["type"] = "exec_boundary", "exit_boundary"
    result = _reduce_events(rows, started_ns=0, ended_ns=10_000_000_000, shared=False)
    assert result["metrics"]["cpu_time"]["eligible"]
    assert result["metrics"]["disk_io"]["eligible"]
    assert not result["metrics"]["cpu_peak"]["eligible"]
    assert not result["metrics"]["memory_peak"]["eligible"]


def test_busy_thread_cannot_hide_another_threads_missing_boundaries():
    rows = _events(0, 2_000_000_000)
    rows += [dict(row, host_tid=124) for row in _events(800_000_000, 1_000_000_000)]
    result = _reduce_events(rows, started_ns=0, ended_ns=2_000_000_000, shared=False)
    assert not result["metrics"]["cpu_time"]["eligible"]
    assert not result["metrics"]["cpu_peak"]["eligible"]


def test_counter_regression_is_not_silently_clamped():
    rows = _events(0, 1_000_000_000)
    rows[10]["cpu_ns"] = 0
    result = _reduce_events(rows, started_ns=0, ended_ns=1_000_000_000, shared=False)
    assert not result["metrics"]["cpu_time"]["available"]


def test_promotion_preserves_measured_execution_window_without_fabricating_coverage():
    call = {"telemetry_quality": "ok", "clauses": [{
        "availability": {"cpu_time": "ok", "cpu": "ok", "memory": "ok", "disk_io": "ok"},
        "cpu_time_seconds": .2, "peak_cpu_cores": .5, "peak_memory_mb": 10,
        "disk_read_bytes": 0, "disk_write_bytes": 0,
        "t_exec_ns": 100_000_000, "t_end_ns": 900_000_000,
    }]}
    synthetic = execution_observation(call, started_ns=0, ended_ns=1_000_000_000)
    real = execution_observation(call, started_ns=0, ended_ns=1_000_000_000, clock="linux_monotonic")
    for result in (synthetic, real):
        _validator().validate(result)
        assert not result["window"]["complete"]
        assert result["metrics"]["cpu_time"]["value_seconds"] == .2
    assert not any(m["eligible"] for m in synthetic["metrics"].values())
    assert synthetic["metrics"]["cpu_time"]["average_cores"] is None
    assert real["metrics"]["cpu_time"]["eligible"]
    assert real["metrics"]["cpu_time"]["average_cores"] == .2
    assert real["metrics"]["memory_peak"]["eligible"]
    assert synthetic["window"]["coverage_ratio"] is None
    assert real["window"]["coverage_ratio"] == .8
    outside = execution_observation(call, started_ns=200_000_000, ended_ns=1_000_000_000, clock="linux_monotonic")
    assert not any(m["eligible"] for m in outside["metrics"].values())


def _cgroup_files(path, cpu, read, peak):
    (path / "cpu.stat").write_text(f"usage_usec {cpu}\n", encoding="utf-8")
    (path / "io.stat").write_text(f"8:0 rbytes={read} wbytes=0\n", encoding="utf-8")
    (path / "memory.peak").write_text(str(peak), encoding="utf-8")


def test_promotion_preserves_eligible_metrics_and_rejects_cross_window_merge():
    from copy import deepcopy
    current = _reduce_events(_events(0, 1_000_000_000), started_ns=0,
                             ended_ns=1_000_000_000, shared=False, network_delta=(3, 4))
    execution = deepcopy(current)
    current["metrics"]["cpu_time"]["eligible"] = False
    execution["metrics"]["network_io"]["eligible"] = False
    merged = promote_execution_observation(current, execution)
    assert merged["metrics"]["cpu_time"]["eligible"]
    assert merged["metrics"]["network_io"] == current["metrics"]["network_io"]
    execution["window"]["requested_start_ns"] = "1"
    assert promote_execution_observation(current, execution) is current


def test_collector_failure_uses_dedicated_cgroup_with_truthful_window(tmp_path):
    _cgroup_files(tmp_path, 100, 10, 200)
    scope = ResourceScope(kind="cgroup-v2", cgroup_path=str(tmp_path),
                          attribution_source="exclusive-execution-cgroup")

    def failed(_):
        raise RuntimeError("BCC permission denied")

    monitor = EbpfToolCallMonitor(failed)
    monitor.begin("call", scope)
    _cgroup_files(tmp_path, 300, 110, 400)
    result = monitor.complete("call")
    _validator().validate(result)
    assert result["backend"] == "cgroup-v2"
    assert result["fallback_used"]
    assert "permission denied" in result["fallback_reason"]
    assert result["metrics"]["cpu_time"]["value_seconds"] == .0002
    assert result["metrics"]["disk_io"]["read_bytes"] == 100
    assert result["metrics"]["memory_charge_peak"]["value_bytes"] == 400
    assert not result["metrics"]["memory_peak"]["available"]
    assert not any(m["eligible"] for m in result["metrics"].values())


def test_shared_scope_never_falls_back_to_container_counters(tmp_path):
    from clawtune_sidecar.monitoring.cgroup_fallback import CgroupFallbackWindow
    _cgroup_files(tmp_path, 0, 0, 200)
    assert CgroupFallbackWindow.open(ResourceScope(
        kind="cgroup-v2", cgroup_path=str(tmp_path), attribution_source="shared-sandbox-container",
    )) is None


def test_fallback_does_not_reuse_old_memory_high_water_mark_or_reset_counters(tmp_path):
    from clawtune_sidecar.monitoring.cgroup_fallback import CgroupFallbackWindow
    _cgroup_files(tmp_path, 100, 100, 200)
    scope = ResourceScope(kind="cgroup-v2", cgroup_path=str(tmp_path),
                          attribution_source="exclusive-execution-cgroup")
    window = CgroupFallbackWindow.open(scope)
    _cgroup_files(tmp_path, 10, 150, 200)
    result = window.finish()
    assert not result["metrics"]["cpu_time"]["available"]
    assert "memory_charge_peak" not in result["metrics"]
    assert result["metrics"]["disk_io"]["read_bytes"] == 50


def test_sparse_ebpf_does_not_trigger_cgroup_fallback(tmp_path):
    class Window:
        started_ns = 0

        def finish(self, *_args, **_kwargs):
            return unavailable_observation("insufficient_samples")

    _cgroup_files(tmp_path, 0, 0, 0)
    monitor = EbpfToolCallMonitor(lambda _: Window())
    monitor.begin("call", ResourceScope(kind="cgroup-v2", cgroup_path=str(tmp_path),
                                        attribution_source="exclusive-execution-cgroup"))
    _cgroup_files(tmp_path, 100, 100, 100)
    assert not monitor.complete("call")["fallback_used"]


def test_execution_cpu_total_subtracts_exec_baseline_independently_of_peak():
    from tool_resource.telemetry import _task_io_totals
    rows = [
        dict(type="exec_boundary", host_pid=1, host_tid=1, exec_seq=0, ts_ns=100, cpu_ns=1000),
        dict(type="exit_boundary", host_pid=1, host_tid=1, exec_seq=0, ts_ns=200, cpu_ns=1300),
    ]
    clause = SimpleNamespace(host_pid=1, exec_seq=0, t_exec_ns=100, t_end_ns=200)
    totals, reason, _ = _task_io_totals(rows, rows, clause, {}, counter_fields=("cpu_ns",))
    assert totals == (300,)
    assert reason == "ok"


def test_finish_exception_releases_lease_and_uses_existing_fallback_baseline(tmp_path):
    released = []

    class Window:
        started_ns = 0
        lease_id = 1
        source = SimpleNamespace(release=released.append)

        def finish(self, *_args, **_kwargs):
            raise RuntimeError("collector stopped")

    _cgroup_files(tmp_path, 100, 100, 100)
    monitor = EbpfToolCallMonitor(lambda _: Window())
    monitor.begin("call", ResourceScope(kind="cgroup-v2", cgroup_path=str(tmp_path),
                                        attribution_source="exclusive-execution-cgroup"))
    _cgroup_files(tmp_path, 200, 200, 200)
    result = monitor.complete("call")
    assert released == [1]
    assert result["fallback_used"]
    assert "collector stopped" in result["fallback_reason"]
    assert result["metrics"]["cpu_time"]["value_seconds"] == .0001


def test_same_cgroup_pid_resolution_preserves_pre_action_events(monkeypatch, tmp_path):
    import threading
    from clawtune_sidecar.monitoring import ebpf_tool
    scope = ResourceScope(kind="pid", pid=123, root_pid=123, cgroup_path=str(tmp_path))
    events = _events(1_000_000_000, 2_000_000_000)
    window = ebpf_tool._KernelWindow(
        source=None, lease_id=1, buffer=SimpleNamespace(lock=threading.Lock(), events=events),
        bpf=None, root_pid=1, root_starttime_ticks=1, started_ns=900_000_000,
        initial_pids={1, 123}, shared=True, loss_before={}, cgroup_id=tmp_path.stat().st_ino,
    )
    monkeypatch.setattr(ebpf_tool, "_pid_starttime_ticks", lambda pid: 99)
    monkeypatch.setattr(ebpf_tool, "_existing_descendants", lambda pid: {pid})
    assert window.narrow_scope(scope)
    assert window.started_ns == 900_000_000
    assert window.buffer.events is events
    selected, pids = window._select_lineage(events, 2_000_000_000, 1_000_000_000)
    assert selected == events and pids == {123}
    assert not window.shared
    other = tmp_path / "other"
    other.mkdir()
    assert not window.narrow_scope(scope.model_copy(update={"cgroup_path": str(other)}))


def test_default_ebpf_path_completes_and_preserves_environment_memory(monkeypatch):
    from clawtune_sidecar.monitoring.tool_runtime import apply_resource_observation
    memory = {"memory_eligible": True, "memory_total_peak_bytes": 20,
              "memory_baseline_bytes": 10, "memory_extra_peak_bytes": 10}
    class Memory:
        def begin(self, key, scope):
            self.key = key
        def complete(self, key, *, started_at, ended_at):
            assert key == self.key
            assert ended_at - started_at == 1
            return memory
        def poll(self):
            pass
    class Ebpf:
        def begin(self, *args):
            pass
        def complete(self, *args, **kwargs):
            return unavailable_observation("ebpf_unavailable")
        def stop(self):
            pass
    monkeypatch.setattr("clawtune_sidecar.monitoring.environment_memory.EnvironmentMemoryMonitor", Memory)
    monitor = RealtimeToolMonitor(ebpf_monitor=Ebpf())
    request = _request()
    data = {k: v for k, v in request.model_dump().items() if k in ToolCompletedEvent.model_fields}
    data.update(event_id="end", occurred_at="2026-07-16T03:23:01Z", duration_ms=1000,
                succeeded=True, decision_id=None, lease_id=None, error_type=None, error_digest=None)
    try:
        assert monitor._memory_poller.is_alive()
        monitor.begin(request, "unknown")
        sample = monitor.complete(ToolCompletedEvent.model_validate(data))
        assert sample.environment_memory == memory
        replaced = apply_resource_observation(sample, unavailable_observation("replacement"))
        assert replaced.environment_memory == memory
    finally:
        monitor.stop()
