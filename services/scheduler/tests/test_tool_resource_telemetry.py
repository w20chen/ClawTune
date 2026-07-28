from __future__ import annotations

import pytest

from tool_resource.clause_bridge import ExecImageRecord, _clause_status
from tool_resource.telemetry import (
    BPF_PROGRAM,
    RawRun,
    _bpf_setup_error_message,
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
