from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_resource.clause_bridge import ExecImageRecord, _clause_status
from tool_resource.telemetry import (
    BPF_PROGRAM,
    ClauseTelemetryCollector,
    RawRun,
    SENTINEL,
    _attribute,
    _bpf_setup_error_message,
    _clauses_and_lineage,
    _command_tree_provenance,
    _container_init_pid,
    _ensure_bcc_importable,
    _isolate_call_events,
    _observed_container_cgroup_ids,
    _runtime_response_exit_code,
    _sampled_peak_rss,
    _scope_identity_inodes,
    _syscall_symbol_candidates,
    shell_command_lookup_failure_evidence,
    validate_clause_telemetry_smoke,
)
from tool_resource.mvdan_client import MvdanClientError


def test_host_scope_never_expands_through_pid_namespace(monkeypatch, tmp_path) -> None:
    cgroup = tmp_path / "exec"
    child = cgroup / "child"
    child.mkdir(parents=True)
    monkeypatch.setattr(
        "tool_resource.telemetry._discover_cgroup_inodes_from_proc",
        lambda _pid: pytest.fail("host scope must not scan all host PIDs"),
    )

    cgroups, pid_namespaces = _scope_identity_inodes(cgroup, 4242, None)

    assert cgroups == set()
    assert pid_namespaces == set()


def test_container_sharing_sidecar_pid_namespace_skips_proc_scan(
    monkeypatch,
    tmp_path,
) -> None:
    cgroup = tmp_path / "container"
    cgroup.mkdir()
    monkeypatch.setattr(
        "tool_resource.telemetry._pid_namespace_inode_for_pid",
        lambda _pid: 123,
    )
    monkeypatch.setattr(
        "tool_resource.telemetry._discover_cgroup_inodes_from_proc",
        lambda _pid: pytest.fail("shared host namespace must not be scanned"),
    )

    cgroups, pid_namespaces = _scope_identity_inodes(cgroup, 4242, "abc123")

    assert cgroup.stat().st_ino in cgroups
    assert pid_namespaces == set()


def test_telemetry_submodule_import_does_not_require_third_party_packages() -> None:
    scheduler_src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(scheduler_src)!r}); "
        "from tool_resource.telemetry import BPF_PROGRAM; "
        "assert 'CLAW_RSS_COUNTER_ADDR' in BPF_PROGRAM"
    )

    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_container_init_pid_falls_back_to_docker_socket_after_cli_api_failure(monkeypatch) -> None:
    class FailedInspect:
        returncode = 1
        stdout = ""
        stderr = "client version 1.41 is too old"

    monkeypatch.setattr("tool_resource.telemetry.subprocess.run", lambda *args, **kwargs: FailedInspect())
    monkeypatch.setattr(
        "tool_resource.telemetry._container_init_pid_from_socket",
        lambda container_id: 4321,
    )

    assert _container_init_pid("abc123", "docker") == 4321


def test_bpf_program_uses_wrapper_aware_syscall_kprobes() -> None:
    assert "TRACEPOINT_PROBE(syscalls, sys_enter_execve" not in BPF_PROGRAM
    assert "TRACEPOINT_PROBE(syscalls, sys_exit_execve" not in BPF_PROGRAM
    assert "int capture_sys_execve(struct pt_regs *ctx)" in BPF_PROGRAM
    assert "int capture_sys_execve_return(struct pt_regs *ctx)" in BPF_PROGRAM
    assert "#ifdef CONFIG_ARCH_HAS_SYSCALL_WRAPPER" in BPF_PROGRAM
    assert "struct pt_regs *regs = syscall_argument_regs(ctx);" in BPF_PROGRAM
    assert "&PT_REGS_PARM1(regs)" in BPF_PROGRAM
    assert "&PT_REGS_PARM2(regs)" in BPF_PROGRAM
    assert "&PT_REGS_PARM3(regs)" in BPF_PROGRAM
    assert "bpf_probe_read_kernel(&argv, sizeof(argv)" in BPF_PROGRAM
    assert "(const char *)PT_REGS_PARM1(regs)" in BPF_PROGRAM
    assert "(const char *const *)PT_REGS_PARM2(regs)" in BPF_PROGRAM
    assert "(const char *)PT_REGS_PARM2(regs)" in BPF_PROGRAM
    assert "(const char *const *)PT_REGS_PARM3(regs)" in BPF_PROGRAM
    assert "PT_REGS_RC(ctx)" in BPF_PROGRAM
    assert "u64 pid_namespace_inode;" in BPF_PROGRAM
    assert "current_pid_namespace_inode" in BPF_PROGRAM
    assert "pid_ns_for_children" in BPF_PROGRAM


