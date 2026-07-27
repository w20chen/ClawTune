from __future__ import annotations

from tool_resource.telemetry import (
    BPF_PROGRAM,
    _bpf_setup_error_message,
    _syscall_symbol_candidates,
)


def test_bpf_program_uses_syscall_kprobes_for_exec_entry_and_return() -> None:
    assert "TRACEPOINT_PROBE(syscalls, sys_enter_execve" not in BPF_PROGRAM
    assert "TRACEPOINT_PROBE(syscalls, sys_exit_execve" not in BPF_PROGRAM
    assert "int capture_sys_execve(struct pt_regs *ctx)" in BPF_PROGRAM
    assert "int capture_sys_execve_return(struct pt_regs *ctx)" in BPF_PROGRAM
    assert "PT_REGS_PARM1(ctx)" in BPF_PROGRAM
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
