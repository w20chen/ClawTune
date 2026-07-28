from __future__ import annotations

import pytest

from tool_resource.clause_bridge import ExecImageRecord, _clause_status
from tool_resource.telemetry import (
    BPF_PROGRAM,
    RawRun,
    _bpf_setup_error_message,
    _isolate_call_events,
    _runtime_response_exit_code,
    _syscall_symbol_candidates,
    validate_clause_telemetry_smoke,
)


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


def test_syscall_symbol_candidates_include_bcc_and_common_kernel_names() -> None:
    class FakeBPF:
        @staticmethod
        def get_syscall_fnname(name: str) -> bytes:
            return f"bcc_{name}".encode()

    candidates = _syscall_symbol_candidates(FakeBPF, "execve")

    assert candidates[0] == "bcc_execve"
    assert "__x64_sys_execve" in candidates
    assert "sys_execve" in candidates


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