def test_bpf_program_feature_probes_both_linux_rss_stat_layouts() -> None:
    assert "#if LINUX_VERSION_CODE" not in BPF_PROGRAM
    assert "#include <linux/percpu_counter.h>" in BPF_PROGRAM
    assert "__builtin_types_compatible_p" in BPF_PROGRAM
    assert "struct percpu_counter[NR_MM_COUNTERS]" in BPF_PROGRAM
    assert "sizeof(atomic_long_t[NR_MM_COUNTERS])" in BPF_PROGRAM
    assert "__alignof__(atomic_long_t[NR_MM_COUNTERS])" in BPF_PROGRAM
    assert "claw_rss_stat_layout_must_be_supported" in BPF_PROGRAM
    assert "__builtin_choose_expr" in BPF_PROGRAM
    assert "((struct percpu_counter *)&((mm)->rss_stat))[(index)].count" in BPF_PROGRAM
    assert "((atomic_long_t *)&((mm)->rss_stat))[(index)].counter" in BPF_PROGRAM
    assert "CLAW_RSS_STAT_IS_PERCPU ? 2 : 1" in BPF_PROGRAM
    assert "(file > 0 ? (s64)file : 0)" in BPF_PROGRAM
    assert "(anon > 0 ? (s64)anon : 0)" in BPF_PROGRAM
    assert "(shmem > 0 ? (s64)shmem : 0)" in BPF_PROGRAM


def test_ensure_bcc_importable_aliases_openEuler_bpfcc(monkeypatch) -> None:
    fake = SimpleNamespace(
        __name__="bpfcc",
        __file__="/usr/lib/python3.11/site-packages/bpfcc/__init__.py",
        BPF=object(),
        PerfSWConfig=object(),
        PerfType=object(),
    )

    def import_module(name: str):
        if name == "bcc":
            raise ImportError("no module named bcc")
        assert name == "bpfcc"
        return fake

    monkeypatch.delitem(sys.modules, "bcc", raising=False)
    monkeypatch.setattr(
        "tool_resource.telemetry.importlib.import_module",
        import_module,
    )

    assert _ensure_bcc_importable() is fake
    assert sys.modules["bcc"] is fake


def test_sampled_rss_provenance_marks_percpu_global_counter_as_approximate() -> None:
    samples = [
        {
            "type": "perf",
            "ts_ns": 110,
            "rss_pages": 10,
            "rss_counter_backend": 2,
            "mm_ptr": 7,
            "host_tid": 42,
        },
        {
            "type": "perf",
            "ts_ns": 120,
            "rss_pages": 11,
            "rss_counter_backend": 2,
            "mm_ptr": 7,
            "host_tid": 42,
        },
    ]

    value, reason, provenance = _sampled_peak_rss(
        samples,
        SimpleNamespace(t_exec_ns=100, t_end_ns=130),
    )

    assert value is not None
    assert reason == "ok"
    assert provenance["counter_backends"] == [
        "percpu_counter_global_approximation"
    ]
    assert provenance["counter_exact"] is False


