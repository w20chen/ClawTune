"""Regression cases for delayed completion, sparse sampling and host scopes."""
from types import SimpleNamespace
from datetime import datetime, timezone
import threading
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clawtune_sidecar.monitoring.environment_memory import EnvironmentMemoryMonitor
from clawtune_sidecar.monitoring.environment_memory import clause_memory_labels
from clawtune_sidecar.monitoring.tool_runtime import RealtimeToolMonitor
from test_tool_runtime_monitor import QueueSampler, _snapshot, _request
from clawtune_sidecar.contracts.models import ToolCompletedEvent


def collect(monkeypatch, points, start, end, *, container=True):
    clock = SimpleNamespace(now=points[0][0], value=points[0][1])
    monkeypatch.setattr("clawtune_sidecar.monitoring.environment_memory.time.time", lambda: clock.now)
    monkeypatch.setattr(EnvironmentMemoryMonitor, "_read", staticmethod(lambda _: clock.value))
    monitor = EnvironmentMemoryMonitor()
    monitor.begin("call", SimpleNamespace(cgroup_path="/sys/fs/cgroup/task", container_id="task" if container else None))
    for clock.now, clock.value in points[1:]:
        monitor.poll()
    clock.value = 999999  # completion-time memory must never enter this peak
    return monitor.complete("call", started_at=start, ended_at=end)


def test_delayed_finalizer_excludes_post_execution_peak(monkeypatch):
    result = collect(monkeypatch, [(10, 100), (10.05, 200), (10.1, 150), (20, 9000)], 10, 10.1)
    assert result["memory_total_peak_bytes"] == 200
    assert result["memory_extra_peak_bytes"] == 100
    assert result["memory_timeline"] == [(10, 100), (10.05, 200), (10.1, 150)]


@pytest.mark.parametrize("points,start,end,reason", [
    ([(10.1, 100), (10.15, 200)], 10, 10.2, "baseline_after_execution_start"),
    ([(10, 100), (10.05, 200)], 10, 15, "memory_sampling_gap"),
    ([(10, 100), (15, 200)], 10, 15, "memory_sampling_gap"),
    ([(10, 100), (11, 200)], 10.5, 11, "stale_memory_baseline"),
    ([(10, 100), (10.1, 200)], 10, 10.01, "no_in_execution_memory_sample"),
])
def test_unavailable_has_no_training_values(monkeypatch, points, start, end, reason):
    assert collect(monkeypatch, points, start, end) == {
        "memory_eligible": False, "memory_unavailable_reason": reason}


def test_host_service_is_not_a_task_environment(monkeypatch):
    assert collect(monkeypatch, [(10, 100), (10.05, 200)], 10, 10.05, container=False) == {
        "memory_eligible": False, "memory_unavailable_reason": "unverified_task_environment"}


@pytest.mark.parametrize("start,begin,eligible", [(10.15, 10.2, True), (10.15, 10.35, True), (10.04, 10.2, False)])
def test_recent_environment_sample_can_precede_delayed_begin(monkeypatch, start, begin, eligible):
    clock = SimpleNamespace(now=10.0, value=100)
    monkeypatch.setattr("clawtune_sidecar.monitoring.environment_memory.time.time", lambda: clock.now)
    monkeypatch.setattr(EnvironmentMemoryMonitor, "_read", staticmethod(lambda _: clock.value))
    scope = SimpleNamespace(cgroup_path="/sys/fs/cgroup/task", container_id="task")
    monitor = EnvironmentMemoryMonitor()
    monitor.begin("previous", scope)
    clock.now = 10.05
    monitor.poll()
    monitor.complete("previous", started_at=10, ended_at=10.05)
    clock.now = 10.1
    monitor.poll()  # Retain a recent idle-environment baseline.
    for timestamp in (10.2, 10.25, 10.3):
        if timestamp < begin:
            clock.now = timestamp
            monitor.poll()
    clock.now, clock.value = begin, 300
    monitor.begin("call", scope)
    clock.now = begin + .05
    monitor.poll()
    result = monitor.complete("call", started_at=start, ended_at=clock.now)
    assert result["memory_eligible"] is eligible
    if eligible:
        assert result["memory_baseline_bytes"] == 100
        assert result["memory_extra_peak_bytes"] == 200
    else:
        assert result["memory_unavailable_reason"] == "overlapping_environment_calls"


