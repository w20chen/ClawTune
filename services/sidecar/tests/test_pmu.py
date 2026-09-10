from __future__ import annotations

import errno
import copy
import ctypes
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
    assert profile.derived == {"ipc": 0.5, "llc_mpki": 1.0, "llc_miss_rate": 0.1}
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


def test_permission_fallback_is_user_only_and_not_kb_eligible() -> None:
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


def test_consumed_profile_remains_idempotent_for_a_delayed_exit() -> None:
    collector = PmuCollector(max_active=1, backend=FakePerfBackend())
    collector.begin("exec", 12)
    original = collector.finish("exec")

    assert collector.take("exec") is original
    assert collector.finish("exec") is original


def test_only_reliable_pmu_metrics_enter_online_kb() -> None:
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


def test_reliable_metrics_are_gated_independently_when_a_ratio_is_undefined() -> None:
    backend = FakePerfBackend()
    backend.values["llc_read_accesses"] = 0
    backend.values["llc_read_misses"] = 0
    collector = PmuCollector(max_active=1, backend=backend)
    collector.begin("exec", 12)

    metrics = _quality_gated_pmu_metrics(collector.finish("exec").to_dict())

    assert metrics == {
        "ipc": 0.5,
        "llc_mpki": 0.0,
        "llc_miss_rate": None,
        "eligible": True,
    }


def test_online_kb_rejects_generic_cache_miss_relabeling() -> None:
    collector = PmuCollector(max_active=1, backend=FakePerfBackend())
    collector.begin("exec", 12)
    profile = collector.finish("exec").to_dict()
    profile["events"]["llc_read_misses"]["semantics"] = "PERF_COUNT_HW_CACHE_MISSES"

    assert _quality_gated_pmu_metrics(profile)["eligible"] is False


@pytest.mark.parametrize("reason", ["aborted", "signal_terminated", "completion_fallback", "root_exited_before_callback"])
def test_incomplete_lifecycle_never_produces_training_labels(reason):
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
    assert _quality_gated_pmu_metrics(profile) == {"ipc": .5, "llc_mpki": 1., "llc_miss_rate": .1, "eligible": True}
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