def test_syscall_symbol_candidates_include_bcc_and_common_kernel_names() -> None:
    class FakeBPF:
        @staticmethod
        def get_syscall_fnname(name: str) -> bytes:
            return f"bcc_{name}".encode()

    candidates = _syscall_symbol_candidates(FakeBPF, "execve")

    assert candidates[0] == "bcc_execve"
    assert "__x64_sys_execve" in candidates
    assert "__arm64_sys_execve" in candidates
    assert "sys_execve" in candidates


def test_syscall_symbol_candidates_prefer_arm64_on_arm_host(monkeypatch) -> None:
    class FakeBPF:
        @staticmethod
        def get_syscall_fnname(_name: str) -> bytes:
            raise RuntimeError("kernel lookup unavailable")

    monkeypatch.setattr("tool_resource.telemetry.platform.machine", lambda: "aarch64")

    candidates = _syscall_symbol_candidates(FakeBPF, "execve")

    assert candidates[:3] == (
        "__arm64_sys_execve",
        "__x64_sys_execve",
        "__ia32_sys_execve",
    )


def test_syscall_symbol_candidates_prefer_x64_on_x86_host(monkeypatch) -> None:
    class FakeBPF:
        @staticmethod
        def get_syscall_fnname(_name: str) -> bytes:
            raise RuntimeError("kernel lookup unavailable")

    monkeypatch.setattr("tool_resource.telemetry.platform.machine", lambda: "x86_64")

    candidates = _syscall_symbol_candidates(FakeBPF, "execve")

    assert candidates[:3] == (
        "__x64_sys_execve",
        "__ia32_sys_execve",
        "__arm64_sys_execve",
    )


def test_bpf_permission_errors_are_reported_as_runtime_permission_failures() -> None:
    message = _bpf_setup_error_message(
        "BPF module load failed",
        RuntimeError("could not open bpf map: pending_seq, error: Operation not permitted"),
    )

    assert "permission denied while creating BPF maps/probes/events" in message
    assert "Stage-2 clause telemetry requires root" in message
    assert "diagnostics=" in message


def _smoke_raw_run(**overrides: object) -> RawRun:
    values: dict[str, object] = {
        "cgroup_id": 7,
        "quota_cores": 1.0,
        "status": 0,
        "wall_ns": 1,
        "usage_usec": 1,
        "ringbuf_reserve_failures": 0,
        "perf_sample_count": 0,
        "oracle_peak_rss_kb": 1,
        "oracle_samples": 1,
        "marker": True,
        "events": [
            {"type": "exec_meta", "arg": "/bin/sh"},
            {"type": "exec_arg", "arg": "/bin/sh"},
            {"type": "exec_arg", "arg": "-c"},
            {"type": "exec_boundary"},
        ],
        "lifecycle_map_entries": {"current_seq": 0, "pending_seq": 0},
    }
    values.update(overrides)
    return RawRun(**values)


def test_clause_telemetry_smoke_requires_real_argv(monkeypatch) -> None:
    monkeypatch.setattr(
        "tool_resource.telemetry.collect_case",
        lambda *args, **kwargs: _smoke_raw_run(
            argv_read_failures=2,
            events=[
                {"type": "exec_meta", "arg": ""},
                {"type": "exec_boundary"},
            ],
        ),
    )

    with pytest.raises(
        RuntimeError,
        match=r"telemetry loss=2.*no non-empty exec argv.*executable path",
    ):
        validate_clause_telemetry_smoke()


def test_clause_telemetry_smoke_reports_healthy_capture(monkeypatch) -> None:
    monkeypatch.setattr(
        "tool_resource.telemetry.collect_case",
        lambda *args, **kwargs: _smoke_raw_run(),
    )

    assert validate_clause_telemetry_smoke() == {
        "ok": True,
        "event_count": 4,
        "exec_arg_count": 2,
        "exec_boundary_count": 1,
        "requested_executable_count": 1,
        "loss_counts": {
            "ringbuf_reserve_failures": 0,
            "argv_read_failures": 0,
            "argv_boundary_read_failures": 0,
        },
    }


