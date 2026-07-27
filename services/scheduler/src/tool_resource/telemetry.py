"""Stage-2 clause telemetry: honest per-clause peak_cpu_cores + sampled_peak_rss.

Extends the Stage-1b lifecycle collector (exec/fork/exit/hiwater) with the
perf CPU-clock sampler validated by the accepted spike, and reconstructs, per
clause = (host_pid, exec_seq):

- ``peak_cpu_cores``: cumulative per-TID CPU deltas of the clause's own threads
  and non-exec descendants, aggregated into 500 ms wall windows (matching the
  resource_timeline label semantics), rate = Delta cpu_ns / window_ns, clipped
  ONLY to the observed cgroup quota; the max window rate. Never cpu_ns/wall_ns.
- ``sampled_peak_rss``: the maximum, over aligned time bins, of the SUM of
  current RSS across DISTINCT live ``mm`` address spaces in the clause lineage
  (threads sharing an mm are deduplicated; distinct mm are summed at the same
  aligned timestamp). Never a per-TID sum, never per-mm maxima summed across
  different times, never a reused lifetime hiwater.

Both carry provenance (cadence, window width, sample/coverage counts, boundary
coverage, lost-event counters, quota) and are returned ``unavailable`` with a
reason when their target-specific coverage is insufficient. ``wall_ns`` and
cumulative ``cpu_ns`` are preserved as separate raw observations.

Non-perturbing: samples emit in-kernel only for tasks in the target cgroup.
Runs a workload in a fresh cgroup-v2 scope (the host's frozen Stage-1b docker
image is unavailable, so — like the spike — a local cgroup is used; container
teardown semantics remain a Stage-1b concern). Root required.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from tool_resource.clause_bridge import ShellCommandLookupFailure

# bcc is imported lazily inside the collection functions so the pure analysis
# (attribution, windowing, aggregation) can be imported and unit-tested without
# a bcc/BPF runtime.

_BCC_SEARCH_ROOTS = (Path("/usr/lib"), Path("/usr/lib64"), Path("/usr/local/lib"))


def _ensure_bcc_importable() -> None:
    """Make distro BCC bindings visible to non-system Python interpreters."""

    try:
        import bcc  # noqa: F401
        return
    except ImportError as first_error:
        for root in _BCC_SEARCH_ROOTS:
            if not root.exists():
                continue
            for package_dir in root.glob("**/site-packages/bcc"):
                parent = str(package_dir.parent)
                if parent not in sys.path:
                    sys.path.append(parent)
                try:
                    import bcc  # noqa: F401
                    return
                except ImportError:
                    continue
            for package_dir in root.glob("**/dist-packages/bcc"):
                parent = str(package_dir.parent)
                if parent not in sys.path:
                    sys.path.append(parent)
                try:
                    import bcc  # noqa: F401
                    return
                except ImportError:
                    continue
        raise first_error


def _bpf_runtime_diagnostics() -> dict[str, Any]:
    """Small environment snapshot for BCC/BPF compile failures."""

    def run(args: Sequence[str]) -> str | None:
        try:
            result = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except Exception:
            return None
        output = (result.stdout or result.stderr).strip()
        return output.splitlines()[0] if output else None

    kernel = run(["uname", "-r"])
    headers: list[str] = []
    if kernel:
        candidates = (
            Path("/lib/modules") / kernel / "build",
            Path("/usr/src") / f"linux-headers-{kernel}",
        )
        headers = [str(path) for path in candidates if path.exists()]
    bcc_file = None
    try:
        _ensure_bcc_importable()
        import bcc

        bcc_file = getattr(bcc, "__file__", None)
    except ImportError:
        pass
    return {
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "python": sys.executable,
        "bcc_file": bcc_file,
        "clang": shutil.which("clang"),
        "llc": shutil.which("llc"),
        "bpftool": shutil.which("bpftool"),
        "kernel_release": kernel,
        "kernel_headers": headers,
        "lib_modules_exists": Path("/lib/modules").exists(),
        "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
    }


_BPF_PERMISSION_ERROR_PATTERNS = (
    "operation not permitted",
    "permission denied",
    "failed to create bpf map",
    "could not open bpf map",
    "perf_event_open",
)


def _is_bpf_permission_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(pattern in message for pattern in _BPF_PERMISSION_ERROR_PATTERNS)


def _bpf_setup_error_message(phase: str, exc: BaseException) -> str:
    diagnostics = json.dumps(_bpf_runtime_diagnostics(), sort_keys=True)
    detail = f"{type(exc).__name__}: {exc}"
    if _is_bpf_permission_error(exc):
        return (
            f"{phase}: permission denied while creating BPF maps/probes/events; "
            "Stage-2 clause telemetry requires root or the kernel capabilities "
            "needed for BPF and perf_event access; "
            f"detail={detail}; diagnostics={diagnostics}"
        )
    return f"{phase}: {detail}; diagnostics={diagnostics}"


def _decode_symbol(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _syscall_symbol_candidates(bpf_cls: Any, name: str) -> tuple[str, ...]:
    candidates: list[str] = []
    try:
        candidates.append(_decode_symbol(bpf_cls.get_syscall_fnname(name)))
    except Exception:
        pass
    candidates.extend(
        [
            f"__x64_sys_{name}",
            f"__ia32_sys_{name}",
            f"__arm64_sys_{name}",
            f"sys_{name}",
        ]
    )
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _attach_first_kprobe(
    bpf: Any,
    bpf_cls: Any,
    *,
    syscall: str,
    fn_name: str,
    retprobe: bool = False,
) -> str:
    errors: list[str] = []
    for event in _syscall_symbol_candidates(bpf_cls, syscall):
        try:
            if retprobe:
                bpf.attach_kretprobe(event=event, fn_name=fn_name)
            else:
                bpf.attach_kprobe(event=event, fn_name=fn_name)
            return event
        except Exception as exc:
            errors.append(f"{event}: {type(exc).__name__}: {exc}")
    probe_kind = "kretprobe" if retprobe else "kprobe"
    raise RuntimeError(
        f"cannot attach {probe_kind} for syscall {syscall}: {'; '.join(errors)}"
    )

SAMPLE_PERIOD_NS = 10_000_000  # ~10 ms CPU-time per perf callback
WINDOW_NS = 500_000_000  # 500 ms wall label window (resource_timeline semantics)
ALIGN_BIN_NS = 20_000_000  # 20 ms aligned bins for RSS summation
SENTINEL = 2**64 - 1
MAX_ARGS = 16
ARG_BYTES = 512
MAX_ARG_CHUNKS = 8
MAX_ARG_WORD_BYTES = (ARG_BYTES - 1) * MAX_ARG_CHUNKS
ARG_FLAG_TRUNCATED = 1
ARG_FLAG_ARGV_CAPPED = 2
ARG_FLAG_CONTINUED = 4
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_NPROC = os.cpu_count() or 1
LOSS_COUNTER_NAMES = (
    "ringbuf_reserve_failures",
    "argv_read_failures",
    "argv_boundary_read_failures",
)

TYPE_NAMES = {
    1: "exec_arg",
    2: "exec_boundary",
    3: "exit_boundary",
    4: "fork",
    5: "perf",
    6: "failed_exec_attempt",
    7: "exec_meta",
    8: "bprm_meta",
    9: "interp_meta",
}

BPF_PROGRAM = r"""
#include <linux/binfmts.h>
#include <linux/mm_types.h>
#include <linux/sched.h>
#include <linux/sched/signal.h>
#include <uapi/linux/bpf_perf_event.h>

#define TYPE_EXEC_ARG 1
#define TYPE_EXEC_BOUNDARY 2
#define TYPE_EXIT_BOUNDARY 3
#define TYPE_FORK 4
#define TYPE_PERF 5
#define TYPE_FAILED_EXEC_ATTEMPT 6
#define TYPE_EXEC_META 7
#define TYPE_BPRM_META 8
#define TYPE_INTERP_META 9
#define MAX_ARGS 16
#define ARG_BYTES 512
#define MAX_ARG_CHUNKS 8
#define ARG_FLAG_TRUNCATED 1
#define ARG_FLAG_ARGV_CAPPED 2
#define ARG_FLAG_CONTINUED 4

struct event_t {
    u64 timestamp_ns;
    u64 cgroup_id;
    u64 exec_seq;
    u64 cpu_ns;         /* per-task cumulative utime+stime at sample time */
    u64 rss_pages;      /* CURRENT rss = file+anon+shmem (not hiwater) */
    u64 mm_ptr;         /* address-space identity for dedup */
    u64 hiwater_pages;  /* raw lifetime hiwater (exit only), kept separate */
    u64 io_read_bytes;  /* task->ioac.read_bytes */
    u64 io_write_bytes; /* task->ioac.write_bytes */
    u64 io_cancelled_write_bytes; /* task->ioac.cancelled_write_bytes */
    u32 type;
    u32 host_pid;
    u32 host_tid;
    u32 parent_host_pid;
    u32 child_host_pid;
    u32 child_host_tid;
    u32 arg_index;
    u32 arg_chunk_index;
    u32 arg_flags;
    u32 exit_code;
    char arg[ARG_BYTES];
};

BPF_RINGBUF_OUTPUT(events, 1024);
BPF_ARRAY(target_cgroup, u64, 1);
BPF_ARRAY(ringbuf_reserve_failures, u64, 1);
BPF_ARRAY(argv_read_failures, u64, 1);
BPF_ARRAY(argv_boundary_read_failures, u64, 1);
BPF_ARRAY(perf_sample_count, u64, 1);
BPF_QUEUE(exec_sequences, u64, 65536);
BPF_ARRAY(sequence_ready, u32, 1);
struct task_key_t {
    u32 tid;
    u32 pad;
    u64 task_ptr;
};
struct pending_exec_t {
    u64 seq;
    u64 argv_ptr;
    u64 argv_captured;
};
BPF_HASH(current_seq, struct task_key_t, u64);
BPF_HASH(pending_seq, struct task_key_t, struct pending_exec_t);

static int wanted(void) {
    u32 zero = 0;
    u64 *t = target_cgroup.lookup(&zero);
    return t && *t && *t == bpf_get_current_cgroup_id();
}

static void lost(u64 *counter) {
    if (counter) __sync_fetch_and_add(counter, 1);
}

static void ringbuf_reserve_failed(void) {
    u32 z = 0;
    lost(ringbuf_reserve_failures.lookup(&z));
}

static void argv_read_failed(void) {
    u32 z = 0;
    lost(argv_read_failures.lookup(&z));
}

static void argv_boundary_read_failed(void) {
    u32 z = 0;
    lost(argv_boundary_read_failures.lookup(&z));
}

static u32 parent_tgid(void) {
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct task_struct *parent = 0;
    u32 tgid = 0;
    bpf_probe_read_kernel(&parent, sizeof(parent), &task->real_parent);
    if (parent) bpf_probe_read_kernel(&tgid, sizeof(tgid), &parent->tgid);
    return tgid;
}

static u64 current_rss_pages(struct task_struct *task, u64 *mm_out) {
    struct mm_struct *mm = 0;
    bpf_probe_read_kernel(&mm, sizeof(mm), &task->mm);
    *mm_out = (u64)mm;
    if (!mm) return 0;
    long file = 0, anon = 0, shmem = 0;
    bpf_probe_read_kernel(&file, sizeof(file), &mm->rss_stat.count[0].counter);
    bpf_probe_read_kernel(&anon, sizeof(anon), &mm->rss_stat.count[1].counter);
    bpf_probe_read_kernel(&shmem, sizeof(shmem), &mm->rss_stat.count[3].counter);
    long total = file + anon + shmem;
    return total < 0 ? 0 : (u64)total;
}

static u64 current_cpu_ns(struct task_struct *task) {
    u64 u = 0, s = 0;
    bpf_probe_read_kernel(&u, sizeof(u), &task->utime);
    bpf_probe_read_kernel(&s, sizeof(s), &task->stime);
    return u + s;
}

static void fill_counters(struct event_t *e, struct task_struct *task) {
    u64 mm_ptr = 0;
    e->rss_pages = current_rss_pages(task, &mm_ptr);
    e->mm_ptr = mm_ptr;
    e->cpu_ns = current_cpu_ns(task);
    bpf_probe_read_kernel(
        &e->io_read_bytes, sizeof(e->io_read_bytes), &task->ioac.read_bytes
    );
    bpf_probe_read_kernel(
        &e->io_write_bytes, sizeof(e->io_write_bytes), &task->ioac.write_bytes
    );
    bpf_probe_read_kernel(
        &e->io_cancelled_write_bytes,
        sizeof(e->io_cancelled_write_bytes),
        &task->ioac.cancelled_write_bytes
    );
}

static void capture_argv(
    u64 seq, const char *const *argv, u64 pid_tgid
) {
    u32 tid = pid_tgid;
    u32 captured_args = 0;
    #pragma unroll
    for (int i = 0; i < MAX_ARGS; i++) {
        const char *a = 0;
        int pointer_read = bpf_probe_read_user(&a, sizeof(a), &argv[i]);
        if (pointer_read < 0) {
            argv_read_failed();
            break;
        }
        if (!a) break;
        captured_args++;
        #pragma unroll
        for (int chunk = 0; chunk < MAX_ARG_CHUNKS; chunk++) {
            int offset = chunk * (ARG_BYTES - 1);
            struct event_t *e = events.ringbuf_reserve(sizeof(*e));
            if (!e) {
                ringbuf_reserve_failed();
                break;
            }
            __builtin_memset(e, 0, sizeof(*e));
            e->timestamp_ns = bpf_ktime_get_ns();
            e->cgroup_id = bpf_get_current_cgroup_id();
            e->exec_seq = seq;
            e->type = TYPE_EXEC_ARG;
            e->host_pid = pid_tgid >> 32;
            e->host_tid = tid;
            e->arg_index = i;
            e->arg_chunk_index = chunk;
            int arg_size = bpf_probe_read_user_str(
                e->arg, sizeof(e->arg), a + offset
            );
            int complete = 0;
            if (arg_size < 0) {
                argv_read_failed();
                complete = 1;
            } else if (arg_size == sizeof(e->arg)) {
                char source_last = 0;
                int last_read = bpf_probe_read_user(
                    &source_last,
                    sizeof(source_last),
                    a + offset + sizeof(e->arg) - 1
                );
                if (last_read < 0) {
                    argv_boundary_read_failed();
                    complete = 1;
                } else if (source_last == '\0') {
                    complete = 1;
                } else if (chunk == MAX_ARG_CHUNKS - 1) {
                    e->arg_flags = ARG_FLAG_TRUNCATED;
                    complete = 1;
                } else {
                    e->arg_flags = ARG_FLAG_CONTINUED;
                }
            } else {
                complete = 1;
            }
            events.ringbuf_submit(e, 0);
            if (complete) break;
        }
    }
    const char *extra = 0;
    if (captured_args == MAX_ARGS) {
        int pointer_read = bpf_probe_read_user(
            &extra, sizeof(extra), &argv[MAX_ARGS]
        );
        if (pointer_read < 0) argv_read_failed();
    }
    if (extra) {
        struct event_t *e = events.ringbuf_reserve(sizeof(*e));
        if (!e) {
            ringbuf_reserve_failed();
        } else {
            __builtin_memset(e, 0, sizeof(*e));
            e->timestamp_ns = bpf_ktime_get_ns();
            e->cgroup_id = bpf_get_current_cgroup_id();
            e->exec_seq = seq;
            e->type = TYPE_EXEC_ARG;
            e->host_pid = pid_tgid >> 32;
            e->host_tid = tid;
            e->arg_index = MAX_ARGS;
            e->arg_flags = ARG_FLAG_ARGV_CAPPED;
            events.ringbuf_submit(e, 0);
        }
    }
}