def test_truncated_memory_timeline_is_unavailable_without_training_values(monkeypatch):
    import clawtune_sidecar.monitoring.environment_memory as module
    clock = SimpleNamespace(now=10.0, value=100)
    monkeypatch.setattr(module.time, "time", lambda: clock.now)
    monkeypatch.setattr(EnvironmentMemoryMonitor, "_read", staticmethod(lambda _: clock.value))
    monkeypatch.setattr(module, "MAX_MEMORY_TIMELINE_POINTS", 2)
    monitor = EnvironmentMemoryMonitor()
    scope = SimpleNamespace(cgroup_path="/sys/fs/cgroup/task", container_id="task")
    monitor.begin("call", scope)
    clock.now = 10.05
    monitor.poll()
    clock.now = 10.10
    monitor.poll()
    assert monitor.complete("call", started_at=10, ended_at=10.10) == {
        "memory_eligible": False,
        "memory_unavailable_reason": "memory_timeline_truncated",
    }


def test_short_clause_without_interior_sample_has_no_memory_label(monkeypatch):
    environment = collect(monkeypatch, [(10, 100), (10.05, 200), (10.1, 150)], 10, 10.1)
    assert clause_memory_labels(environment, [{"ts_start": 10, "ts_end": 10.01}]) == [{}]
    assert clause_memory_labels(environment, [{"ts_start": 10, "ts_end": 10.1}])[0]["memory_total_peak_bytes"] == 200


def test_memory_availability_matches_public_schema(monkeypatch):
    schema_path = Path(__file__).resolve().parents[3] / "contracts" / "environment-memory.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    eligible = collect(monkeypatch, [(10, 100), (10.05, 200)], 10, 10.05)
    unavailable = collect(monkeypatch, [(10, 100)], 10, 10.01)
    validator.validate(eligible)
    validator.validate(unavailable)
    assert list(validator.iter_errors(unavailable | {"memory_total_peak_bytes": 0}))


def test_completion_clock_does_not_rewind_to_last_live_sample():
    sampler = QueueSampler([
        _snapshot(captured_at=10, cpu_s=0, rss=100, available=True, source="psutil-process-tree"),
        _snapshot(captured_at=30, cpu_s=None, rss=None, available=False, source="pid-unavailable"),
    ])
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    try:
        request = _request()
        monitor.begin(request, "unknown")
        data = {k: v for k, v in request.model_dump().items() if k in ToolCompletedEvent.model_fields}
        data.update(event_id="end", occurred_at=datetime.fromtimestamp(20, timezone.utc).isoformat(),
                    duration_ms=5000, succeeded=True, decision_id=None, lease_id=None,
                    error_type=None, error_digest=None)
        sample = monitor.complete(ToolCompletedEvent.model_validate(data))
        assert (sample.started_at, sample.ended_at) == (15, 20)
        assert sample.monitor_end_wall_s == 10
    finally:
        monitor.stop()


def test_memory_polling_continues_while_process_sampling_is_blocked(monkeypatch):
    entered, release, memory_polled = threading.Event(), threading.Event(), threading.Event()
    class SlowSampler:
        def snapshot(self, scope=None, *, net_mode="ignore"):
            if net_mode == "ignore":
                entered.set()
                release.wait(2)
            return _snapshot(captured_at=10, cpu_s=0, rss=100, available=True, source="psutil-process-tree")
    from clawtune_sidecar.contracts.models import ResourceScope
    monitor = RealtimeToolMonitor(sampler=SlowSampler(), poll_interval_s=.01)
    try:
        monitor.begin(_request(ResourceScope(pid=123)), "unknown")
        assert entered.wait(1)
        monkeypatch.setattr(monitor.environment_memory, "poll", memory_polled.set)
        assert memory_polled.wait(1)
    finally:
        release.set()
        monitor.stop()


def test_resource_poll_does_not_initialize_network_bcc(monkeypatch):
    from clawtune_sidecar.monitoring.process import ProcessResourceSampler
    from clawtune_sidecar.contracts.models import ResourceScope
    sampler = ProcessResourceSampler()
    def unexpected(*args):
        pytest.fail("poll initialized the network collector")
    monkeypatch.setattr(sampler, "_net_accounting", unexpected)
    assert sampler._snapshot_net(ResourceScope(pid=123), [123], "ignore") == (None, None)


def completion_data(**changes):
    request = _request()
    data = {k: v for k, v in request.model_dump().items() if k in ToolCompletedEvent.model_fields}
    data.update(event_id="end", occurred_at="1970-01-01T00:00:20Z", duration_ms=10000,
                succeeded=True, decision_id=None, lease_id=None, error_type=None, error_digest=None)
    return data | changes