def test_runtime_response_exit_code_accepts_scheduler_and_sdk_names() -> None:
    assert _runtime_response_exit_code({"returncode": 7}) == 7
    assert _runtime_response_exit_code({"exit_code": 8}) == 8
    assert _runtime_response_exit_code({"returncode": 7, "exit_code": 8}) == 7
    assert _runtime_response_exit_code({"exit_code": True}) is None


def test_live_pipeline_lookup_failure_does_not_require_source_replay(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "tool_resource.clause_bridge.shell_lookup_exit_semantics",
        lambda command, head, exit_code: (
            "nonfinal_pipeline_masked_0"
            if (command, head, exit_code) == ("pip install . | tail -5", "pip", 0)
            else None
        ),
    )

    evidence = shell_command_lookup_failure_evidence(
        command="pip install . | tail -5",
        source_tool_call_id="",
        replay_tool_call_id="call-1",
        source_command="",
        source_tool_result="",
        replay_result="/bin/sh: 1: pip: not found",
        replay_stderr="",
        replay_exit_code=0,
    )

    assert evidence is not None
    assert evidence.evidence_mode == "live_execution"
    assert evidence.executable_head == "pip"
    assert evidence.source_tool_call_id == ""
    assert evidence.source_channel == "unavailable"
    assert evidence.exit_code_semantics == "nonfinal_pipeline_masked_0"


def test_live_lookup_failure_rejects_unanchored_diagnostic(monkeypatch) -> None:
    monkeypatch.setattr(
        "tool_resource.clause_bridge.shell_lookup_exit_semantics",
        lambda *_args: "direct_command_not_found_127",
    )

    assert (
        shell_command_lookup_failure_evidence(
            command="missing",
            source_tool_call_id="",
            replay_tool_call_id="call-1",
            source_command="",
            source_tool_result="",
            replay_result="the missing command was not found",
            replay_stderr="",
            replay_exit_code=127,
        )
        is None
    )


def test_clause_status_uses_terminal_image_from_owned_root_chain() -> None:
    wrapper = ExecImageRecord(
        host_pid=42,
        exec_seq=1,
        t_exec_ns=1,
        t_end_ns=2,
        bin="env",
        argv=("env", "false"),
        terminal=False,
        cpu_windows=(),
        rss_bins=(),
    )
    workload = ExecImageRecord(
        host_pid=42,
        exec_seq=2,
        t_exec_ns=2,
        t_end_ns=3,
        bin="false",
        argv=("false",),
        terminal=True,
        cpu_windows=(),
        rss_bins=(),
        normal_exit_status=1,
    )

    assert _clause_status((42,), (wrapper, workload)) == {
        "state": "exited",
        "exit_code": 1,
        "signal": None,
        "succeeded": False,
        "reason": None,
        "source": "root_exec_chain_terminal",
    }


def _event(
    event_type: str,
    ts_ns: int,
    host_pid: int,
    *,
    exec_seq: int = 1,
    child_host_pid: int = 0,
    arg_index: int = 0,
    arg: str = "",
) -> dict[str, object]:
    return {
        "type": event_type,
        "ts_ns": ts_ns,
        "cgroup_id": 0,
        "pid_namespace_inode": 0,
        "host_pid": host_pid,
        "host_tid": host_pid,
        "child_host_pid": child_host_pid,
        "exec_seq": exec_seq,
        "arg_index": arg_index,
        "arg_chunk_index": 0,
        "arg_flags": 0,
        "arg": arg,
        "exit_code": 0,
    }


def test_container_pid_set_uses_pid_namespace_when_cgroup_mismatches() -> None:
    from tool_resource.telemetry import _container_pid_set

    pids = _container_pid_set(
        [
            {
                "type": "exec_boundary",
                "cgroup_id": 1054166,
                "pid_namespace_inode": 777,
                "host_pid": 42,
                "child_host_pid": 0,
            },
            {
                "type": "exec_boundary",
                "cgroup_id": 9294,
                "pid_namespace_inode": 1,
                "host_pid": 99,
                "child_host_pid": 0,
            },
        ],
        10,
        cgroup_inodes={1055634},
        pid_namespace_inodes={777},
    )

    assert 42 in pids
    assert 99 not in pids