static void emit_kernel_exec_meta(
    u32 type, u64 seq, const char *value, u32 argc, u64 pid_tgid
) {
    if (!value) return;
    struct event_t *e = events.ringbuf_reserve(sizeof(*e));
    if (!e) {
        ringbuf_reserve_failed();
        return;
    }
    __builtin_memset(e, 0, sizeof(*e));
    e->timestamp_ns = bpf_ktime_get_ns();
    e->cgroup_id = bpf_get_current_cgroup_id();
    e->exec_seq = seq;
    e->type = type;
    e->host_pid = pid_tgid >> 32;
    e->host_tid = (u32)pid_tgid;
    e->exit_code = argc;
    int size = bpf_probe_read_kernel_str(e->arg, sizeof(e->arg), value);
    if (size < 0)
        argv_read_failed();
    else if (size == sizeof(e->arg))
        e->arg_flags = ARG_FLAG_TRUNCATED;
    events.ringbuf_submit(e, 0);
}

/* execve/execveat ENTRY: assign a new seq as PENDING without replacing the
 * current image. The saved vector is read after copy_strings() has faulted
 * valid cold pages; failed exec argv remains available on return. */
static int capture_enter(const char *filename, const char *const *argv) {
    u32 zero = 0;
    u32 *ready = sequence_ready.lookup(&zero);
    if (!ready || !*ready) return 0;
    if (!wanted()) return 0;
    u64 seq = 0;
    if (exec_sequences.pop(&seq)) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = pid_tgid;
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)bpf_get_current_task(),
    };
    struct pending_exec_t pending = {
        .seq = seq,
        .argv_ptr = (u64)argv,
    };
    pending_seq.update(&task_key, &pending);
    struct event_t *e = events.ringbuf_reserve(sizeof(*e));
    if (!e) {
        ringbuf_reserve_failed();
    } else {
        __builtin_memset(e, 0, sizeof(*e));
        e->timestamp_ns = bpf_ktime_get_ns();
        e->cgroup_id = bpf_get_current_cgroup_id();
        e->exec_seq = seq;
        e->type = TYPE_EXEC_META;
        e->host_pid = pid_tgid >> 32;
        e->host_tid = tid;
        int filename_size = bpf_probe_read_user_str(
            e->arg, sizeof(e->arg), filename
        );
        if (filename_size < 0) argv_read_failed();
        else if (filename_size == sizeof(e->arg))
            e->arg_flags = ARG_FLAG_TRUNCATED;
        events.ringbuf_submit(e, 0);
    }
    return 0;
}

int capture_sys_execve(struct pt_regs *ctx) {
    return capture_enter(
        (const char *)PT_REGS_PARM1(ctx),
        (const char *const *)PT_REGS_PARM2(ctx)
    );
}

int capture_sys_execveat(struct pt_regs *ctx) {
    return capture_enter(
        (const char *)PT_REGS_PARM2(ctx),
        (const char *const *)PT_REGS_PARM3(ctx)
    );
}

/* copy_strings() has faulted the original argv pages before bprm_execve.
 * Capture here, before a script interpreter can rewrite the final argv. */
int capture_bprm_argv(struct pt_regs *ctx) {
    if (!wanted()) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = pid_tgid;
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)bpf_get_current_task(),
    };
    struct pending_exec_t *pending = pending_seq.lookup(&task_key);
    if (!pending || pending->argv_captured) return 0;
    struct linux_binprm *bprm =
        (struct linux_binprm *)PT_REGS_PARM1(ctx);
    const char *filename = 0;
    const char *interp = 0;
    int argc = 0;
    bpf_probe_read_kernel(&filename, sizeof(filename), &bprm->filename);
    bpf_probe_read_kernel(&interp, sizeof(interp), &bprm->interp);
    bpf_probe_read_kernel(&argc, sizeof(argc), &bprm->argc);
    emit_kernel_exec_meta(
        TYPE_BPRM_META, pending->seq, filename, argc, pid_tgid
    );
    emit_kernel_exec_meta(
        TYPE_INTERP_META, pending->seq, interp, argc, pid_tgid
    );
    capture_argv(
        pending->seq,
        (const char *const *)pending->argv_ptr,
        pid_tgid
    );
    pending->argv_captured = 1;
    return 0;
}

int capture_interp_change(struct pt_regs *ctx) {
    if (!wanted()) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = pid_tgid;
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)bpf_get_current_task(),
    };
    struct pending_exec_t *pending = pending_seq.lookup(&task_key);
    if (!pending) return 0;
    const char *interp = (const char *)PT_REGS_PARM1(ctx);
    emit_kernel_exec_meta(
        TYPE_INTERP_META, pending->seq, interp, 0, pid_tgid
    );
    return 0;
}

/* execve RETURN: promote a successful image or close a failed attempt. */
static int on_exec_return(long ret) {
    if (!wanted()) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = pid_tgid;
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)bpf_get_current_task(),
    };
    struct pending_exec_t *pending = pending_seq.lookup(&task_key);
    if (pending) {
        if (!pending->argv_captured && ret < 0) {
            capture_argv(
                pending->seq,
                (const char *const *)pending->argv_ptr,
                pid_tgid
            );
        } else if (!pending->argv_captured) {
            argv_read_failed();
        }
        if (ret >= 0) {
            current_seq.update(&task_key, &pending->seq);
        }
        struct event_t *e = events.ringbuf_reserve(sizeof(*e));
        if (!e) {
            ringbuf_reserve_failed();
        } else {
            __builtin_memset(e, 0, sizeof(*e));
            e->timestamp_ns = bpf_ktime_get_ns();
            e->cgroup_id = bpf_get_current_cgroup_id();
            e->exec_seq = pending->seq;
            e->type = ret < 0 ? TYPE_FAILED_EXEC_ATTEMPT : TYPE_EXEC_BOUNDARY;
            e->host_pid = pid_tgid >> 32;
            e->host_tid = tid;
            e->parent_host_pid = parent_tgid();
            if (ret < 0) {
                e->exit_code = (u32)(-ret);  /* positive errno */
            } else {
                fill_counters(
                    e, (struct task_struct *)bpf_get_current_task()
                );
            }
            events.ringbuf_submit(e, 0);
        }
    }
    pending_seq.delete(&task_key);
    return 0;
}

int capture_sys_execve_return(struct pt_regs *ctx) {
    return on_exec_return(PT_REGS_RC(ctx));
}

int capture_sys_execveat_return(struct pt_regs *ctx) {
    return on_exec_return(PT_REGS_RC(ctx));
}

/* Fork lineage must be TGID-consistent with every other event (which key on
 * tgid = pid_tgid>>32). The tracepoint's parent_pid/child_pid are TIDs; using
 * parent_pid directly breaks lineage when a non-leader thread forks. Record the
 * forking task's TGID as the parent. child_pid is always the new TID; it is also
 * the TGID for a process fork and supplies the zero I/O baseline for both
 * process and thread children. */
RAW_TRACEPOINT_PROBE(sched_process_fork) {
    if (!wanted()) return 0;
    struct task_struct *child = (struct task_struct *)ctx->args[1];
    u32 child_tid = 0;
    bpf_probe_read_kernel(&child_tid, sizeof(child_tid), &child->pid);
    struct task_key_t child_key = {
        .tid = child_tid,
        .task_ptr = (u64)child,
    };
    current_seq.delete(&child_key);
    pending_seq.delete(&child_key);
    struct event_t *e = events.ringbuf_reserve(sizeof(*e));
    if (!e) { ringbuf_reserve_failed(); return 0; }
    __builtin_memset(e, 0, sizeof(*e));
    e->timestamp_ns = bpf_ktime_get_ns();
    e->cgroup_id = bpf_get_current_cgroup_id();
    e->exec_seq = ~0ULL;
    e->type = TYPE_FORK;
    e->host_pid = bpf_get_current_pid_tgid() >> 32;  /* parent TGID */
    e->child_host_pid = child_tid;
    e->child_host_tid = child_tid;
    events.ringbuf_submit(e, 0);
    return 0;
}

TRACEPOINT_PROBE(sched, sched_process_exit) {
    if (!wanted()) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = pid_tgid;
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct signal_struct *signal = 0;
    u64 gu = 0, gs = 0;
    u32 exit_code = 0;
    bpf_probe_read_kernel(&signal, sizeof(signal), &task->signal);
    bpf_probe_read_kernel(&exit_code, sizeof(exit_code), &task->exit_code);
    struct mm_struct *mm = 0;
    unsigned long hiwater = 0;
    bpf_probe_read_kernel(&mm, sizeof(mm), &task->mm);
    if (mm) bpf_probe_read_kernel(&hiwater, sizeof(hiwater), &mm->hiwater_rss);
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)task,
    };
    u64 *seq = current_seq.lookup(&task_key);
    struct event_t *e = events.ringbuf_reserve(sizeof(*e));
    if (!e) { ringbuf_reserve_failed(); return 0; }
    __builtin_memset(e, 0, sizeof(*e));
    fill_counters(e, task);
    e->timestamp_ns = bpf_ktime_get_ns();
    e->cgroup_id = bpf_get_current_cgroup_id();
    e->exec_seq = seq ? *seq : ~0ULL;
    e->hiwater_pages = hiwater;
    e->type = TYPE_EXIT_BOUNDARY;
    e->host_pid = pid_tgid >> 32;
    e->host_tid = tid;
    e->parent_host_pid = parent_tgid();
    e->exit_code = exit_code;
    events.ringbuf_submit(e, 0);
    return 0;
}

/* CPU-clock can sample a terminal task after sched_process_exit. Keep its exec
 * identity until the task_struct is actually released; the analysis still
 * excludes samples outside the original half-open exec window. This hook is
 * intentionally unfiltered because the freeing task need not share the dead
 * task's cgroup. A new in-scope fork also clears both child slots defensively. */
RAW_TRACEPOINT_PROBE(sched_process_free) {
    struct task_struct *task = (struct task_struct *)ctx->args[0];
    u32 tid = 0;
    bpf_probe_read_kernel(&tid, sizeof(tid), &task->pid);
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)task,
    };
    current_seq.delete(&task_key);
    pending_seq.delete(&task_key);
    return 0;
}