@pytest.mark.parametrize("timestamp", ["bad", "2026-07-16T03:23:01", "2026-99-16T03:23:01Z"])
def test_bad_completion_timestamp_rejected_at_http_boundary(timestamp):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    @app.post("/completed")
    def completed(event: ToolCompletedEvent):
        pytest.fail("invalid timestamp reached completion handler")
    with TestClient(app) as client:
        assert client.post("/completed", json=completion_data(occurred_at=timestamp)).status_code == 422


@pytest.mark.parametrize("timestamp", ["2026-07-16T03:23:01z", "2026-07-16T03:23:01.123456789Z", "2026-07-16T11:23:01+08:00"])
def test_completion_accepts_supported_timestamp_precision(timestamp):
    event = ToolCompletedEvent.model_validate(completion_data(occurred_at=timestamp))
    assert datetime.fromisoformat(event.occurred_at).tzinfo is not None


@pytest.mark.parametrize("duration", [10000, 1000, 20000, 0])
def test_sparse_cpu_counter_is_unavailable_even_when_endpoints_look_aligned(duration):
    from clawtune_sidecar.predictors.tool_resource import completed_call_from_completion, _trace_cpu_window_aligned
    from tool_resource.runtime_kb import _target_values
    sampler = QueueSampler([
        _snapshot(captured_at=10, cpu_s=0, rss=100, available=True, source="psutil-process-tree"),
        _snapshot(captured_at=20, cpu_s=1, rss=100, available=True, source="psutil-process-tree"),
    ])
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    try:
        request = _request()
        monitor.begin(request, "unknown")
        event = ToolCompletedEvent.model_validate(completion_data(duration_ms=duration))
        sample = monitor.complete(event)
        assert sample.cpu_time_delta_s is None
        assert sample.cpu_utilization_avg_cores is None
        targets = _target_values(completed_call_from_completion(event, sample, repo="r", start=request))
        assert "cpu_avg_cores" not in targets
        assert "cpu_time_seconds" not in targets
        if duration == 0:
            assert "latency_ms" not in targets
        resources = {"monitor_start_wall_time_ns": "10000000000", "monitor_end_wall_time_ns": "20000000000"}
        assert _trace_cpu_window_aligned(resources, sample.started_at, sample.ended_at) == (duration == 10000)
    finally:
        monitor.stop()


def test_dense_cpu_counter_is_interpolated_to_action_boundaries():
    from dataclasses import replace
    sampler = QueueSampler([
        _snapshot(captured_at=9.95, cpu_s=0, rss=100, available=True, source="psutil-process-tree"),
        _snapshot(captured_at=11.05, cpu_s=.55, rss=100, available=True, source="psutil-process-tree"),
    ])
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    try:
        monitor.begin(_request(), "unknown")
        key = next(iter(monitor._active))
        points = [
            {"ts": 9.95 + i * .05, "cpu_time_s": i * .025,
             "source": "psutil-process-tree", "available": True}
            for i in range(23)
        ]
        monitor._active[key] = replace(monitor._active[key], timeline=points)
        sample = monitor.complete(ToolCompletedEvent.model_validate(completion_data(
            occurred_at="1970-01-01T00:00:11Z", duration_ms=1000)))
        assert sample.cpu_time_delta_s == pytest.approx(.5)
        assert sample.cpu_utilization_avg_cores == pytest.approx(.5)
    finally:
        monitor.stop()


def test_unknown_duration_does_not_discard_independent_pmu():
    from tool_resource.runtime_kb import CompletedCall, _target_values
    targets = _target_values(CompletedCall("r", "exec", None, 20, 20, pmu_cycles=123, pmu_eligible=True))
    assert "latency_ms" not in targets
    assert targets["pmu_cycles"] == 123


def test_cpu_peak_excludes_samples_outside_action():
    from dataclasses import replace
    sampler = QueueSampler([
        _snapshot(captured_at=10, cpu_s=0, rss=100, available=True, source="psutil-process-tree"),
        _snapshot(captured_at=12, cpu_s=101, rss=100, available=True, source="psutil-process-tree"),
    ])
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    try:
        monitor.begin(_request(), "unknown")
        key = next(iter(monitor._active))
        points = [{"ts": 10 + i / 20, "cpu_time_s": i * 5 if i <= 20 else 100 + (i - 20) / 20,
                   "source": "psutil-process-tree", "available": True} for i in range(40)]
        monitor._active[key] = replace(monitor._active[key], timeline=points)
        event = ToolCompletedEvent.model_validate(completion_data(
            occurred_at="1970-01-01T00:00:12Z", duration_ms=1000))
        sample = monitor.complete(event)
        assert sample.cpu_peak_cores == pytest.approx(1)
        assert sample.cpu_utilization_avg_cores == pytest.approx(1)
    finally:
        monitor.stop()