def test_dynamic_cgroup_discovery_rejects_unrelated_exec_boundaries() -> None:
    events = [
        {
            "type": "exec_boundary",
            "cgroup_id": 101,
            "pid_namespace_inode": 777,
            "host_pid": 42,
        },
        {
            "type": "exec_boundary",
            "cgroup_id": 202,
            "pid_namespace_inode": 1,
            "host_pid": 99,
        },
        {
            "type": "exec_boundary",
            "cgroup_id": 303,
            "pid_namespace_inode": 0,
            "host_pid": 123,
        },
    ]

    assert _observed_container_cgroup_ids(events, {123}, {777}) == {101, 303}


def _shell_tree(
    *,
    entry_pid: int,
    shell_pid: int,
    child_pid: int,
    command: str,
    start_ns: int,
) -> list[dict[str, object]]:
    return [
        _event(
            "fork",
            start_ns,
            entry_pid,
            child_host_pid=shell_pid,
            exec_seq=0,
        ),
        _event("exec_arg", start_ns + 1, shell_pid, arg_index=0, arg="/bin/sh"),
        _event("exec_arg", start_ns + 2, shell_pid, arg_index=1, arg="-c"),
        _event("exec_arg", start_ns + 3, shell_pid, arg_index=2, arg=command),
        _event("exec_boundary", start_ns + 4, shell_pid),
        _event(
            "fork",
            start_ns + 5,
            shell_pid,
            child_host_pid=child_pid,
        ),
        _event("exec_arg", start_ns + 6, child_pid, arg_index=0, arg="/bin/echo"),
        _event("exec_arg", start_ns + 7, child_pid, arg_index=1, arg=command),
        _event("exec_boundary", start_ns + 8, child_pid),
        _event("exit_boundary", start_ns + 9, child_pid),
        _event("exit_boundary", start_ns + 10, shell_pid),
    ]


def test_call_event_isolation_selects_exact_parallel_launcher_tree() -> None:
    first = _shell_tree(
        entry_pid=10,
        shell_pid=11,
        child_pid=12,
        command="echo first",
        start_ns=100,
    )
    second = _shell_tree(
        entry_pid=20,
        shell_pid=21,
        child_pid=22,
        command="echo second",
        start_ns=105,
    )

    selected, provenance = _isolate_call_events(
        sorted(first + second, key=lambda event: int(event["ts_ns"])),
        "echo second",
    )

    assert provenance["mode"] == "exact_launcher_command"
    assert provenance["window_root_pids"] == [11, 21]
    assert provenance["matching_root_pids"] == [21]
    assert provenance["selected_root_pid"] == 21
    assert {event["host_pid"] for event in selected} == {20, 21, 22}


def test_call_event_isolation_keeps_ambiguous_parallel_trees_fail_closed() -> None:
    first = _shell_tree(
        entry_pid=10,
        shell_pid=11,
        child_pid=12,
        command="echo same",
        start_ns=100,
    )
    second = _shell_tree(
        entry_pid=20,
        shell_pid=21,
        child_pid=22,
        command="echo same",
        start_ns=105,
    )
    events = sorted(first + second, key=lambda event: int(event["ts_ns"]))

    selected, provenance = _isolate_call_events(events, "echo same")

    assert selected == events
    assert provenance["mode"] == "unresolved"
    assert provenance["matching_root_pids"] == [11, 21]