int on_cpu_clock(struct bpf_perf_event_data *ctx) {
    if (!wanted()) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tid = pid_tgid;
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct task_key_t task_key = {
        .tid = tid,
        .task_ptr = (u64)task,
    };
    u64 *seq = current_seq.lookup(&task_key);
    struct event_t *e = events.ringbuf_reserve(sizeof(*e));
    if (!e) { ringbuf_reserve_failed(); return 0; }
    __builtin_memset(e, 0, sizeof(*e));
    fill_counters(e, task);
    e->timestamp_ns = bpf_ktime_get_ns();
    e->cgroup_id = bpf_get_current_cgroup_id();
    e->exec_seq = seq ? *seq : ~0ULL;
    e->type = TYPE_PERF;
    e->host_pid = pid_tgid >> 32;
    e->host_tid = tid;
    events.ringbuf_submit(e, 0);
    u32 z = 0;
    u64 *c = perf_sample_count.lookup(&z);
    if (c) __sync_fetch_and_add(c, 1);
    return 0;
}
"""


# ---------------------------------------------------------------------------
# Raw collection
# ---------------------------------------------------------------------------


def _read_usage_usec(cgroup: Path) -> int:
    for line in (cgroup / "cpu.stat").read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    raise RuntimeError("usage_usec not found")


def observed_quota_cores(cgroup: Path) -> float:
    raw = (cgroup / "cpu.max").read_text().split()
    return float(_NPROC) if raw[0] == "max" else float(int(raw[0]) / int(raw[1]))


class RssOracle(threading.Thread):
    """Independent live-RSS reference: sum of VmRSS over distinct cgroup PIDs.

    A userspace poller (analysis-only oracle, never a prediction input): at each
    tick it sums current VmRSS across distinct tgids in the cgroup and keeps the
    max. This is the ground truth ``sampled_peak_rss`` is compared against.
    """

    def __init__(self, cgroup: Path, interval_s: float = 0.002) -> None:
        super().__init__(daemon=True)
        self._cgroup = cgroup
        self._interval = interval_s
        self._halt = threading.Event()  # not _stop: Thread._stop is internal
        self.peak_sum_kb = 0
        self.samples = 0

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                pids = (self._cgroup / "cgroup.procs").read_text().split()
            except OSError:
                break
            total = 0
            for pid in pids:
                try:
                    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1])
                            break
                except OSError:
                    continue
            if total > self.peak_sum_kb:
                self.peak_sum_kb = total
            self.samples += 1
            time.sleep(self._interval)

    def stop(self) -> None:
        self._halt.set()


@dataclass
class RawRun:
    cgroup_id: int
    quota_cores: float
    status: int
    wall_ns: int
    usage_usec: int
    ringbuf_reserve_failures: int
    perf_sample_count: int
    oracle_peak_rss_kb: int
    oracle_samples: int
    marker: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_map_entries: dict[str, int] = field(default_factory=dict)
    argv_read_failures: int = 0
    argv_boundary_read_failures: int = 0

    @property
    def loss_count(self) -> int:
        return (
            self.ringbuf_reserve_failures
            + self.argv_read_failures
            + self.argv_boundary_read_failures
        )

    @property
    def loss_counts(self) -> dict[str, int]:
        return {
            "ringbuf_reserve_failures": self.ringbuf_reserve_failures,
            "argv_read_failures": self.argv_read_failures,
            "argv_boundary_read_failures": self.argv_boundary_read_failures,
        }


@dataclass(frozen=True)
class ToolCallToken:
    tool_call_id: str
    command: str
    started_ns: int
    ringbuf_reserve_failures: int
    perf_sample_count: int
    argv_read_failures: int = 0
    argv_boundary_read_failures: int = 0
    source_tool_call_id: str = ""
    source_command: str = ""
    source_tool_result: str = ""


_EXIT_CODE_DIAGNOSTIC = re.compile(r"^Exit code: (?P<code>-?\d+)$")
_PROTOCOL_TIMEOUT_MARKERS = frozenset(
    {"[timeout]", "[resource_timeout]", "[resource_stall_timeout]"}
)


def _anchored_command_not_found(text: str) -> tuple[str, str] | None:
    from tool_resource.clause_bridge import parse_shell_lookup_diagnostic

    matches = [
        (head, line)
        for line in text.splitlines()
        if (head := parse_shell_lookup_diagnostic(line)) is not None
    ]
    if not matches or len({head for head, _line in matches}) != 1:
        return None
    return matches[0]


def _strict_exit_code(tool_result: str) -> int | None:
    lines = [line for line in tool_result.splitlines() if line]
    if not lines or (match := _EXIT_CODE_DIAGNOSTIC.fullmatch(lines[-1])) is None:
        return None
    return int(match.group("code"))


def _is_protocol_timeout(replay_exit_code: int | None, replay_result: str) -> bool:
    return replay_exit_code == 124 and any(
        line.strip() in _PROTOCOL_TIMEOUT_MARKERS for line in replay_result.splitlines()
    )


def _replay_tool_result(
    replay_response: Mapping[str, Any] | None,
    replay_exit_code: int | None,
) -> str:
    if replay_response is None:
        return ""
    result = str(replay_response.get("result") or "")
    if not replay_response.get("ok", False) and not result.startswith("Error"):
        result = f"Error: {result}"
    if replay_exit_code is not None:
        return f"{result}\n\nExit code: {replay_exit_code}".strip()
    return result


def shell_command_lookup_failure_evidence(
    *,
    command: str,
    source_tool_call_id: str,
    replay_tool_call_id: str,
    source_command: str,
    source_tool_result: str,
    replay_result: str,
    replay_stderr: str,
    replay_exit_code: int | None,
) -> ShellCommandLookupFailure | None:
    """Return strict source/replay command-lookup evidence, else no evidence."""

    from tool_resource.clause_bridge import (
        ShellCommandLookupFailure,
        shell_lookup_exit_semantics,
    )

    if (
        not source_tool_call_id
        or not replay_tool_call_id
        or source_command != command
        or replay_exit_code not in {0, 127}
    ):
        return None
    source_exit_code = _strict_exit_code(source_tool_result)
    if source_exit_code != replay_exit_code:
        return None
    source_match = _anchored_command_not_found(source_tool_result)
    replay_channel = "raw_stderr" if replay_stderr else "tool_result"
    replay_match = _anchored_command_not_found(
        replay_stderr if replay_stderr else replay_result
    )
    if (
        source_match is None
        or replay_match is None
        or source_match[0] != replay_match[0]
    ):
        return None
    exit_code_semantics = shell_lookup_exit_semantics(
        command,
        source_match[0],
        source_exit_code,
    )
    if exit_code_semantics is None:
        return None
    return ShellCommandLookupFailure(
        executable_head=source_match[0],
        command=command,
        source_tool_call_id=source_tool_call_id,
        replay_tool_call_id=replay_tool_call_id,
        source_exit_code=source_exit_code,
        replay_exit_code=replay_exit_code,
        source_diagnostic=source_match[1],
        replay_diagnostic=replay_match[1],
        source_channel="source_tool_result",
        replay_channel=replay_channel,
        parser="anchored_shell_command_not_found_v1",
        exit_code_semantics=exit_code_semantics,
    )


def _source_exec_fields(
    action: Mapping[str, Any] | None,
) -> tuple[str, str, str]:
    data = action.get("data") if action is not None else None
    if not isinstance(data, Mapping):
        return "", "", ""
    raw_args = data.get("tool_args")
    if isinstance(raw_args, Mapping):
        args = raw_args
    else:
        try:
            parsed = json.loads(str(raw_args or "{}"))
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        args = parsed if isinstance(parsed, Mapping) else {}
    return (
        str(data.get("tool_call_id") or ""),
        str(args.get("command") or ""),
        str(data.get("tool_result", data.get("result", "")) or ""),
    )


def _new_cgroup(tag: str) -> Path:
    cg = Path(f"/sys/fs/cgroup/clause_stage2_{os.getpid()}_{tag}")
    cg.mkdir(exist_ok=False)
    return cg


def _rmdir_with_retry(cg: Path, attempts: int = 25, delay_s: float = 0.02) -> None:
    """Remove an emptied cgroup, retrying transient EBUSY; never hide failure.

    A just-emptied cgroup can briefly return EBUSY while the kernel reaps the
    last exiting task. We retry for ~0.5 s; a persistent failure is reported
    loudly to stderr (with the lingering PIDs) rather than silently swallowed.
    """

    for _ in range(attempts):
        try:
            cg.rmdir()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay_s)
    try:
        procs = (cg / "cgroup.procs").read_text().split()
    except OSError:
        procs = ["<unreadable>"]
    print(
        f"WARNING: cgroup {cg} not removed after {attempts} attempts; "
        f"lingering pids={procs}",
        file=sys.stderr,
    )


def collect_case(command: str, tag: str, *, marker: str = "") -> RawRun:
    """Attach the sampler, run ``sh -c command`` in a fresh cgroup, analyze raw."""

    _ensure_bcc_importable()
    from bcc import BPF, PerfSWConfig, PerfType

    cg = _new_cgroup(tag)
    cgroup_id = cg.stat().st_ino
    bpf = BPF(text=BPF_PROGRAM)
    _attach_first_kprobe(
        bpf,
        BPF,
        syscall="execve",
        fn_name="capture_sys_execve",
    )
    _attach_first_kprobe(
        bpf,
        BPF,
        syscall="execveat",
        fn_name="capture_sys_execveat",
    )
    _attach_first_kprobe(
        bpf,
        BPF,
        syscall="execve",
        fn_name="capture_sys_execve_return",
        retprobe=True,
    )
    _attach_first_kprobe(
        bpf,
        BPF,
        syscall="execveat",
        fn_name="capture_sys_execveat_return",
        retprobe=True,
    )
    bpf.attach_kprobe(event="bprm_execve", fn_name="capture_bprm_argv")
    bpf.attach_kprobe(
        event="bprm_change_interp", fn_name="capture_interp_change"
    )
    q = bpf["exec_sequences"]
    for seq in range(8192):
        q.push(ctypes.c_ulonglong(seq))
    bpf["sequence_ready"][ctypes.c_int(0)] = ctypes.c_uint(1)
    bpf["target_cgroup"][ctypes.c_int(0)] = ctypes.c_ulonglong(cgroup_id)

    events: list[dict[str, Any]] = []
    lock = threading.Lock()
    table = bpf["events"]

    def receive(_ctx: int, data: int, _size: int) -> int:
        row = _event_row(table, data)
        with lock:
            events.append(row)
        return 0

    table.open_ring_buffer(receive)
    stop_poll = threading.Event()

    def poll() -> None:
        while not stop_poll.is_set():
            bpf.ring_buffer_poll(timeout=10)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()

    bpf.attach_perf_event(
        ev_type=PerfType.SOFTWARE,
        ev_config=PerfSWConfig.CPU_CLOCK,
        fn_name="on_cpu_clock",
        sample_period=SAMPLE_PERIOD_NS,
    )

    oracle = RssOracle(cg)
    usage_before = _read_usage_usec(cg)

    def _join_cgroup() -> None:
        (cg / "cgroup.procs").write_text(str(os.getpid()))

    oracle.start()
    t0 = time.monotonic_ns()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        preexec_fn=_join_cgroup,
    )
    out, _ = proc.communicate()
    status = proc.returncode
    wall_ns = time.monotonic_ns() - t0
    time.sleep(0.25)  # drain after exit
    oracle.stop()
    oracle.join(timeout=1)
    stop_poll.set()
    poller.join(timeout=1)
    try:
        bpf.ring_buffer_consume()
    except Exception:
        pass
    usage_delta = _read_usage_usec(cg) - usage_before
    loss_counts = _loss_counts(bpf)
    perf_count = bpf["perf_sample_count"][ctypes.c_int(0)].value
    lifecycle_map_entries = {
        name: sum(1 for _ in bpf[name].items())
        for name in ("current_seq", "pending_seq")
    }
    quota = observed_quota_cores(cg)
    bpf.detach_perf_event(ev_type=PerfType.SOFTWARE, ev_config=PerfSWConfig.CPU_CLOCK)
    bpf.cleanup()
    _rmdir_with_retry(cg)
    with lock:
        ordered = sorted(events, key=lambda r: r["ts_ns"])
    return RawRun(
        cgroup_id=cgroup_id,
        quota_cores=quota,
        status=status,
        wall_ns=wall_ns,
        usage_usec=usage_delta,
        ringbuf_reserve_failures=loss_counts["ringbuf_reserve_failures"],
        perf_sample_count=perf_count,
        oracle_peak_rss_kb=oracle.peak_sum_kb,
        oracle_samples=oracle.samples,
        marker=marker.encode() in out if marker else True,
        events=[e for e in ordered if e["cgroup_id"] == cgroup_id],
        lifecycle_map_entries=lifecycle_map_entries,
        argv_read_failures=loss_counts["argv_read_failures"],
        argv_boundary_read_failures=loss_counts["argv_boundary_read_failures"],
    )


# ---------------------------------------------------------------------------
# Clause reconstruction, attribution, and the two honest metrics
# ---------------------------------------------------------------------------


@dataclass
class Clause:
    host_pid: int
    exec_seq: int
    t_exec_ns: int
    t_end_ns: int
    bin: str
    argv: tuple[str, ...]
    requested_executable_path: str | None
    requested_executable_path_truncated: bool
    bprm_filename: str | None
    bprm_interp: str | None
    bprm_evidence_truncated: bool
    exact_argc: int | None
    lineage_parent_pid: int | None
    terminal: bool
    has_causal_end: bool  # real exit (terminal) or next same-pid exec (non-terminal)
    argv_capture_flags: int = 0


def _captured_argv(
    events: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[tuple[int, int], dict[int, str]],
    dict[tuple[int, int], int],
]:
    chunks: dict[tuple[int, int], dict[int, dict[int, tuple[bytes, int]]]] = {}
    capture_flags: dict[tuple[int, int], int] = {}
    for event in events:
        if event["type"] != "exec_arg":
            continue
        key = (int(event["host_pid"]), int(event["exec_seq"]))
        index = int(event["arg_index"])
        event_flags = int(event.get("arg_flags", 0))
        if index == MAX_ARGS and event_flags & ARG_FLAG_ARGV_CAPPED:
            capture_flags[key] = capture_flags.get(key, 0) | (1 << MAX_ARGS)
            continue
        if index >= MAX_ARGS:
            continue
        chunk_index = int(event.get("arg_chunk_index", 0))
        word_chunks = chunks.setdefault(key, {}).setdefault(index, {})
        if chunk_index in word_chunks:
            capture_flags[key] = capture_flags.get(key, 0) | (1 << index)
        raw = event.get("arg_raw")
        payload = (
            bytes.fromhex(raw)
            if isinstance(raw, str)
            else str(event.get("arg", "")).encode()
        )
        word_chunks[chunk_index] = (payload, event_flags)

    words: dict[tuple[int, int], dict[int, str]] = {}
    for key, by_index in chunks.items():
        for index, word_chunks in by_index.items():
            ordered = sorted(word_chunks)
            flags = [word_chunks[chunk][1] for chunk in ordered]
            complete = (
                len(ordered) <= MAX_ARG_CHUNKS
                and ordered == list(range(len(ordered)))
                and all(flag == ARG_FLAG_CONTINUED for flag in flags[:-1])
                and flags[-1] == 0
            )
            if not complete:
                capture_flags[key] = capture_flags.get(key, 0) | (1 << index)
            words.setdefault(key, {})[index] = b"".join(
                word_chunks[chunk][0] for chunk in ordered
            ).decode("utf-8", "replace")
    return words, capture_flags


def _clauses_and_lineage(
    events: list[dict[str, Any]],
) -> tuple[list[Clause], dict[int, int]]:
    """Build per-clause windows and the child_tgid -> parent_tgid fork map."""

    fork_parent: dict[int, int] = {}
    for e in events:
        if e["type"] == "fork" and e["child_host_pid"]:
            fork_parent.setdefault(e["child_host_pid"], e["host_pid"])

    argv_words, argv_capture_flags = _captured_argv(events)
    exact_argc: dict[tuple[int, int], int | None] = {}
    requested_paths: dict[tuple[int, int], str] = {}
    requested_path_truncated: set[tuple[int, int]] = set()
    bprm_filenames: dict[tuple[int, int], str] = {}
    bprm_interpreters: dict[tuple[int, int], str] = {}
    bprm_truncated: set[tuple[int, int]] = set()
    for e in events:
        if e["type"] == "exec_meta":
            key = (e["host_pid"], e["exec_seq"])
            requested_paths[key] = e.get("arg", "")
            if int(e.get("arg_flags", 0)) & ARG_FLAG_TRUNCATED:
                requested_path_truncated.add(key)
        if e["type"] == "bprm_meta":
            key = (e["host_pid"], e["exec_seq"])
            bprm_filenames[key] = e.get("arg", "")
            argc = int(e.get("exit_code") or 0)
            if argc > 0:
                exact_argc[key] = argc
            if int(e.get("arg_flags", 0)) & ARG_FLAG_TRUNCATED:
                bprm_truncated.add(key)
        if e["type"] == "interp_meta":
            key = (e["host_pid"], e["exec_seq"])
            bprm_interpreters[key] = e.get("arg", "")
            if int(e.get("arg_flags", 0)) & ARG_FLAG_TRUNCATED:
                bprm_truncated.add(key)
    def argv_of(pid: int, seq: int) -> tuple[tuple[str, ...], int]:
        words = argv_words.get((pid, seq), {})
        return (
            tuple(words[i] for i in sorted(words)),
            argv_capture_flags.get((pid, seq), 0),
        )

    exits: dict[int, int] = {}
    for e in events:
        if e["type"] == "exit_boundary":
            exits[e["host_pid"]] = max(exits.get(e["host_pid"], 0), e["ts_ns"])

    # exec boundaries per pid, ordered -> clause windows
    execs_by_pid: dict[int, list[dict[str, Any]]] = {}
    for e in events:
        if e["type"] == "exec_boundary" and e["exec_seq"] != SENTINEL:
            execs_by_pid.setdefault(e["host_pid"], []).append(e)

    last_ts = max((r["ts_ns"] for r in events), default=0)
    clauses: list[Clause] = []
    for pid, execs in execs_by_pid.items():
        execs.sort(key=lambda r: r["ts_ns"])
        for i, e in enumerate(execs):
            terminal = i == len(execs) - 1
            if not terminal:
                t_end = execs[i + 1]["ts_ns"]  # next exec on same pid (causal)
                has_causal_end = True
            elif pid in exits:
                t_end = exits[pid]
                has_causal_end = True
            else:
                t_end = last_ts  # synthetic bound; NOT a real causal end
                has_causal_end = False
            argv, capture_flags = argv_of(pid, e["exec_seq"])
            clauses.append(
                Clause(
                    host_pid=pid,
                    exec_seq=e["exec_seq"],
                    t_exec_ns=e["ts_ns"],
                    t_end_ns=t_end,
                    bin=Path(argv[0]).name if argv else "",
                    argv=argv,
                    requested_executable_path=requested_paths.get(
                        (pid, e["exec_seq"])
                    ),
                    requested_executable_path_truncated=(
                        (pid, e["exec_seq"]) in requested_path_truncated
                    ),
                    bprm_filename=bprm_filenames.get((pid, e["exec_seq"])),
                    bprm_interp=bprm_interpreters.get((pid, e["exec_seq"])),
                    bprm_evidence_truncated=(
                        (pid, e["exec_seq"]) in bprm_truncated
                    ),
                    exact_argc=exact_argc.get((pid, e["exec_seq"]), len(argv)),
                    lineage_parent_pid=fork_parent.get(pid),
                    terminal=terminal,
                    has_causal_end=has_causal_end,
                    argv_capture_flags=capture_flags,
                )
            )
    return clauses, fork_parent


def _clause_at(clauses_on_pid: "list[Clause] | tuple", ts: int) -> "Clause | None":
    """Half-open window match: [t_exec, t_end), for terminal and non-terminal
    clauses alike. The exit-boundary sample (ts == t_end) carries its clause's
    exec_seq and is attributed by the direct-seq path, so no inclusive terminal
    end is needed here — and an inclusive end would wrongly pull a sentinel
    sample at exactly t_end onto a just-ended clause."""

    for c in clauses_on_pid:
        if c.t_exec_ns <= ts < c.t_end_ns:
            return c
    return None


def _ancestor_clause_pid(
    pid: int,
    ts: int,
    clause_by_pid: dict[int, list[Clause]],
    fork_parent: dict[int, int],
) -> Clause | None:
    """Nearest ancestor pid whose half-open clause window contains ts."""

    seen: set[int] = set()
    cur = pid
    while cur and cur not in seen:
        match = _clause_at(clause_by_pid.get(cur, ()), ts)
        if match is not None:
            return match
        seen.add(cur)
        cur = fork_parent.get(cur, 0)
    return None


@dataclass
class ClauseMetrics:
    host_pid: int
    exec_seq: int
    bin: str
    argv: tuple[str, ...]  # exec-image argv, evidence for the clause bridge
    requested_executable_path: str | None
    requested_executable_path_truncated: bool
    bprm_filename: str | None
    bprm_interp: str | None
    bprm_evidence_truncated: bool
    exact_argc: int | None
    lineage_parent_pid: int | None  # fork parent, for bridge lineage attribution
    terminal: bool
    has_causal_end: bool  # real exit or next same-pid exec; fail closed if False
    t_exec_ns: int  # clause-window bounds, for the bridge time-aligned merge
    t_end_ns: int
    wall_ns: int
    cpu_ns_cumulative: int  # raw, preserved separately
    exit_signal: int | None  # low 7 bits of exit_code on the terminal exit
    normal_exit_status: int | None  # wait status high byte; unavailable on signal
    peak_cpu_cores: float | None
    peak_cpu_cores_reason: str
    sampled_peak_rss_mb: float | None
    sampled_peak_rss_reason: str
    disk_read_bytes_total: int | None
    disk_write_bytes_total: int | None
    disk_cancelled_write_bytes_total: int | None
    disk_io_reason: str
    # time-aligned profiles the clause bridge merges across owned images
    cpu_windows: tuple[tuple[int, int], ...]
    rss_bins: tuple[tuple[int, int, float], ...]
    provenance: dict[str, Any]
    argv_capture_flags: int = 0


def _attribute(
    events: list[dict[str, Any]],
    clauses: list[Clause],
    fork_parent: dict[int, int],
    *,
    entry_pid: int | None = None,
) -> tuple[dict[tuple[int, int], list[dict[str, Any]]], list[dict[str, Any]]]:
    """Attribute perf + boundary samples to a clause; return (per_clause, gaps)."""

    clause_by_pid: dict[int, list[Clause]] = {}
    by_pid_seq: dict[tuple[int, int], Clause] = {}
    for c in clauses:
        clause_by_pid.setdefault(c.host_pid, []).append(c)
        by_pid_seq[(c.host_pid, c.exec_seq)] = c
    per_clause: dict[tuple[int, int], list[dict[str, Any]]] = {
        (c.host_pid, c.exec_seq): [] for c in clauses
    }
    fork_records: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        if event["type"] == "fork" and event.get("child_host_pid"):
            fork_records.setdefault(event["child_host_pid"], []).append(event)
    exec_boundaries_by_tid: dict[int, list[dict[str, Any]]] = {}
    exec_arg_start_by_tid_seq: dict[tuple[int, int], int] = {}
    for event in events:
        if event["type"] == "exec_boundary":
            exec_boundaries_by_tid.setdefault(event["host_tid"], []).append(event)
        elif event["type"] == "exec_arg" and event["arg_index"] == 0:
            key = (event["host_tid"], event["exec_seq"])
            exec_arg_start_by_tid_seq[key] = min(
                exec_arg_start_by_tid_seq.get(key, event["ts_ns"]),
                event["ts_ns"],
            )

    def pre_exec_owner(
        event: dict[str, Any],
    ) -> tuple[Clause | None, dict[str, Any], str | None]:
        ts, pid, tid = event["ts_ns"], event["host_pid"], event["host_tid"]
        if any(
            boundary["ts_ns"] <= ts for boundary in exec_boundaries_by_tid.get(tid, ())
        ):
            return None, {}, "sentinel_after_successful_exec"

        lineage_id = tid if tid != pid else pid
        # The collector can arm after the initial command process was forked.
        # A CPU-clock sample may then land after sys_enter_execve captured one
        # pending argv but before sys_exit_execve promotes that same seq.
        # It belongs to neither the not-yet-successful image nor an observable
        # fork ancestor. Preserve it as structural setup only with a unique,
        # same-TID successful boundary that strictly closes the pending window.
        # Failed execs and samples without that future boundary remain fatal.
        if not fork_records.get(lineage_id):
            pending_successes = [
                boundary
                for boundary in exec_boundaries_by_tid.get(tid, ())
                if boundary["ts_ns"] > ts
                and (
                    arg_start := exec_arg_start_by_tid_seq.get(
                        (tid, boundary["exec_seq"])
                    )
                )
                is not None
                and arg_start <= ts
            ]
            if len(pending_successes) == 1:
                boundary = pending_successes[0]
                arg_start = exec_arg_start_by_tid_seq[(tid, boundary["exec_seq"])]
                return (
                    None,
                    {
                        "pending_exec_evidence": {
                            "host_pid": pid,
                            "host_tid": tid,
                            "pending_exec_seq": boundary["exec_seq"],
                            "exec_arg_start_ns": arg_start,
                            "sample_ts_ns": ts,
                            "successful_exec_boundary_ns": boundary["ts_ns"],
                        }
                    },
                    "initial_exec_pending_pre_boundary_structural_setup",
                )

        ancestry = [lineage_id]
        current = lineage_id
        seen = {lineage_id}
        first_fork_ts: int | None = None
        ancestor_ts_bound = ts
        fork_chain_records: list[dict[str, int]] = []
        while current != entry_pid:
            all_records = fork_records.get(current, ())
            eligible = [
                record for record in all_records if record["ts_ns"] <= ancestor_ts_bound
            ]
            if len(eligible) != 1:
                reason = (
                    "sentinel_pre_exec_ambiguous_fork_ancestry"
                    if len(eligible) > 1
                    else "sentinel_pre_exec_missing_fork_ancestry"
                )
                record_rows = [
                    {
                        "parent_pid": int(record["host_pid"]),
                        "ts_ns": int(record["ts_ns"]),
                    }
                    for record in all_records
                ]
                return (
                    None,
                    {
                        "fork_ancestry": ancestry,
                        "fork_chain_records": fork_chain_records,
                        "fork_resolution_failure": {
                            "failure_kind": (
                                "ambiguous_generation"
                                if len(eligible) > 1
                                else "missing_generation"
                            ),
                            "child_id": current,
                            "timestamp_bound_ns": ancestor_ts_bound,
                            "eligible_records": sorted(
                                (
                                    row
                                    for row in record_rows
                                    if row["ts_ns"] <= ancestor_ts_bound
                                ),
                                key=lambda row: (
                                    row["ts_ns"],
                                    row["parent_pid"],
                                ),
                            ),
                            "rejected_records": sorted(
                                (
                                    row
                                    for row in record_rows
                                    if row["ts_ns"] > ancestor_ts_bound
                                ),
                                key=lambda row: (
                                    row["ts_ns"],
                                    row["parent_pid"],
                                ),
                            ),
                        },
                    },
                    reason,
                )
            fork_record = eligible[0]
            parent = fork_record["host_pid"]
            if first_fork_ts is None:
                first_fork_ts = fork_record["ts_ns"]
            if parent <= 0 or parent in seen:
                record_rows = [
                    {
                        "parent_pid": int(record["host_pid"]),
                        "ts_ns": int(record["ts_ns"]),
                    }
                    for record in all_records
                ]
                return (
                    None,
                    {
                        "fork_ancestry": ancestry,
                        "fork_chain_records": fork_chain_records,
                        "fork_resolution_failure": {
                            "failure_kind": (
                                "nonpositive_parent" if parent <= 0 else "cyclic_parent"
                            ),
                            "child_id": current,
                            "timestamp_bound_ns": ancestor_ts_bound,
                            "eligible_records": sorted(
                                (
                                    row
                                    for row in record_rows
                                    if row["ts_ns"] <= ancestor_ts_bound
                                ),
                                key=lambda row: (
                                    row["ts_ns"],
                                    row["parent_pid"],
                                ),
                            ),
                            "rejected_records": sorted(
                                (
                                    row
                                    for row in record_rows
                                    if row["ts_ns"] > ancestor_ts_bound
                                ),
                                key=lambda row: (
                                    row["ts_ns"],
                                    row["parent_pid"],
                                ),
                            ),
                        },
                    },
                    "sentinel_pre_exec_ambiguous_fork_ancestry",
                )
            ancestry.append(parent)
            fork_chain_records.append(
                {
                    "child_id": current,
                    "parent_pid": parent,
                    "ts_ns": fork_record["ts_ns"],
                }
            )
            seen.add(parent)
            current = parent
            ancestor_ts_bound = fork_record["ts_ns"]

        match = next(
            (
                active
                for ancestor in ancestry[1:]
                if (
                    active := _clause_at(
                        clause_by_pid.get(ancestor, ()),
                        ts,
                    )
                )
                is not None
            ),
            None,
        )
        if match is not None:
            endpoint = min(
                (
                    candidate
                    for candidate in events
                    if candidate["host_tid"] == tid
                    and candidate["type"] in {"exec_boundary", "exit_boundary"}
                    and candidate["ts_ns"] > ts
                ),
                key=lambda candidate: candidate["ts_ns"],
                default=None,
            )
            provenance = {
                "kind": "inherited_active_exec_owner",
                "original_type": event["type"],
                "original_ts_ns": ts,
                "original_host_pid": pid,
                "original_host_tid": tid,
                "original_exec_seq": event["exec_seq"],
                "owner_host_pid": match.host_pid,
                "owner_exec_seq": match.exec_seq,
                "fork_ancestry": ancestry,
                "fork_chain_records": fork_chain_records,
                "fork_ts_ns": first_fork_ts,
                "cpu_counter_support": {
                    "baseline": {
                        "ts_ns": first_fork_ts,
                        "cpu_ns": 0,
                        "source": "new_fork_zero",
                    },
                    "endpoint": (
                        {
                            "type": endpoint["type"],
                            "ts_ns": endpoint["ts_ns"],
                            "host_pid": endpoint["host_pid"],
                            "host_tid": endpoint["host_tid"],
                            "exec_seq": endpoint["exec_seq"],
                            "cpu_ns": endpoint["cpu_ns"],
                        }
                        if endpoint is not None
                        else None
                    ),
                },
            }
            return match, provenance, None

        return (
            None,
            {
                "fork_ancestry": ancestry,
                "fork_chain_records": fork_chain_records,
                "fork_ts_ns": first_fork_ts,
            },
            "entry_fork_pre_exec_structural_setup",
        )

    gaps: list[dict[str, Any]] = []
    for e in events:
        if e["type"] not in {"perf", "exec_boundary", "exit_boundary"}:
            continue
        ts, pid, tid, seq = (
            e["ts_ns"],
            e["host_pid"],
            e["host_tid"],
            e["exec_seq"],
        )
        target: Clause | None = None
        attributed_event = e
        # 1) DIRECT-SEQ: the sample carries the exec_seq of a clause on its pid
        #    (exec/exit boundaries, and perf on the exec'ing thread) — exact,
        #    taken before any window/lineage fallback.
        if seq != SENTINEL:
            target = by_pid_seq.get((pid, seq))
            if (
                target is not None
                and e["type"] == "perf"
                and not (target.t_exec_ns <= ts < target.t_end_ns)
            ):
                attributed_event = {
                    **e,
                    "metric_excluded": {
                        "reason": "outside_half_open_exec_window",
                        "t_exec_ns": target.t_exec_ns,
                        "t_end_ns": target.t_end_ns,
                        "offset_from_end_ns": ts - target.t_end_ns,
                    },
                }
        elif pid != entry_pid or tid in fork_records:
            target, provenance, reason = pre_exec_owner(e)
            if target is not None:
                attributed_event = {**e, "attribution": provenance}
            elif reason is not None:
                gaps.append({**e, **provenance, "reason": reason})
                continue
        # Non-sentinel unmatched events keep the existing causal ancestor
        # fallback. Sentinel events use only the stricter pre-exec contract.
        if target is None and seq != SENTINEL:
            target = _clause_at(clause_by_pid.get(pid, ()), ts)
            if target is None:
                target = _ancestor_clause_pid(pid, ts, clause_by_pid, fork_parent)
        if target is None:
            gaps.append(
                {
                    **e,
                    "reason": (
                        "sentinel_exec_seq_without_active_exec_image_or_owned_ancestor"
                        if seq == SENTINEL
                        else "exec_seq_without_matching_exec_image_or_owned_ancestor"
                    ),
                }
            )
        else:
            per_clause[(target.host_pid, target.exec_seq)].append(attributed_event)
    return per_clause, gaps


_MIN_ELIGIBLE_SPAN_NS = 1_000_000_000  # resource_timeline: clause >= 1 s
_MIN_WINDOW_SPAN_NS = 100_000_000  # ignore <100 ms trailing windows (rate noise)


def _apportion(t0: int, t1: int, cpu_ns: int) -> "list[tuple[int, float]]":
    """Split a cpu_ns delta over [t0, t1) across EVERY intersected 500 ms window,
    proportional to each window's overlap — a delta spanning a window boundary
    must not be dumped whole into one window."""

    dt = t1 - t0
    if dt <= 0:
        return []
    out: list[tuple[int, float]] = []
    w = t0 // WINDOW_NS
    while w * WINDOW_NS < t1:
        lo = max(w * WINDOW_NS, t0)
        hi = min((w + 1) * WINDOW_NS, t1)
        overlap = hi - lo
        if overlap > 0:
            out.append((w, cpu_ns * overlap / dt))
        w += 1
    return out


def _cpu_counter_points(
    samples: list[dict[str, Any]],
) -> dict[int, list[tuple[int, int]]]:
    per_tid: dict[int, dict[int, int]] = {}
    for sample in samples:
        if sample["cpu_ns"] > 0:
            per_tid.setdefault(sample["host_tid"], {})[sample["ts_ns"]] = sample[
                "cpu_ns"
            ]
        support = sample.get("attribution", {}).get("cpu_counter_support")
        if not isinstance(support, dict):
            continue
        for point in (support.get("baseline"), support.get("endpoint")):
            if not isinstance(point, dict):
                continue
            ts_ns, cpu_ns = point.get("ts_ns"), point.get("cpu_ns")
            if isinstance(ts_ns, int) and isinstance(cpu_ns, int):
                per_tid.setdefault(sample["host_tid"], {})[ts_ns] = cpu_ns
    return {tid: sorted(points.items()) for tid, points in per_tid.items()}


def cpu_window_profile(samples: list[dict[str, Any]]) -> tuple[tuple[int, int], ...]:
    """Absolute-indexed (window_idx, cpu_ns) contributions for the clause bridge.

    Windows are keyed by ``ts // WINDOW_NS`` (a common absolute grid) so the
    bridge can SUM concurrent owned images per window; each per-TID cpu delta is
    apportioned across every window it intersects.
    """

    windows: dict[int, float] = {}
    for points in _cpu_counter_points(samples).values():
        for (t0, c0), (t1, c1) in zip(points, points[1:]):
            if t1 <= t0 or c1 < c0:
                continue
            for widx, part in _apportion(t0, t1, c1 - c0):
                windows[widx] = windows.get(widx, 0.0) + part
    return tuple((w, int(round(v))) for w, v in sorted(windows.items()))


def _peak_cpu_cores(
    samples: list[dict[str, Any]], clause: Clause, quota: float
) -> tuple[float | None, str, dict[str, Any]]:
    cpu_samples = [s for s in samples if s["cpu_ns"] > 0]
    cpu_counter_points = sum(
        len(points) for points in _cpu_counter_points(samples).values()
    )
    profile = cpu_window_profile(samples)  # apportioned absolute windows
    span = clause.t_end_ns - clause.t_exec_ns
    prov = {
        "cpu_sample_count": len(cpu_samples),
        "cpu_counter_point_count": cpu_counter_points,
        "cpu_windows": len(profile),
        "span_s": round(span / 1e9, 3),
    }
    # resource_timeline eligibility: clause >= 1 s AND >= 2 CPU samples.
    if span < _MIN_ELIGIBLE_SPAN_NS:
        return None, "clause_shorter_than_1s_ineligible_for_peak", prov
    if cpu_counter_points < 2 or not profile:
        return None, "insufficient_cpu_samples", prov
    peak: float | None = None
    for widx, cpu_ns in profile:
        win_start = widx * WINDOW_NS
        win_span = min(clause.t_end_ns, win_start + WINDOW_NS) - max(
            clause.t_exec_ns, win_start
        )
        if win_span < _MIN_WINDOW_SPAN_NS:
            continue
        rate = min(cpu_ns / win_span, quota)
        peak = rate if peak is None else max(peak, rate)
    if peak is None:
        return None, "no_eligible_merged_window", prov
    return peak, "ok", prov


def rss_bin_profile(
    samples: list[dict[str, Any]],
) -> tuple[tuple[int, int, float], ...]:
    """Absolute-indexed (bin_idx, mm_ptr, rss_mb) samples for the clause bridge."""

    return tuple(
        (s["ts_ns"] // ALIGN_BIN_NS, s["mm_ptr"], s["rss_pages"] * PAGE / 1e6)
        for s in samples
        if s["rss_pages"] > 0
    )


def _sampled_peak_rss(
    samples: list[dict[str, Any]], clause: Clause
) -> tuple[float | None, str, dict[str, Any]]:
    rss_samples = [s for s in samples if s["rss_pages"] > 0]
    # aligned bins -> per bin, one RSS per distinct mm (latest), sum distinct mm
    bins: dict[int, dict[int, int]] = {}
    mm_tids: dict[int, set[int]] = {}
    for s in rss_samples:
        b = s["ts_ns"] // ALIGN_BIN_NS
        bins.setdefault(b, {})[s["mm_ptr"]] = s["rss_pages"]
        mm_tids.setdefault(s["mm_ptr"], set()).add(s["host_tid"])
    perf_rss = [s for s in rss_samples if s["type"] == "perf"]
    boundary_rss = [s for s in rss_samples if s["type"] != "perf"]
    span = max(clause.t_end_ns - clause.t_exec_ns, 1)
    # coverage: largest gap between consecutive rss samples vs the window
    ts_sorted = sorted(s["ts_ns"] for s in rss_samples)
    max_gap = 0
    edges = [clause.t_exec_ns, *ts_sorted, clause.t_end_ns]
    for a, b in zip(edges, edges[1:]):
        max_gap = max(max_gap, b - a)
    prov = {
        "rss_sample_count": len(rss_samples),
        "perf_rss_samples": len(perf_rss),
        "boundary_rss_samples": len(boundary_rss),
        "distinct_mm": len(mm_tids),
        "shared_mm_tid_counts": {
            hex(mm): len(t) for mm, t in mm_tids.items() if len(t) > 1
        },
        "max_intersample_gap_frac": round(max_gap / span, 3),
    }
    if len(rss_samples) < 2:
        return None, "insufficient_rss_samples", prov
    peak_pages = max(sum(per_mm.values()) for per_mm in bins.values())
    return peak_pages * PAGE / 1e6, "ok", prov


_IO_COUNTER_FIELDS = (
    "io_read_bytes",
    "io_write_bytes",
    "io_cancelled_write_bytes",
)


def _fork_io_baselines(
    events: list[dict[str, Any]],
    clauses: list[Clause],
    fork_parent: dict[int, int],
) -> dict[tuple[int, int], dict[int, int]]:
    """Map each newly forked TID to the exec image active at its fork."""

    clause_by_pid: dict[int, list[Clause]] = {}
    for clause in clauses:
        clause_by_pid.setdefault(clause.host_pid, []).append(clause)
    baselines: dict[tuple[int, int], dict[int, int]] = {
        (clause.host_pid, clause.exec_seq): {} for clause in clauses
    }
    for event in events:
        if event["type"] != "fork" or not event.get("child_host_tid"):
            continue
        target = _clause_at(clause_by_pid.get(event["host_pid"], ()), event["ts_ns"])
        if target is None:
            target = _ancestor_clause_pid(
                event["host_pid"],
                event["ts_ns"],
                clause_by_pid,
                fork_parent,
            )
        if target is not None:
            baselines[(target.host_pid, target.exec_seq)].setdefault(
                event["child_host_tid"], event["ts_ns"]
            )
    return baselines


def _task_io_totals(
    events: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    clause: Clause,
    fork_baselines: dict[int, int],
) -> tuple[tuple[int, int, int] | None, str, dict[str, Any]]:
    """Exact task-I/O-accounting deltas for one exec image.

    The image's exec boundary is its surviving TID's baseline. A new forked
    TID starts from the kernel's zeroed task I/O accounting. The first later
    exec or exit boundary is the exact endpoint, so adjacent exec images and
    owned descendants remain disjoint; perf samples are diagnostic only.
    """

    counter_events = [
        event
        for event in events
        if event["type"] in {"perf", "exec_boundary", "exit_boundary"}
    ]
    exec_baselines = [
        event
        for event in counter_events
        if event["type"] == "exec_boundary"
        and event["host_pid"] == clause.host_pid
        and event["exec_seq"] == clause.exec_seq
        and event["ts_ns"] == clause.t_exec_ns
    ]
    provenance: dict[str, Any] = {
        "source": "linux_task_io_accounting",
        "reduction": "nonnegative_per_tid_deltas_then_sum",
        "fields": {
            "read_bytes": "task->ioac.read_bytes",
            "write_bytes": "task->ioac.write_bytes",
            "cancelled_write_bytes": "task->ioac.cancelled_write_bytes",
        },
        "exec_boundary_baseline_tids": [],
        "zero_fork_baseline_tids": sorted(fork_baselines),
        "exact_endpoint_tids": [],
        "perf_sample_count": sum(event["type"] == "perf" for event in samples),
        "counter_regression_clamps": 0,
    }
    if len(exec_baselines) != 1:
        provenance["exec_boundary_count"] = len(exec_baselines)
        return None, "missing_or_ambiguous_exec_io_baseline", provenance

    root = exec_baselines[0]
    baselines: dict[int, tuple[int, dict[str, int]]] = {
        root["host_tid"]: (
            root["ts_ns"],
            {field: int(root[field]) for field in _IO_COUNTER_FIELDS},
        )
    }
    provenance["exec_boundary_baseline_tids"] = [root["host_tid"]]
    for tid, ts_ns in fork_baselines.items():
        baselines.setdefault(
            tid,
            (ts_ns, dict.fromkeys(_IO_COUNTER_FIELDS, 0)),
        )

    attributed_tids = {event["host_tid"] for event in samples}
    missing_baselines = sorted(attributed_tids - set(baselines))
    if missing_baselines:
        provenance["missing_baseline_tids"] = missing_baselines
        return None, "missing_tid_io_baseline", provenance

    totals = dict.fromkeys(_IO_COUNTER_FIELDS, 0)
    for tid, (baseline_ts, baseline) in baselines.items():
        endpoints = [
            event
            for event in counter_events
            if event["host_tid"] == tid
            and baseline_ts < event["ts_ns"] <= clause.t_end_ns
            and event["type"] in {"exec_boundary", "exit_boundary"}
        ]
        if not endpoints:
            provenance["missing_endpoint_tids"] = sorted(
                {
                    *provenance.get("missing_endpoint_tids", []),
                    tid,
                }
            )
            continue
        endpoint = min(endpoints, key=lambda event: event["ts_ns"])
        provenance["exact_endpoint_tids"].append(tid)
        for counter_field in _IO_COUNTER_FIELDS:
            delta = int(endpoint[counter_field]) - baseline[counter_field]
            if delta < 0:
                provenance["counter_regression_clamps"] += 1
                delta = 0
            totals[counter_field] += delta

    if provenance.get("missing_endpoint_tids"):
        return None, "missing_exact_tid_io_endpoint", provenance
    if provenance["counter_regression_clamps"]:
        return None, "io_counter_regression", provenance
    return (
        (
            totals["io_read_bytes"],
            totals["io_write_bytes"],
            totals["io_cancelled_write_bytes"],
        ),
        "ok",
        provenance,
    )


def analyze(
    run: RawRun,
    *,
    entry_pid: int | None = None,
) -> tuple[list[ClauseMetrics], list[dict[str, Any]]]:
    clauses, fork_parent = _clauses_and_lineage(run.events)
    per_clause, gaps = _attribute(
        run.events,
        clauses,
        fork_parent,
        entry_pid=entry_pid,
    )
    fork_io_baselines = _fork_io_baselines(run.events, clauses, fork_parent)
    metrics: list[ClauseMetrics] = []
    for c in clauses:
        attributed_samples = per_clause[(c.host_pid, c.exec_seq)]
        samples = [
            sample for sample in attributed_samples if "metric_excluded" not in sample
        ]
        identity_only_samples = [
            {
                "type": sample["type"],
                "ts_ns": sample["ts_ns"],
                "host_pid": sample["host_pid"],
                "host_tid": sample["host_tid"],
                "exec_seq": sample["exec_seq"],
                **sample["metric_excluded"],
            }
            for sample in attributed_samples
            if "metric_excluded" in sample
        ]
        in_window = sum(
            1
            for e in run.events
            if e["type"] in {"perf", "exec_boundary", "exit_boundary"}
            and c.t_exec_ns <= e["ts_ns"] <= c.t_end_ns
            and e["host_pid"] == c.host_pid
        )
        peak, cpu_reason, cpu_prov = _peak_cpu_cores(samples, c, run.quota_cores)
        rss, rss_reason, rss_prov = _sampled_peak_rss(samples, c)
        io_totals, io_reason, io_prov = _task_io_totals(
            run.events,
            samples,
            c,
            fork_io_baselines[(c.host_pid, c.exec_seq)],
        )
        has_exit = any(
            e["type"] == "exit_boundary" and e["host_pid"] == c.host_pid
            for e in run.events
        )
        # Raw cumulative CPU (preserved separately, never used for the peak):
        # deterministic group sum across the terminal process's threads.
        if c.terminal:
            exits = [
                e
                for e in run.events
                if e["type"] == "exit_boundary" and e["host_pid"] == c.host_pid
            ]
            cpu_cum = sum(e["cpu_ns"] for e in exits)
            leader = next(
                (e for e in exits if e["host_tid"] == e["host_pid"]),
                exits[0] if exits else None,
            )
            raw_exit_code = leader["exit_code"] if leader else None
            signal = (raw_exit_code & 0x7F) if raw_exit_code is not None else 0
            exit_signal = signal or None
            normal_exit_status = (
                (raw_exit_code >> 8) & 0xFF
                if raw_exit_code is not None and signal == 0
                else None
            )
        else:
            cpu_cum = 0
            exit_signal = None
            normal_exit_status = None
        metrics.append(
            ClauseMetrics(
                host_pid=c.host_pid,
                exec_seq=c.exec_seq,
                bin=c.bin,
                argv=c.argv,
                requested_executable_path=c.requested_executable_path,
                requested_executable_path_truncated=(
                    c.requested_executable_path_truncated
                ),
                bprm_filename=c.bprm_filename,
                bprm_interp=c.bprm_interp,
                bprm_evidence_truncated=c.bprm_evidence_truncated,
                exact_argc=c.exact_argc,
                lineage_parent_pid=c.lineage_parent_pid,
                terminal=c.terminal,
                has_causal_end=c.has_causal_end,
                t_exec_ns=c.t_exec_ns,
                t_end_ns=c.t_end_ns,
                wall_ns=c.t_end_ns - c.t_exec_ns,
                cpu_ns_cumulative=cpu_cum,
                exit_signal=exit_signal,
                normal_exit_status=normal_exit_status,
                peak_cpu_cores=peak,
                peak_cpu_cores_reason=cpu_reason,
                sampled_peak_rss_mb=rss,
                sampled_peak_rss_reason=rss_reason,
                disk_read_bytes_total=(io_totals[0] if io_totals is not None else None),
                disk_write_bytes_total=(
                    io_totals[1] if io_totals is not None else None
                ),
                disk_cancelled_write_bytes_total=(
                    io_totals[2] if io_totals is not None else None
                ),
                disk_io_reason=io_reason,
                cpu_windows=cpu_window_profile(samples),
                rss_bins=rss_bin_profile(samples),
                provenance={
                    "cadence_ns": SAMPLE_PERIOD_NS,
                    "window_ns": WINDOW_NS,
                    "align_bin_ns": ALIGN_BIN_NS,
                    "attributed_samples": len(samples),
                    "identity_only_sample_count": len(identity_only_samples),
                    "identity_only_samples": identity_only_samples,
                    "attribution_coverage": round(len(samples) / max(in_window, 1), 3),
                    "boundary_coverage": {
                        "has_exec": True,
                        "has_exit": has_exit,
                    },
                    "reserve_failures": run.loss_count,
                    "loss_counts": run.loss_counts,
                    "quota_cores": run.quota_cores,
                    "cpu": cpu_prov,
                    "rss": rss_prov,
                    "disk_io": io_prov,
                    "sample_attribution": {
                        "inherited_owner_sample_count": sum(
                            "attribution" in sample for sample in samples
                        ),
                        "inherited_owner_samples": [
                            sample["attribution"]
                            for sample in samples
                            if "attribution" in sample
                        ],
                    },
                },
                argv_capture_flags=c.argv_capture_flags,
            )
        )
    return metrics, gaps


class ClauseTelemetryIntegrityError(RuntimeError):
    """Stage-2 data cannot be used without hiding a coverage or lifecycle gap."""

    def __init__(
        self,
        message: str,
        *,
        artifact_payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.artifact_payload = dict(artifact_payload or {})


def _command_tree_provenance(
    metrics: Sequence[Clause | ClauseMetrics],
    fork_parent: Mapping[int, int],
    *,
    fork_records: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
) -> tuple[int, set[int], dict[str, Any]]:
    """Identify transitive exec roots and their one observed outside parent."""

    exec_pids = {metric.host_pid for metric in metrics}
    ancestry: list[dict[str, Any]] = []
    roots: list[int] = []
    entry_by_root: dict[int, int] = {}
    failure: str | None = None
    fork_ambiguities: list[dict[str, Any]] = []
    for pid in sorted(exec_pids):
        chain: list[int] = []
        nearest_exec_ancestor: int | None = None
        current = pid
        seen = {pid}
        ancestor_ts_bound = min(
            metric.t_exec_ns for metric in metrics if metric.host_pid == pid
        )
        while current in fork_parent or (
            fork_records is not None and current in fork_records
        ):
            eligible_records = (
                [
                    record
                    for record in fork_records[current]
                    if int(record["ts_ns"]) <= ancestor_ts_bound
                ]
                if fork_records is not None and current in fork_records
                else [
                    {
                        "host_pid": int(fork_parent[current]),
                        "ts_ns": ancestor_ts_bound,
                    }
                ]
            )
            if len(eligible_records) != 1:
                failure = "ambiguous_fork_ancestry"
                if not eligible_records:
                    failure = "temporally_invalid_fork_ancestry"
                evidence_records = (
                    eligible_records
                    if eligible_records
                    else list(fork_records.get(current, ()))
                )
                fork_ambiguities.append(
                    {
                        "child_pid": current,
                        "parent_candidates": sorted(
                            {int(record["host_pid"]) for record in evidence_records}
                        ),
                        "candidate_records": sorted(
                            (
                                {
                                    "parent_pid": int(record["host_pid"]),
                                    "ts_ns": int(record["ts_ns"]),
                                }
                                for record in evidence_records
                            ),
                            key=lambda record: (
                                record["ts_ns"],
                                record["parent_pid"],
                            ),
                        ),
                    }
                )
                break
            fork_record = eligible_records[0]
            parent = int(fork_record["host_pid"])
            if parent <= 0 or parent in seen:
                failure = "invalid_or_cyclic_fork_ancestry"
                break
            chain.append(parent)
            seen.add(parent)
            if nearest_exec_ancestor is None and parent in exec_pids:
                nearest_exec_ancestor = parent
            current = parent
            ancestor_ts_bound = int(fork_record["ts_ns"])
        is_root = nearest_exec_ancestor is None
        if is_root:
            roots.append(pid)
            if chain:
                entry_by_root[pid] = chain[-1]
            else:
                failure = failure or "missing_root_ancestry"
        ancestry.append(
            {
                "exec_pid": pid,
                "ancestor_chain": chain,
                "nearest_exec_ancestor_pid": nearest_exec_ancestor,
                "is_root": is_root,
            }
        )

    entries = sorted(set(entry_by_root.values()))
    if not exec_pids:
        failure = "no_exec_images"
    elif len(entries) != 1 or len(entry_by_root) != len(roots):
        failure = failure or "disconnected_command_trees"
    provenance = {
        "status": "failed" if failure else "ok",
        "reason": failure,
        "entry_pid": entries[0] if not failure else None,
        "root_pids": roots,
        "exec_ancestry": ancestry,
    }
    if fork_ambiguities:
        provenance["fork_ambiguities"] = fork_ambiguities
    if failure:
        raise ClauseTelemetryIntegrityError(
            "cannot identify one connected command tree: "
            f"reason={failure} roots={roots} entries={entries}",
            artifact_payload={"provenance": {"command_tree": provenance}},
        )
    return entries[0], set(roots), provenance


def validate_clause_telemetry_runtime(
    *,
    container_executable: str | None,
    concurrency: int,
    workers: int,
) -> None:
    """Fail before container preparation when clause telemetry is unsupported."""

    if sys.platform != "linux":
        raise ValueError("clause telemetry requires Linux")
    if os.geteuid() != 0:
        raise ValueError("clause telemetry requires root")
    if container_executable != "docker":
        raise ValueError("clause telemetry requires --container docker")
    if workers != 1:
        raise ValueError("clause telemetry requires --workers 1")
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise ValueError("clause telemetry requires cgroup v2")
    try:
        _ensure_bcc_importable()
    except ImportError as exc:
        raise ValueError(
            "clause telemetry requires BCC Python bindings in the active interpreter"
        ) from exc


def _container_cgroup(
    container_id: str,
    container_executable: str,
) -> tuple[Path, int]:
    result = subprocess.run(
        [
            container_executable,
            "inspect",
            container_id,
            "--format",
            "{{.State.Pid}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"cannot resolve container host pid: {detail}")
    init_pid = int(result.stdout.strip())
    cgroup_lines = Path(f"/proc/{init_pid}/cgroup").read_text().splitlines()
    unified = next(
        (line.split(":", 2)[2] for line in cgroup_lines if line.startswith("0::")),
        None,
    )
    if unified is None:
        raise RuntimeError(f"container {container_id[:12]} has no cgroup-v2 path")
    cgroup = Path("/sys/fs/cgroup") / unified.lstrip("/")
    if not cgroup.is_dir():
        raise RuntimeError(f"container cgroup does not exist: {cgroup}")
    return cgroup, init_pid


def _event_row(table: Any, data: int) -> dict[str, Any]:
    event = table.event(data)
    row = {
        "type": TYPE_NAMES[int(event.type)],
        "ts_ns": int(event.timestamp_ns),
        "cgroup_id": int(event.cgroup_id),
        "exec_seq": int(event.exec_seq),
        "cpu_ns": int(event.cpu_ns),
        "rss_pages": int(event.rss_pages),
        "mm_ptr": int(event.mm_ptr),
        "hiwater_pages": int(event.hiwater_pages),
        "io_read_bytes": int(event.io_read_bytes),
        "io_write_bytes": int(event.io_write_bytes),
        "io_cancelled_write_bytes": int(event.io_cancelled_write_bytes),
        "host_pid": int(event.host_pid),
        "host_tid": int(event.host_tid),
        "parent_host_pid": int(event.parent_host_pid),
        "child_host_pid": int(event.child_host_pid),
        "child_host_tid": int(event.child_host_tid),
        "arg_index": int(event.arg_index),
        "arg_chunk_index": int(event.arg_chunk_index),
        "arg_flags": int(event.arg_flags),
        "exit_code": int(event.exit_code),
        "errno": (
            int(event.exit_code)
            if TYPE_NAMES[int(event.type)] == "failed_exec_attempt"
            else 0
        ),
    }
    if event.type in {1, 7, 8, 9}:
        payload = bytes(event.arg).split(b"\0", 1)[0]
        row["arg"] = payload.decode("utf-8", "replace")
        if event.type == 1:
            row["arg_raw"] = payload.hex()
    return row


def _counter(bpf: Any, name: str) -> int:
    return int(bpf[name][ctypes.c_int(0)].value)


def _loss_counts(bpf: Any) -> dict[str, int]:
    return {name: _counter(bpf, name) for name in LOSS_COUNTER_NAMES}


def _loss_delta(bpf: Any, token: ToolCallToken) -> dict[str, int]:
    before = {
        "ringbuf_reserve_failures": token.ringbuf_reserve_failures,
        "argv_read_failures": token.argv_read_failures,
        "argv_boundary_read_failures": token.argv_boundary_read_failures,
    }
    return {name: _counter(bpf, name) - before[name] for name in LOSS_COUNTER_NAMES}


def _exec_image_record(metric: ClauseMetrics) -> Any:
    from tool_resource.clause_bridge import ExecImageRecord

    return ExecImageRecord(
        host_pid=metric.host_pid,
        exec_seq=metric.exec_seq,
        t_exec_ns=metric.t_exec_ns,
        t_end_ns=metric.t_end_ns,
        bin=metric.bin,
        argv=metric.argv,
        terminal=metric.terminal,
        cpu_windows=metric.cpu_windows,
        rss_bins=metric.rss_bins,
        peak_cpu_cores=metric.peak_cpu_cores,
        peak_cpu_reason=metric.peak_cpu_cores_reason,
        sampled_peak_rss_mb=metric.sampled_peak_rss_mb,
        sampled_rss_reason=metric.sampled_peak_rss_reason,
        disk_read_bytes_total=metric.disk_read_bytes_total,
        disk_write_bytes_total=metric.disk_write_bytes_total,
        disk_cancelled_write_bytes_total=metric.disk_cancelled_write_bytes_total,
        disk_io_reason=metric.disk_io_reason,
        cpu_ns_cumulative=metric.cpu_ns_cumulative,
        exit_signal=metric.exit_signal,
        normal_exit_status=metric.normal_exit_status,
        has_causal_end=metric.has_causal_end,
        argv_capture_flags=metric.argv_capture_flags,
        requested_executable_path=metric.requested_executable_path,
        requested_executable_path_truncated=(
            metric.requested_executable_path_truncated
        ),
        exact_argc=metric.exact_argc,
        argv_capped=bool(metric.argv_capture_flags & (1 << MAX_ARGS)),
        truncated_words=tuple(
            index
            for index in range(min(len(metric.argv), MAX_ARGS))
            if metric.argv_capture_flags & (1 << index)
        ),
        bprm_filename=metric.bprm_filename,
        bprm_interp=metric.bprm_interp,
        bprm_evidence_truncated=metric.bprm_evidence_truncated,
        provenance=metric.provenance,
    )


def _failed_exec_attempt_records(events: list[dict[str, Any]]) -> list[Any]:
    from tool_resource.clause_bridge import FailedExecAttempt

    argv_words, argv_capture_flags = _captured_argv(events)
    attempts: list[FailedExecAttempt] = []
    for event in events:
        if event["type"] != "failed_exec_attempt":
            continue
        words = argv_words.get((event["host_pid"], event["exec_seq"]), {})
        argv = tuple(words[index] for index in sorted(words))
        attempts.append(
            FailedExecAttempt(
                host_pid=event["host_pid"],
                exec_seq=event["exec_seq"],
                ts_ns=event["ts_ns"],
                argv=argv,
                errno=event["errno"],
                argv_capture_flags=argv_capture_flags.get(
                    (event["host_pid"], event["exec_seq"]), 0
                ),
            )
        )
    return attempts


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event["type"])
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


class ClauseTelemetryCollector:
    """One BPF program, armed once, delimiting serial exec tool calls."""

    def __init__(
        self,
        *,
        container_id: str,
        container_executable: str,
        repo: str,
        artifact_path: Path,
        source_actions: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        _ensure_bcc_importable()
        from bcc import BPF, PerfSWConfig, PerfType

        cgroup, init_pid = _container_cgroup(container_id, container_executable)
        self.container_id = container_id
        self.cgroup = cgroup
        self.cgroup_id = cgroup.stat().st_ino
        self.init_pid = init_pid
        self.quota_cores = observed_quota_cores(cgroup)
        self.repo = repo
        self.artifact_path = artifact_path
        self._epoch_offset_s = time.time() - time.monotonic()
        self._events: list[dict[str, Any]] = []
        self._events_lock = threading.Lock()
        self._stop_poll = threading.Event()
        self._poll_error: BaseException | None = None
        self._active: ToolCallToken | None = None
        self._closed = False
        self.state = "active"
        self._disabled_reason: str | None = None
        self._first_disabled_call: str | None = None
        self._cleanup_status = "pending"
        self._integrity_errors: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self._source_exec_actions = [
            action
            for action in source_actions
            if action.get("action_type") == "tool_exec"
            and isinstance(action.get("data"), Mapping)
            and action["data"].get("tool_name") == "exec"
        ]
        self._source_exec_index = 0

        try:
            self._bpf = BPF(text=BPF_PROGRAM)
        except BaseException as exc:
            self._closed = True
            self._cleanup_status = "not_started"
            raise RuntimeError(
                _bpf_setup_error_message("BPF module load failed", exc)
            ) from exc
        try:
            _attach_first_kprobe(
                self._bpf,
                BPF,
                syscall="execve",
                fn_name="capture_sys_execve",
            )
            _attach_first_kprobe(
                self._bpf,
                BPF,
                syscall="execveat",
                fn_name="capture_sys_execveat",
            )
            _attach_first_kprobe(
                self._bpf,
                BPF,
                syscall="execve",
                fn_name="capture_sys_execve_return",
                retprobe=True,
            )
            _attach_first_kprobe(
                self._bpf,
                BPF,
                syscall="execveat",
                fn_name="capture_sys_execveat_return",
                retprobe=True,
            )
            self._bpf.attach_kprobe(
                event="bprm_execve", fn_name="capture_bprm_argv"
            )
            self._bpf.attach_kprobe(
                event="bprm_change_interp", fn_name="capture_interp_change"
            )
            queue = self._bpf["exec_sequences"]
            for sequence in range(8192):
                queue.push(ctypes.c_ulonglong(sequence))
            self._bpf["sequence_ready"][ctypes.c_int(0)] = ctypes.c_uint(1)
            self._bpf["target_cgroup"][ctypes.c_int(0)] = ctypes.c_ulonglong(
                self.cgroup_id
            )
            self._table = self._bpf["events"]

            def receive(_ctx: int, data: int, _size: int) -> int:
                row = _event_row(self._table, data)
                with self._events_lock:
                    self._events.append(row)
                return 0

            self._table.open_ring_buffer(receive)

            def poll() -> None:
                try:
                    while not self._stop_poll.is_set():
                        self._bpf.ring_buffer_poll(timeout=10)
                except BaseException as exc:
                    if not self._stop_poll.is_set():
                        self._poll_error = exc

            self._poller = threading.Thread(
                target=poll,
                name="clause-telemetry-ring-poller",
                daemon=True,
            )
            self._poller.start()
            self._bpf.attach_perf_event(
                ev_type=PerfType.SOFTWARE,
                ev_config=PerfSWConfig.CPU_CLOCK,
                fn_name="on_cpu_clock",
                sample_period=SAMPLE_PERIOD_NS,
            )
            self._perf_type = PerfType
            self._perf_config = PerfSWConfig
        except BaseException as exc:
            self._stop_poll.set()
            poller = getattr(self, "_poller", None)
            if poller is not None:
                poller.join(timeout=2)
            self._bpf.cleanup()
            self._closed = True
            self._cleanup_status = "ok"
            raise RuntimeError(
                _bpf_setup_error_message("BPF collector attach failed", exc)
            ) from exc

    @classmethod
    def unavailable(
        cls,
        *,
        repo: str,
        artifact_path: Path,
        reason: str,
        container_id: str = "",
        source_actions: Sequence[Mapping[str, Any]] = (),
    ) -> "ClauseTelemetryCollector":
        """Return a disabled collector when setup cannot arm BPF."""

        collector = object.__new__(cls)
        collector.container_id = container_id
        collector.cgroup = None
        collector.cgroup_id = 0
        collector.init_pid = 0
        collector.quota_cores = 0.0
        collector.repo = repo
        collector.artifact_path = artifact_path
        collector._epoch_offset_s = time.time() - time.monotonic()
        collector._events = []
        collector._events_lock = threading.Lock()
        collector._stop_poll = threading.Event()
        collector._poll_error = None
        collector._active = None
        collector._closed = False
        collector.state = "disabled"
        collector._disabled_reason = reason
        collector._first_disabled_call = None
        collector._cleanup_status = "not_started"
        collector._integrity_errors = [reason]
        collector.calls = []
        collector._source_exec_actions = [
            action
            for action in source_actions
            if action.get("action_type") == "tool_exec"
            and isinstance(action.get("data"), Mapping)
            and action["data"].get("tool_name") == "exec"
        ]
        collector._source_exec_index = 0
        return collector

    def _disable(self, reason: str, *, tool_call_id: str | None = None) -> None:
        if self.state == "closed":
            return
        if self.state == "active":
            self.state = "disabled"
            self._disabled_reason = reason
            self._first_disabled_call = tool_call_id
        if reason not in self._integrity_errors:
            self._integrity_errors.append(reason)

    def _source_fields(self) -> tuple[str, str, str]:
        source_action = (
            self._source_exec_actions[self._source_exec_index]
            if self._source_exec_index < len(self._source_exec_actions)
            else None
        )
        self._source_exec_index += 1
        return _source_exec_fields(source_action)

    def _unavailable_call(
        self,
        token: ToolCallToken,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        unavailable_reason = reason or self._disabled_reason or "collector_disabled"
        summary = {
            "version": 2,
            "tool_call_id": token.tool_call_id,
            "tool_trace_ref": token.tool_call_id,
            "command": token.command,
            "telemetry_quality": "unavailable",
            "eligible_for_kb": False,
            "invalid_reasons": [
                {"kind": "collector_disabled", "detail": unavailable_reason}
            ],
            "mapping": {
                "static_clause_count": 0,
                "mappable_clause_count": 0,
                "mapped_clause_count": 0,
                "observation_clause_count": 0,
                "no_runtime_exec_count": 0,
                "coverage": 0.0,
                "gaps": [],
                "unobserved_builtins": [],
            },
            "clauses": [],
            "no_runtime_exec": [],
            "integrity": {
                "status": "unavailable",
                "errors": [f"unavailable:collector_disabled:{unavailable_reason}"],
            },
        }
        self.calls.append(summary)
        return summary

    def begin_tool_call(self, tool_call_id: str, command: str) -> ToolCallToken:
        source_tool_call_id, source_command, source_tool_result = self._source_fields()
        if self.state == "active" and self._poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(self._poll_error).__name__}: {self._poll_error}",
                tool_call_id=tool_call_id,
            )
        if self._active is not None:
            self._disable(
                f"overlapping exec tool calls: {self._active.tool_call_id}, "
                f"{tool_call_id}",
                tool_call_id=tool_call_id,
            )
            self._unavailable_call(self._active, reason="exec delimiter desynchronized")
            self._active = None
        if not tool_call_id:
            self._disable("exec tool call has no tool_call_id", tool_call_id=tool_call_id)
        counters = dict.fromkeys(
            (
                "ringbuf_reserve_failures",
                "perf_sample_count",
                "argv_read_failures",
                "argv_boundary_read_failures",
            ),
            0,
        )
        if self.state == "active":
            try:
                counters = {
                    "ringbuf_reserve_failures": _counter(
                        self._bpf, "ringbuf_reserve_failures"
                    ),
                    "perf_sample_count": _counter(self._bpf, "perf_sample_count"),
                    "argv_read_failures": _counter(self._bpf, "argv_read_failures"),
                    "argv_boundary_read_failures": _counter(
                        self._bpf, "argv_boundary_read_failures"
                    ),
                }
            except BaseException as exc:
                self._disable(
                    f"collector counter read failed: {type(exc).__name__}: {exc}",
                    tool_call_id=tool_call_id,
                )
        token = ToolCallToken(
            tool_call_id=tool_call_id,
            command=command,
            started_ns=time.monotonic_ns(),
            ringbuf_reserve_failures=int(counters["ringbuf_reserve_failures"]),
            perf_sample_count=int(counters["perf_sample_count"]),
            argv_read_failures=int(counters["argv_read_failures"]),
            argv_boundary_read_failures=int(
                counters["argv_boundary_read_failures"]
            ),
            source_tool_call_id=source_tool_call_id,
            source_command=source_command,
            source_tool_result=source_tool_result,
        )
        self._active = token
        return token

    def finish_tool_call(
        self,
        token: ToolCallToken,
        *,
        replay_response: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if token is not self._active:
            self._disable(
                f"exec delimiter mismatch for {token.tool_call_id}",
                tool_call_id=token.tool_call_id,
            )
            return self._unavailable_call(token, reason="exec delimiter desynchronized")
        ended_ns = time.monotonic_ns()
        self._active = None
        if self.state != "active":
            return self._unavailable_call(token)
        if self._poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(self._poll_error).__name__}: {self._poll_error}",
                tool_call_id=token.tool_call_id,
            )
            return self._unavailable_call(token)
        # The kernel timestamps events before ring delivery. Let the poller drain,
        # then slice on the captured end timestamp; command timing is unchanged.
        try:
            time.sleep(0.03)
            loss_counts = _loss_delta(self._bpf, token)
            perf_samples = (
                _counter(self._bpf, "perf_sample_count") - token.perf_sample_count
            )
            with self._events_lock:
                events = sorted(
                    (
                        event
                        for event in self._events
                        if token.started_ns <= event["ts_ns"] <= ended_ns
                        and event["cgroup_id"] == self.cgroup_id
                    ),
                    key=lambda event: event["ts_ns"],
                )
        except BaseException as exc:
            self._disable(
                f"collector finish failed: {type(exc).__name__}: {exc}",
                tool_call_id=token.tool_call_id,
            )
            return self._unavailable_call(token)
        replay_result = (
            str(replay_response.get("result") or "")
            if replay_response is not None
            else ""
        )
        replay_stderr = (
            str(replay_response.get("stderr") or "")
            if replay_response is not None
            else ""
        )
        raw_replay_exit = (
            replay_response.get("returncode") if replay_response is not None else None
        )
        replay_exit_code = (
            raw_replay_exit
            if isinstance(raw_replay_exit, int)
            and not isinstance(raw_replay_exit, bool)
            else None
        )
        protocol_timeout = _is_protocol_timeout(
            replay_exit_code,
            replay_result,
        )
        source_exit_code = _strict_exit_code(token.source_tool_result)
        replay_tool_result = _replay_tool_result(
            replay_response,
            replay_exit_code,
        )
        control_flow_fidelity = {
            "source_action_available": bool(token.source_tool_call_id),
            "source_command_matches": token.source_command == token.command,
            "source_exit_code": source_exit_code,
            "replay_exit_code": replay_exit_code,
            "exit_code_matches": (
                source_exit_code is not None
                and replay_exit_code is not None
                and source_exit_code == replay_exit_code
            ),
            "tool_result_exact": (
                bool(token.source_tool_call_id)
                and token.source_tool_result == replay_tool_result
            ),
        }
        control_flow_fidelity["short_circuit_eligible"] = (
            control_flow_fidelity["source_command_matches"]
            and control_flow_fidelity["exit_code_matches"]
            and control_flow_fidelity["tool_result_exact"]
        )
        lookup_failure = shell_command_lookup_failure_evidence(
            command=token.command,
            source_tool_call_id=token.source_tool_call_id,
            replay_tool_call_id=token.tool_call_id,
            source_command=token.source_command,
            source_tool_result=token.source_tool_result,
            replay_result=replay_result,
            replay_stderr=replay_stderr,
            replay_exit_code=replay_exit_code,
        )
        try:
            summary, violations = self._summarize_call(
                token=token,
                ended_ns=ended_ns,
                events=events,
                loss_counts=loss_counts,
                perf_samples=perf_samples,
                command_lookup_failure=lookup_failure,
                control_flow_fidelity=control_flow_fidelity,
                protocol_timeout=protocol_timeout,
            )
        except Exception as exc:
            message = (
                f"{token.tool_call_id}: telemetry analysis failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if isinstance(exc, ClauseTelemetryIntegrityError):
                self._integrity_errors.append(message)
            else:
                self._disable(message, tool_call_id=token.tool_call_id)
            failed_call = {
                "version": 2,
                "tool_call_id": token.tool_call_id,
                "tool_trace_ref": token.tool_call_id,
                "command": token.command,
                "telemetry_quality": "invalid",
                "eligible_for_kb": False,
                "invalid_reasons": [
                    {"kind": "analysis_failure", "detail": message}
                ],
                "integrity": {"status": "failed", "errors": [message]},
            }
            if isinstance(exc, ClauseTelemetryIntegrityError):
                failed_call.update(exc.artifact_payload)
            self.calls.append(failed_call)
            return failed_call
        self.calls.append(summary)
        for violation in violations:
            if violation not in self._integrity_errors:
                self._integrity_errors.append(violation)
        return summary

    def record_safety_guard_blocked(
        self,
        tool_call_id: str,
        command: str,
        replay_result: str,
    ) -> dict[str, Any]:
        """Record an exec rejected before the container runtime was entered."""

        from tool_resource.clause_bridge import SafetyGuardBlockEvidence

        token = self.begin_tool_call(tool_call_id, command)
        ended_ns = time.monotonic_ns()
        self._active = None
        if self.state != "active":
            return self._unavailable_call(token)
        try:
            loss_counts = _loss_delta(self._bpf, token)
            perf_samples = (
                _counter(self._bpf, "perf_sample_count") - token.perf_sample_count
            )
        except BaseException as exc:
            self._disable(
                f"collector finish failed: {type(exc).__name__}: {exc}",
                tool_call_id=tool_call_id,
            )
            return self._unavailable_call(token)
        evidence = SafetyGuardBlockEvidence(
            command=command,
            source_command=token.source_command,
            source_tool_call_id=token.source_tool_call_id,
            replay_tool_call_id=token.tool_call_id,
            source_result=token.source_tool_result,
            replay_result=replay_result,
        )
        fidelity = {
            "source_action_available": bool(token.source_tool_call_id),
            "source_command_matches": token.source_command == command,
            "source_exit_code": None,
            "replay_exit_code": None,
            "exit_code_matches": False,
            "tool_result_exact": token.source_tool_result == replay_result,
            "short_circuit_eligible": False,
        }
        try:
            summary, violations = self._summarize_call(
                token=token,
                ended_ns=ended_ns,
                events=[],
                loss_counts=loss_counts,
                perf_samples=perf_samples,
                control_flow_fidelity=fidelity,
                safety_guard_blocked=evidence,
            )
        except Exception as exc:
            message = (
                f"{token.tool_call_id}: telemetry analysis failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if isinstance(exc, ClauseTelemetryIntegrityError):
                self._integrity_errors.append(message)
            else:
                self._disable(message, tool_call_id=token.tool_call_id)
            summary = {
                "version": 2,
                "tool_call_id": token.tool_call_id,
                "tool_trace_ref": token.tool_call_id,
                "command": token.command,
                "telemetry_quality": "invalid",
                "eligible_for_kb": False,
                "invalid_reasons": [
                    {"kind": "analysis_failure", "detail": message}
                ],
                "integrity": {"status": "failed", "errors": [message]},
            }
            violations = [message]
        self.calls.append(summary)
        for violation in violations:
            if violation not in self._integrity_errors:
                self._integrity_errors.append(violation)
        return summary

    def _summarize_call(
        self,
        *,
        token: ToolCallToken,
        ended_ns: int,
        events: list[dict[str, Any]],
        loss_counts: Mapping[str, int],
        perf_samples: int,
        command_lookup_failure: ShellCommandLookupFailure | None = None,
        control_flow_fidelity: Mapping[str, Any] | None = None,
        safety_guard_blocked: Any | None = None,
        protocol_timeout: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        from tool_resource.clause_bridge import bridge_command

        normalized_loss_counts = {
            name: int(loss_counts.get(name, 0)) for name in LOSS_COUNTER_NAMES
        }
        loss = sum(normalized_loss_counts.values())
        run = RawRun(
            cgroup_id=self.cgroup_id,
            quota_cores=self.quota_cores,
            status=0,
            wall_ns=ended_ns - token.started_ns,
            usage_usec=0,
            ringbuf_reserve_failures=normalized_loss_counts["ringbuf_reserve_failures"],
            perf_sample_count=perf_samples,
            oracle_peak_rss_kb=0,
            oracle_samples=0,
            marker=True,
            events=events,
            argv_read_failures=normalized_loss_counts["argv_read_failures"],
            argv_boundary_read_failures=normalized_loss_counts[
                "argv_boundary_read_failures"
            ],
        )
        fork_records: dict[int, list[dict[str, Any]]] = {}
        for event in events:
            if event["type"] == "fork" and event["child_host_pid"]:
                fork_records.setdefault(event["child_host_pid"], []).append(event)
        fork_parent = {
            child: next(iter({record["host_pid"] for record in records}))
            for child, records in fork_records.items()
            if len({record["host_pid"] for record in records}) == 1
        }
        clauses, _ = _clauses_and_lineage(events)
        if safety_guard_blocked is not None and not clauses and not events:
            entry_pid = 0
            root_pids: set[int] = set()
            command_tree = {
                "status": "not_applicable",
                "reason": "safety_guard_blocked_before_runtime",
                "entry_pid": None,
                "root_pids": [],
                "exec_ancestry": [],
            }
        else:
            entry_pid, root_pids, command_tree = _command_tree_provenance(
                clauses,
                fork_parent,
                fork_records=fork_records,
            )
        metrics, attribution_gaps = analyze(run, entry_pid=entry_pid)

        def command_descendant(pid: int) -> bool:
            current = pid
            seen: set[int] = set()
            while current and current not in seen:
                if current in root_pids:
                    return True
                seen.add(current)
                current = fork_parent.get(current, 0)
            return False

        def gap_payload(event: Mapping[str, Any]) -> dict[str, Any]:
            structural_setup_reasons = {
                "entry_fork_pre_exec_structural_setup",
                "initial_exec_pending_pre_boundary_structural_setup",
            }
            if event["reason"] in structural_setup_reasons:
                relation = event["reason"]
            elif event["host_pid"] == entry_pid:
                relation = (
                    "entry_parent"
                    if event["host_tid"] == entry_pid
                    else "entry_parent_thread"
                )
            elif command_descendant(event["host_pid"]):
                relation = "command_descendant"
            else:
                relation = "outside_entry_parent_and_command"
            lineage_id = (
                event["host_tid"]
                if event["host_tid"] != event["host_pid"]
                else event["host_pid"]
            )
            payload = {
                "type": event["type"],
                "ts_ns": event["ts_ns"],
                "host_pid": event["host_pid"],
                "host_tid": event["host_tid"],
                "exec_seq": event["exec_seq"],
                "entry_pid": entry_pid,
                "entry_parent_relation": relation,
                "fork_parent_pid": fork_parent.get(lineage_id),
                "reason": event["reason"],
            }
            if "fork_ancestry" in event:
                payload["fork_ancestry"] = list(event["fork_ancestry"])
            if "fork_chain_records" in event:
                payload["fork_chain_records"] = list(event["fork_chain_records"])
            if "fork_resolution_failure" in event:
                payload["fork_resolution_failure"] = dict(
                    event["fork_resolution_failure"]
                )
            if "fork_ts_ns" in event:
                payload["fork_ts_ns"] = event["fork_ts_ns"]
            if "pending_exec_evidence" in event:
                payload["pending_exec_evidence"] = dict(event["pending_exec_evidence"])
            return payload

        gap_evidence = [gap_payload(event) for event in attribution_gaps]
        relevant_gaps = [
            event
            for event in gap_evidence
            if event["entry_parent_relation"]
            not in {
                "entry_parent",
                "entry_parent_thread",
                "entry_fork_pre_exec_structural_setup",
                "initial_exec_pending_pre_boundary_structural_setup",
            }
        ]
        structural_gaps = [
            event
            for event in gap_evidence
            if event["entry_parent_relation"]
            in {
                "entry_parent",
                "entry_parent_thread",
                "entry_fork_pre_exec_structural_setup",
                "initial_exec_pending_pre_boundary_structural_setup",
            }
        ]
        exec_image_records = [_exec_image_record(metric) for metric in metrics]
        bridge = bridge_command(
            self.repo,
            token.command,
            exec_image_records,
            failed_exec_attempts=[
                attempt
                for attempt in _failed_exec_attempt_records(events)
                if command_descendant(attempt.host_pid)
            ],
            command_lookup_failure=command_lookup_failure,
            safety_guard_blocked=safety_guard_blocked,
            allow_control_short_circuit=bool(
                control_flow_fidelity
                and control_flow_fidelity.get("short_circuit_eligible") is True
            ),
            entry_pid=entry_pid,
            fork_parent=fork_parent,
            epoch_offset=self._epoch_offset_s,
            loss_count=loss,
            attribution_gap_count=len(relevant_gaps),
            protocol_timeout=protocol_timeout,
        )
        mapping_gaps = [
            {"kind": gap.kind, "detail": gap.detail} for gap in bridge.coverage_gaps
        ]
        clauses = [
            {
                "bin": bridged.observation.bin,
                "argv": list(bridged.observation.argv),
                "ts_start": bridged.observation.ts_start,
                "ts_end": bridged.observation.ts_end,
                "latency_ms": bridged.observation.latency_ms,
                "peak_cpu_cores": bridged.observation.peak_cpu_cores,
                "sampled_peak_rss_mb": bridged.observation.sampled_peak_rss_mb,
                "cpu_ns_cumulative": bridged.observation.cpu_ns_cumulative,
                "in_loop": bridged.observation.in_loop,
                "in_pipe": bridged.observation.in_pipe,
                "in_subst": bridged.observation.in_subst,
                "pipeline_position": bridged.observation.pipeline_position,
                "disk_io": {
                    "read_bytes_total": bridged.disk_read_bytes_total,
                    "write_bytes_total": bridged.disk_write_bytes_total,
                    "cancelled_write_bytes_total": (
                        bridged.disk_cancelled_write_bytes_total
                    ),
                    "read_write_bytes_total": (
                        bridged.disk_read_bytes_total + bridged.disk_write_bytes_total
                        if bridged.disk_read_bytes_total is not None
                        and bridged.disk_write_bytes_total is not None
                        else None
                    ),
                    "availability": bridged.availability["disk_io"],
                },
                "availability": bridged.availability,
                "mapping_evidence": bridged.mapping_evidence,
                "owned_exec_image_count": len(bridged.owned_exec_images),
                "provenance": bridged.provenance,
            }
            for bridged in bridge.bridged
        ]

        def no_runtime_exec_row(resolved: Any) -> dict[str, Any]:
            row = {
                "bin": resolved.bin,
                "argv": list(resolved.argv),
                "availability": resolved.availability,
                "mapping_evidence": resolved.mapping_evidence,
                "attempt_count": len(resolved.attempts),
            }
            if resolved.safety_guard_blocked is not None:
                evidence = resolved.safety_guard_blocked
                row["provenance"] = {
                    "evidence_kind": "safety_guard_blocked_before_runtime",
                    "command": evidence.command,
                    "source": {
                        "tool_call_id": evidence.source_tool_call_id,
                        "command": evidence.source_command,
                        "result": evidence.source_result,
                    },
                    "replay": {
                        "tool_call_id": evidence.replay_tool_call_id,
                        "result": evidence.replay_result,
                    },
                }
                return row
            if resolved.control_short_circuit is not None:
                row["provenance"] = {
                    "evidence_kind": "shell_control_short_circuit",
                    **resolved.control_short_circuit,
                }
                return row
            if resolved.command_lookup_failure is None:
                row["errno"] = sorted({attempt.errno for attempt in resolved.attempts})
                row["provenance"] = {
                    "evidence_kind": "failed_execve",
                    "failed_exec_attempts": [
                        {
                            "host_pid": attempt.host_pid,
                            "exec_seq": attempt.exec_seq,
                            "ts_ns": attempt.ts_ns,
                            "errno": attempt.errno,
                        }
                        for attempt in resolved.attempts
                    ],
                }
                return row
            evidence = resolved.command_lookup_failure
            row["provenance"] = {
                "evidence_kind": "shell_command_lookup_failure",
                "parser": evidence.parser,
                "command": evidence.command,
                "executable_head": evidence.executable_head,
                "exit_code_semantics": evidence.exit_code_semantics,
                "source": {
                    "tool_call_id": evidence.source_tool_call_id,
                    "exit_code": evidence.source_exit_code,
                    "channel": evidence.source_channel,
                    "diagnostic": evidence.source_diagnostic,
                },
                "replay": {
                    "tool_call_id": evidence.replay_tool_call_id,
                    "exit_code": evidence.replay_exit_code,
                    "channel": evidence.replay_channel,
                    "diagnostic": evidence.replay_diagnostic,
                },
            }
            return row

        no_runtime_exec = [
            no_runtime_exec_row(resolved) for resolved in bridge.no_runtime_exec
        ]
        target_availability: dict[str, Any] = {}
        for target in ("latency", "cpu", "memory"):
            values = [
                clause["availability"][target]
                for clause in [*clauses, *no_runtime_exec]
            ]
            reasons: dict[str, int] = {}
            for value in values:
                reasons[value] = reasons.get(value, 0) + 1
            target_availability[target] = {
                "available": sum(value == "ok" for value in values),
                "total": len(values),
                "reasons": reasons,
            }
        mappable = bridge.static_clause_count - len(bridge.unobserved_builtins)
        mapped = len(bridge.bridged) + len(bridge.no_runtime_exec)
        summary = {
            "version": 2,
            "tool_call_id": token.tool_call_id,
            "tool_trace_ref": token.tool_call_id,
            "command": token.command,
            "static_word_intent": [
                {
                    "clause_index": index,
                    "bin": clause["bin"],
                    "argv": clause["argv"],
                    "span": clause["span"],
                    "structural_context": clause.get("structural_context", []),
                    "word_intents": clause.get("word_intents", []),
                }
                for index, clause in enumerate(bridge.static_clauses)
            ],
            "runtime_invocations": [
                {
                    "host_pid": image.host_pid,
                    "exec_seq": image.exec_seq,
                    "requested_executable_path": image.requested_executable_path,
                    "requested_executable_path_truncated": (
                        image.requested_executable_path_truncated
                    ),
                    "argv": list(image.argv),
                    "argc": image.exact_argc,
                    "argv_capped": image.argv_capped,
                    "truncated_words": list(image.truncated_words),
                    "bprm_filename": image.bprm_filename,
                    "bprm_interp": image.bprm_interp,
                    "bprm_evidence_truncated": image.bprm_evidence_truncated,
                }
                for image in exec_image_records
            ],
            "transition_graph": bridge.transition_graph,
            "candidate_rejections": bridge.candidate_rejections,
            "mapping": {
                "static_clause_count": bridge.static_clause_count,
                "mappable_clause_count": mappable,
                "mapped_clause_count": mapped,
                "observation_clause_count": len(bridge.observations),
                "no_runtime_exec_count": len(bridge.no_runtime_exec),
                "coverage": mapped / max(mappable, 1),
                "gaps": mapping_gaps,
                "unobserved_builtins": bridge.unobserved_builtins,
            },
            "target_availability": target_availability,
            "coverage_gaps": {
                "relevant": {
                    "count": len(relevant_gaps),
                    "event_types": _event_type_counts(relevant_gaps),
                    "events": relevant_gaps,
                },
                "structural": {
                    "count": len(structural_gaps),
                    "event_types": _event_type_counts(structural_gaps),
                    "events": structural_gaps,
                },
            },
            "telemetry_loss": {
                **normalized_loss_counts,
                "total": loss,
                "perf_sample_count": perf_samples,
            },
            "ring_loss": {
                "reserve_failures": loss,
                "perf_sample_count": perf_samples,
            },
            "clauses": clauses,
            "no_runtime_exec": no_runtime_exec,
            "provenance": {
                "collector": "stage2_ebpf",
                "repo": self.repo,
                "container_cgroup_id": self.cgroup_id,
                "quota_cores": self.quota_cores,
                "page_size_bytes": PAGE,
                "call_started_monotonic_ns": token.started_ns,
                "call_ended_monotonic_ns": ended_ns,
                "raw_event_count": len(events),
                "exec_image_count": len(metrics),
                "command_tree": command_tree,
                "source_replay_control_flow_fidelity": (
                    dict(control_flow_fidelity)
                    if control_flow_fidelity is not None
                    else {
                        "source_action_available": False,
                        "short_circuit_eligible": False,
                    }
                ),
                "cadence_ns": SAMPLE_PERIOD_NS,
                "window_ns": WINDOW_NS,
                "align_bin_ns": ALIGN_BIN_NS,
                "disk_io_semantics": "linux_task_io_accounting_total_bytes",
                "disk_io_fields": [
                    "task->ioac.read_bytes",
                    "task->ioac.write_bytes",
                    "task->ioac.cancelled_write_bytes",
                ],
            },
        }
        violations: list[str] = []
        if loss:
            causes = ",".join(
                f"{name}={count}"
                for name, count in normalized_loss_counts.items()
                if count
            )
            violations.append(f"{token.tool_call_id}: telemetry loss={loss} ({causes})")
        if relevant_gaps:
            violations.append(
                f"{token.tool_call_id}: relevant coverage gaps={len(relevant_gaps)}"
            )
        if mapping_gaps:
            kinds = sorted({gap["kind"] for gap in mapping_gaps})
            violations.append(f"{token.tool_call_id}: mapping gaps={','.join(kinds)}")
        invalid_reasons: list[dict[str, str]] = []
        if loss:
            invalid_reasons.append(
                {"kind": "telemetry_loss", "detail": violations[0]}
            )
        if relevant_gaps:
            invalid_reasons.append(
                {
                    "kind": "attribution_gap",
                    "detail": (
                        f"{token.tool_call_id}: relevant coverage "
                        f"gaps={len(relevant_gaps)}"
                    ),
                }
            )
        invalid_reasons.extend(mapping_gaps)
        summary["telemetry_quality"] = "invalid" if violations else "ok"
        summary["eligible_for_kb"] = not violations and bridge.data_valid
        summary["invalid_reasons"] = invalid_reasons
        for clause in summary["clauses"]:
            clause["telemetry_quality"] = summary["telemetry_quality"]
            clause["eligible_for_kb"] = summary["eligible_for_kb"]
        summary["integrity"] = {
            "status": "failed" if violations else "ok",
            "errors": violations,
        }
        return summary, violations

    def add_integrity_error(self, message: str) -> None:
        self._disable(message)

    def finalize(self, *, replay_execution: str = "completed") -> None:
        if self.state == "closed":
            return
        if replay_execution not in {"completed", "failed", "incomplete"}:
            raise ValueError(f"invalid replay execution state {replay_execution!r}")
        try:
            total_loss_counts = (
                _loss_counts(self._bpf)
                if self.state == "active"
                else dict.fromkeys(LOSS_COUNTER_NAMES, 0)
            )
        except BaseException as exc:
            total_loss_counts = dict.fromkeys(LOSS_COUNTER_NAMES, 0)
            self._disable(f"collector counter read failed: {type(exc).__name__}: {exc}")
        total_loss = sum(total_loss_counts.values())
        if self._active is not None:
            self._disable(
                f"unterminated exec delimiter: {self._active.tool_call_id}",
                tool_call_id=self._active.tool_call_id,
            )
            self._unavailable_call(self._active, reason="unterminated exec delimiter")
            self._active = None
        try:
            if hasattr(self, "_bpf"):
                self._close_bpf()
        except BaseException as exc:
            self._cleanup_status = "failed"
            self._disable(
                f"collector cleanup leak: {type(exc).__name__}: {exc}"
            )
        if total_loss:
            causes = ",".join(
                f"{name}={count}" for name, count in total_loss_counts.items() if count
            )
            self._integrity_errors.append(
                f"collector total telemetry loss={total_loss} ({causes})"
            )
        if self._poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(self._poll_error).__name__}: {self._poll_error}"
            )
        prior_state = self.state
        valid_count = sum(
            call.get("telemetry_quality") == "ok" for call in self.calls
        )
        invalid_count = sum(
            call.get("telemetry_quality") == "invalid" for call in self.calls
        )
        unavailable_count = sum(
            call.get("telemetry_quality") == "unavailable" for call in self.calls
        )
        eligible_count = sum(
            call.get("eligible_for_kb") is True for call in self.calls
        )
        collector_healthy = (
            prior_state == "active"
            and self._cleanup_status == "ok"
            and total_loss == 0
            and unavailable_count == 0
        )
        telemetry_quality = "ok" if collector_healthy else "unavailable"
        formal_completeness = (
            "unavailable"
            if not collector_healthy
            else ("complete" if eligible_count == len(self.calls) else "partial")
        )
        collection_validity = "valid" if collector_healthy else "invalid"
        call_errors = {
            str(error)
            for call in self.calls
            for error in (call.get("integrity") or {}).get("errors", [])
        }
        collector_errors = (
            [
                error
                for error in self._integrity_errors
                if error not in call_errors
            ]
            if collector_healthy
            else list(self._integrity_errors)
        )
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "mode": "clause",
                    "status_model": "call_granular_v1",
                    "container_id": self.container_id,
                    "cgroup_id": self.cgroup_id,
                    "quota_cores": self.quota_cores,
                    "calls": self.calls,
                    "telemetry_loss_total": {
                        **total_loss_counts,
                        "total": total_loss,
                    },
                    "ring_loss_total": total_loss,
                    "cleanup": self._cleanup_status,
                    "collector": {
                        "state": "closed",
                        "state_before_close": prior_state,
                        "health": (
                            "healthy" if collector_healthy else "unavailable"
                        ),
                        "first_disabled_call": self._first_disabled_call,
                        "disabled_reason": self._disabled_reason,
                        "valid_call_count": valid_count,
                        "invalid_call_count": invalid_count,
                        "unavailable_call_count": unavailable_count,
                        "eligible_call_count": eligible_count,
                    },
                    "call_coverage": {
                        "total_call_count": len(self.calls),
                        "eligible_call_count": eligible_count,
                        "withheld_call_count": len(self.calls) - eligible_count,
                        "eligible_fraction": (
                            eligible_count / len(self.calls) if self.calls else 1.0
                        ),
                    },
                    "replay_execution": replay_execution,
                    "telemetry_quality": telemetry_quality,
                    "formal_completeness": formal_completeness,
                    "collection_validity": collection_validity,
                    "integrity": {
                        "status": "ok" if collector_healthy else "failed",
                        "errors": collector_errors,
                    },
                    "provenance": {
                        "collector": "stage2_ebpf",
                        "repo": self.repo,
                        "page_size_bytes": PAGE,
                        "cadence_ns": SAMPLE_PERIOD_NS,
                        "window_ns": WINDOW_NS,
                        "align_bin_ns": ALIGN_BIN_NS,
                        "disk_io_semantics": (
                            "nonnegative_per_tid_linux_task_io_accounting_deltas"
                        ),
                        "disk_io_fields": [
                            "task->ioac.read_bytes",
                            "task->ioac.write_bytes",
                            "task->ioac.cancelled_write_bytes",
                        ],
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.state = "closed"

    def _close_bpf(self) -> None:
        if self._closed:
            return
        self._stop_poll.set()
        self._poller.join(timeout=2)
        if self._poller.is_alive():
            raise RuntimeError("ring poller did not stop")
        self._bpf.detach_perf_event(
            ev_type=self._perf_type.SOFTWARE,
            ev_config=self._perf_config.CPU_CLOCK,
        )
        self._bpf.cleanup()
        self._closed = True
        self._cleanup_status = "ok"


__all__ = [
    "ALIGN_BIN_NS",
    "BPF_PROGRAM",
    "Clause",
    "ClauseMetrics",
    "ClauseTelemetryCollector",
    "ClauseTelemetryIntegrityError",
    "RawRun",
    "SAMPLE_PERIOD_NS",
    "SENTINEL",
    "ToolCallToken",
    "WINDOW_NS",
    "analyze",
    "cpu_window_profile",
    "rss_bin_profile",
    "validate_clause_telemetry_runtime",
]