def test_trusted_execution_root_isolates_identical_parallel_commands() -> None:
    first = _shell_tree(
        entry_pid=10,
        shell_pid=11,
        child_pid=12,
        command="echo same",
        start_ns=100,
    )
    second = _shell_tree(
        entry_pid=20,
        shell_pid=21,
        child_pid=22,
        command="echo same",
        start_ns=105,
    )

    selected, provenance = _isolate_call_events(
        sorted(first + second, key=lambda event: int(event["ts_ns"])),
        "echo same",
        trusted_root_pid=21,
    )

    assert provenance["mode"] == "trusted_execution_root"
    assert provenance["trusted_root_pid"] == 21
    assert {event["host_pid"] for event in selected} == {20, 21, 22}
    assert not any(event["host_pid"] == 11 for event in selected)


def test_trusted_execution_root_remaps_container_pid_to_exact_host_exec() -> None:
    events = _shell_tree(
        entry_pid=600_001,
        shell_pid=600_042,
        child_pid=600_043,
        command="echo mapped",
        start_ns=100,
    )[1:]

    selected, isolation = _isolate_call_events(
        events,
        "echo mapped",
        trusted_root_pid=42,
        allow_trusted_root_pid_remap=True,
    )

    assert isolation == {
        "mode": "trusted_execution_root_pid_namespace_remap",
        "trusted_root_pid": 600_042,
        "claimed_trusted_root_pid": 42,
        "remap_evidence": "exact_registered_root_shell",
        "selected_pid_count": 2,
        "raw_window_event_count": len(events),
        "selected_event_count": len(events),
    }
    clauses, fork_parent = _clauses_and_lineage(selected)
    entry_pid, root_pids, command_tree = _command_tree_provenance(
        clauses,
        fork_parent,
        trusted_root_pid=isolation["trusted_root_pid"],
    )
    assert entry_pid == 600_042
    assert root_pids == {600_042}
    assert command_tree["status"] == "ok"


class _KprobeHitTable:
    def __getitem__(self, _key: object) -> SimpleNamespace:
        return SimpleNamespace(value=17)


class _AnalysisTestBpf:
    def __getitem__(self, name: str) -> _KprobeHitTable:
        assert name == "kprobe_total_hits"
        return _KprobeHitTable()


def _active_test_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> ClauseTelemetryCollector:
    collector = ClauseTelemetryCollector.unavailable(
        repo="/repo",
        artifact_path=tmp_path / "artifact.json",
        reason="test bootstrap",
    )
    collector.state = "active"
    collector._disabled_reason = None
    collector._first_disabled_call = None
    collector._integrity_errors = []
    collector._cleanup_status = "pending"
    collector._bpf = _AnalysisTestBpf()
    monkeypatch.setattr(
        "tool_resource.telemetry._counter",
        lambda _bpf, _name: 0,
    )
    monkeypatch.setattr(
        "tool_resource.telemetry._loss_delta",
        lambda _bpf, _token: {
            "ringbuf_reserve_failures": 0,
            "argv_read_failures": 0,
            "argv_boundary_read_failures": 0,
        },
    )
    monkeypatch.setattr(
        "tool_resource.telemetry._loss_counts",
        lambda _bpf: {
            "ringbuf_reserve_failures": 0,
            "argv_read_failures": 0,
            "argv_boundary_read_failures": 0,
        },
    )
    monkeypatch.setattr("tool_resource.telemetry.time.sleep", lambda _delay: None)

    def close_bpf() -> None:
        collector._closed = True
        collector._cleanup_status = "ok"

    monkeypatch.setattr(collector, "_close_bpf", close_bpf)
    return collector


def test_post_capture_analysis_failure_preserves_healthy_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    collector = _active_test_collector(monkeypatch, tmp_path)
    token = collector.begin_tool_call("call-analysis", "printf ok")

    def fail_analysis(**_kwargs: object) -> None:
        raise MvdanClientError("adapter unavailable")

    monkeypatch.setattr(collector, "_summarize_call", fail_analysis)

    call = collector.finish_tool_call(
        token,
        replay_response={"result": "ok", "stderr": "", "returncode": 0},
    )

    assert call["telemetry_quality"] == "invalid"
    assert call["invalid_reasons"][0]["kind"] == "analysis_failure"
    assert collector.state == "active"
    assert collector._disabled_reason is None
    assert collector._first_disabled_call is None

    collector.finalize()
    artifact = json.loads(collector.artifact_path.read_text(encoding="utf-8"))

    assert artifact["collector"]["health"] == "healthy"
    assert artifact["collector"]["state_before_close"] == "active"
    assert artifact["collector"]["kprobe_total_hits"] == 17
    assert artifact["collector"]["invalid_call_count"] == 1
    assert artifact["collector"]["disabled_reason"] is None
    assert artifact["telemetry_quality"] == "invalid"
    assert artifact["formal_completeness"] == "partial"
    assert artifact["collection_validity"] == "invalid"


def test_safety_guard_analysis_failure_is_call_granular(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    collector = _active_test_collector(monkeypatch, tmp_path)

    def fail_analysis(**_kwargs: object) -> None:
        raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(collector, "_summarize_call", fail_analysis)

    call = collector.record_safety_guard_blocked(
        "call-guard",
        "rm guarded",
        "blocked",
    )

    assert call["telemetry_quality"] == "invalid"
    assert call["invalid_reasons"][0]["kind"] == "analysis_failure"
    assert collector.state == "active"
    assert collector._disabled_reason is None


def test_trusted_execution_root_pid_remap_requires_explicit_cgroup_permission() -> None:
    events = _shell_tree(
        entry_pid=600_001,
        shell_pid=600_042,
        child_pid=600_043,
        command="echo mapped",
        start_ns=100,
    )[1:]

    selected, isolation = _isolate_call_events(
        events,
        "echo mapped",
        trusted_root_pid=42,
    )

    assert selected == []
    assert isolation["mode"] == "trusted_execution_root"
    assert isolation["trusted_root_pid"] == 42


def test_trusted_execution_root_pid_remap_fails_closed_when_ambiguous() -> None:
    first = _shell_tree(
        entry_pid=600_001,
        shell_pid=600_011,
        child_pid=600_012,
        command="echo same",
        start_ns=100,
    )
    second = _shell_tree(
        entry_pid=600_020,
        shell_pid=600_021,
        child_pid=600_022,
        command="echo same",
        start_ns=105,
    )

    selected, isolation = _isolate_call_events(
        sorted(first + second, key=lambda event: int(event["ts_ns"])),
        "echo same",
        trusted_root_pid=42,
        allow_trusted_root_pid_remap=True,
    )

    assert selected == []
    assert isolation["mode"] == "trusted_execution_root"
    assert isolation["trusted_root_pid"] == 42


def test_trusted_execution_root_replaces_missing_initial_fork_ancestry() -> None:
    metric = SimpleNamespace(host_pid=42, t_exec_ns=100)

    entry_pid, root_pids, provenance = _command_tree_provenance(
        [metric],
        {},
        fork_records={},
        trusted_root_pid=42,
    )

    assert entry_pid == 42
    assert root_pids == {42}
    assert provenance["status"] == "ok"
    assert provenance["identity_anchor"] == {
        "kind": "launcher_started",
        "host_pid": 42,
    }


def test_trusted_root_pre_exec_sample_is_structural_without_missing_generation() -> None:
    events = [
        _event("perf", 99, 42, exec_seq=SENTINEL),
        _event("exec_arg", 100, 42, arg_index=0, arg="/bin/sh"),
        _event("exec_boundary", 101, 42),
        _event("exit_boundary", 102, 42),
    ]
    clauses, fork_parent = _clauses_and_lineage(events)

    _per_clause, gaps = _attribute(
        events,
        clauses,
        fork_parent,
        entry_pid=42,
    )

    assert [gap["reason"] for gap in gaps] == [
        "trusted_root_pre_exec_structural_setup"
    ]
    assert all("fork_resolution_failure" not in gap for gap in gaps)
