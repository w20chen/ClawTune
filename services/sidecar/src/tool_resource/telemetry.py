"""eBPF clause telemetry: honest per-clause peak_cpu_cores + sampled_peak_rss.

Extends the Stage-1b lifecycle collector (exec/fork/exit/hiwater) with the
perf CPU-clock sampler validated by the accepted spike, and reconstructs, per
clause = (host_pid, exec_seq):

- ``peak_cpu_cores``: cumulative per-TID CPU deltas of the clause's own threads
  and non-exec descendants, aggregated into 500 ms wall windows (matching the
  resource_timeline label semantics), rate = Delta cpu_ns / window_ns, clipped
  ONLY to the observed cgroup quota; the max window rate. Never cpu_ns/wall_ns.
- ``sampled_peak_rss``: the maximum, over aligned time bins, of the SUM of
  fast kernel-reported current RSS across DISTINCT live ``mm`` address spaces
  in the clause lineage (the kernel counter can be approximate and its backend
  is recorded in provenance)
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

import atexit
import ctypes
import http.client
import importlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from functools import wraps
from urllib.parse import quote
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from tool_resource.clause_bridge import ShellCommandLookupFailure

# bcc is imported lazily inside the collection functions so the pure analysis
# (attribution, windowing, aggregation) can be imported and unit-tested without
# a bcc/BPF runtime.

_BCC_SEARCH_ROOTS = (Path("/usr/lib"), Path("/usr/lib64"), Path("/usr/local/lib"))
_BCC_BINDING_NAMES = ("bcc", "bpfcc")
_BCC_REQUIRED_ATTRIBUTES = ("BPF", "PerfSWConfig", "PerfType")


def _configure_bcc_tracefs(module: Any) -> Any:
    """Prefer the kernel's modern tracefs mount when packaged BCC is legacy."""
    configured = getattr(module, "TRACEFS", None)
    modern = Path("/sys/kernel/tracing")
    if configured and not Path(str(configured)).exists() and modern.exists():
        module.TRACEFS = str(modern)
    return module


def _ensure_bcc_importable() -> Any:
    """Return usable BCC bindings, including openEuler's ``bpfcc`` name.

    Debian-family packages expose the Python module as ``bcc`` while
    openEuler packages the same API as ``bpfcc``.  The latter is aliased in
    ``sys.modules`` so existing third-party BCC code importing ``bcc`` keeps
    working.  Distro package roots are also searched for Conda/venv Python
    interpreters that omit system site-packages.
    """

    failures: list[str] = []

    def load_candidate() -> Any | None:
        for module_name in _BCC_BINDING_NAMES:
            try:
                module = importlib.import_module(module_name)
            except (ImportError, OSError) as exc:
                failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
                continue
            missing = [
                attribute
                for attribute in _BCC_REQUIRED_ATTRIBUTES
                if not hasattr(module, attribute)
            ]
            if missing:
                failures.append(
                    f"{module_name}: missing required attributes {', '.join(missing)}"
                )
                continue
            sys.modules["bcc"] = module
            return _configure_bcc_tracefs(module)
        return None

    module = load_candidate()
    if module is not None:
        return module

    for root in _BCC_SEARCH_ROOTS:
        if not root.exists():
            continue
        for site_kind in ("site-packages", "dist-packages"):
            for module_name in _BCC_BINDING_NAMES:
                for package_dir in root.glob(f"**/{site_kind}/{module_name}"):
                    parent = str(package_dir.parent)
                    if parent not in sys.path:
                        sys.path.append(parent)
                    importlib.invalidate_caches()
                    module = load_candidate()
                    if module is not None:
                        return module

    detail = "; ".join(dict.fromkeys(failures)) or "no candidates found"
    raise ImportError(
        "BCC Python bindings are unavailable; tried modules 'bcc' and "
        f"'bpfcc' ({detail})"
    )


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
    bcc_module = None
    try:
        bcc = _ensure_bcc_importable()
        bcc_file = getattr(bcc, "__file__", None)
        bcc_module = getattr(bcc, "__name__", None)
    except ImportError:
        pass
    return {
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "python": sys.executable,
        "bcc_module": bcc_module,
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
            "eBPF clause telemetry requires root or the kernel capabilities "
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
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        architecture_candidates = (
            f"__arm64_sys_{name}",
            f"__x64_sys_{name}",
            f"__ia32_sys_{name}",
        )
    else:
        architecture_candidates = (
            f"__x64_sys_{name}",
            f"__ia32_sys_{name}",
            f"__arm64_sys_{name}",
        )
    candidates.extend((*architecture_candidates, f"sys_{name}"))
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
#include <linux/nsproxy.h>
#include <linux/percpu_counter.h>
#include <linux/pid_namespace.h>
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

/* mm_struct::rss_stat exists in two layouts in supported kernels: an
 * atomic_long_t array wrapper, or an array of struct percpu_counter.  Do not
 * infer the layout from LINUX_VERSION_CODE: distribution kernels routinely
 * backport this change without changing their advertised base version.
 *
 * Instead, let clang inspect the type supplied by the *active kernel headers*.
 * Both candidate member expressions go through explicit casts, so they remain
 * syntactically valid on either layout; __builtin_choose_expr then selects the
 * matching address and value type at compile time.  The size/alignment check
 * recognizes the historical anonymous atomic wrapper and rejects incompatible
 * layouts instead of silently reading the wrong bytes.
 *
 * For the percpu layout, reading .count and clamping each member independently
 * matches the kernel's fast get_mm_rss()/percpu_counter_read_positive()
 * approximation.  It deliberately does not claim to include unbatched
 * per-CPU residuals. */
#define CLAWTUNE_RSS_STAT_IS_PERCPU                                         \
    __builtin_types_compatible_p(                                      \
        __typeof__(((struct mm_struct *)0)->rss_stat),                  \
        struct percpu_counter[NR_MM_COUNTERS])
#define CLAWTUNE_RSS_STAT_IS_ATOMIC                                        \
    (!CLAWTUNE_RSS_STAT_IS_PERCPU &&                                       \
     sizeof(((struct mm_struct *)0)->rss_stat) ==                       \
         sizeof(atomic_long_t[NR_MM_COUNTERS]) &&                       \
     __alignof__(((struct mm_struct *)0)->rss_stat) ==                  \
         __alignof__(atomic_long_t[NR_MM_COUNTERS]))
typedef char claw_rss_stat_layout_must_be_supported[
    (CLAWTUNE_RSS_STAT_IS_PERCPU || CLAWTUNE_RSS_STAT_IS_ATOMIC) ? 1 : -1
];

#define CLAWTUNE_RSS_PERCPU_COUNTER_ADDR(mm, index)                         \
    (&((struct percpu_counter *)&((mm)->rss_stat))[(index)].count)
#define CLAWTUNE_RSS_ATOMIC_COUNTER_ADDR(mm, index)                         \
    (&((atomic_long_t *)&((mm)->rss_stat))[(index)].counter)
#define CLAWTUNE_RSS_COUNTER_ADDR(mm, index)                                \
    __builtin_choose_expr(                                             \
        CLAWTUNE_RSS_STAT_IS_PERCPU,                                       \
        CLAWTUNE_RSS_PERCPU_COUNTER_ADDR((mm), (index)),                    \
        CLAWTUNE_RSS_ATOMIC_COUNTER_ADDR((mm), (index)))
#define CLAWTUNE_RSS_COUNTER_BACKEND (CLAWTUNE_RSS_STAT_IS_PERCPU ? 2 : 1)
typedef __typeof__(*CLAWTUNE_RSS_COUNTER_ADDR((struct mm_struct *)0, 0))
    claw_rss_counter_t;

struct event_t {
    u64 timestamp_ns;
    u64 cgroup_id;
    u64 pid_namespace_inode;
    u64 exec_seq;
    u64 cpu_ns;         /* per-task cumulative utime+stime at sample time */
    u64 rss_pages;      /* fast CURRENT rss = file+anon+shmem (not hiwater) */
    u64 mm_ptr;         /* address-space identity for dedup */
    u64 hiwater_pages;  /* raw lifetime hiwater (exit only), kept separate */
    u64 io_read_bytes;  /* task->ioac.read_bytes */
    u64 io_write_bytes; /* task->ioac.write_bytes */
    u64 io_cancelled_write_bytes; /* task->ioac.cancelled_write_bytes */
    u32 type;
    u32 rss_counter_backend; /* 1=atomic layout, 2=percpu global approximation */
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
BPF_HASH(allowed_cgroups, u64, u8, 4096);
BPF_HASH(allowed_pid_namespaces, u64, u8, 256);
BPF_ARRAY(ringbuf_reserve_failures, u64, 1);
BPF_ARRAY(argv_read_failures, u64, 1);
BPF_ARRAY(argv_boundary_read_failures, u64, 1);
BPF_ARRAY(perf_sample_count, u64, 1);
BPF_ARRAY(kprobe_total_hits, u64, 1);
BPF_ARRAY(next_exec_sequence, u64, 1);
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

static u64 current_pid_namespace_inode(struct task_struct *task);

static int wanted(void) {
    u32 zero = 0;
    u64 *t = target_cgroup.lookup(&zero);
    u64 current_cgroup_id = bpf_get_current_cgroup_id();
    int matched = t && *t && *t == current_cgroup_id;
    if (!matched && allowed_cgroups.lookup(&current_cgroup_id) != 0) matched = 1;
    if (!matched) {
        struct task_struct *task = (struct task_struct *)bpf_get_current_task();
        u64 pid_ns = current_pid_namespace_inode(task);
        matched = pid_ns && allowed_pid_namespaces.lookup(&pid_ns) != 0;
    }
    if (matched) {
        u64 *counter = kprobe_total_hits.lookup(&zero);
        if (counter) __sync_fetch_and_add(counter, 1);
    }
    return matched;
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

static u64 current_pid_namespace_inode(struct task_struct *task) {
    struct nsproxy *nsproxy = 0;
    struct pid_namespace *pid_ns = 0;
    u32 inum = 0;
    bpf_probe_read_kernel(&nsproxy, sizeof(nsproxy), &task->nsproxy);
    if (!nsproxy) return 0;
    bpf_probe_read_kernel(&pid_ns, sizeof(pid_ns), &nsproxy->pid_ns_for_children);
    if (!pid_ns) return 0;
    bpf_probe_read_kernel(&inum, sizeof(inum), &pid_ns->ns.inum);
    return (u64)inum;
}

static void fill_identity(struct event_t *e, struct task_struct *task) {
    e->cgroup_id = bpf_get_current_cgroup_id();
    e->pid_namespace_inode = current_pid_namespace_inode(task);
}

static u64 current_rss_pages(struct task_struct *task, u64 *mm_out) {
    struct mm_struct *mm = 0;
    bpf_probe_read_kernel(&mm, sizeof(mm), &task->mm);
    *mm_out = (u64)mm;
    if (!mm) return 0;
    claw_rss_counter_t file = 0, anon = 0, shmem = 0;
    bpf_probe_read_kernel(
        &file, sizeof(file), CLAWTUNE_RSS_COUNTER_ADDR(mm, 0)
    );
    bpf_probe_read_kernel(
        &anon, sizeof(anon), CLAWTUNE_RSS_COUNTER_ADDR(mm, 1)
    );
    bpf_probe_read_kernel(
        &shmem, sizeof(shmem), CLAWTUNE_RSS_COUNTER_ADDR(mm, 3)
    );
    s64 total =
        (file > 0 ? (s64)file : 0) +
        (anon > 0 ? (s64)anon : 0) +
        (shmem > 0 ? (s64)shmem : 0);
    return (u64)total;
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
    e->rss_counter_backend = CLAWTUNE_RSS_COUNTER_BACKEND;
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
            fill_identity(e, (struct task_struct *)bpf_get_current_task());
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
            fill_identity(e, (struct task_struct *)bpf_get_current_task());
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
    fill_identity(e, (struct task_struct *)bpf_get_current_task());
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
    if (!wanted()) return 0;
    u64 *next = next_exec_sequence.lookup(&zero);
    if (!next) return 0;
    // Read-then-atomic-increment avoids __sync_fetch_and_add with return
    // value, which triggers "Invalid usage of the XADD return value" on
    // some clang/LLVM versions.  The exec sequence only needs to be
    // approximately monotonic per task (disambiguated by task_key), so a
    // narrow window between the read and the atomic add is harmless.
    u64 seq = *next;
    __sync_fetch_and_add(next, 1);
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
        fill_identity(e, (struct task_struct *)bpf_get_current_task());
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

/*
 * CONFIG_ARCH_HAS_SYSCALL_WRAPPER kernels (including x86_64 and arm64) pass
 * one pointer to the saved syscall register frame to __*_sys_execve*. The outer
 * kprobe frame therefore does not contain the syscall arguments directly.
 * Treating PT_REGS_PARM1(ctx) as filename saves the inner pt_regs pointer as a
 * user address and makes every later filename/argv read fail.
 */
static struct pt_regs *syscall_argument_regs(struct pt_regs *ctx) {
#ifdef CONFIG_ARCH_HAS_SYSCALL_WRAPPER
    return (struct pt_regs *)PT_REGS_PARM1(ctx);
#else
    return ctx;
#endif
}

int capture_sys_execve(struct pt_regs *ctx) {
    struct pt_regs *regs = syscall_argument_regs(ctx);
#ifdef CONFIG_ARCH_HAS_SYSCALL_WRAPPER
    u64 filename = 0;
    u64 argv = 0;
    bpf_probe_read_kernel(
        &filename, sizeof(filename), &PT_REGS_PARM1(regs)
    );
    bpf_probe_read_kernel(&argv, sizeof(argv), &PT_REGS_PARM2(regs));
    return capture_enter(
        (const char *)filename,
        (const char *const *)argv
    );
#else
    return capture_enter(
        (const char *)PT_REGS_PARM1(regs),
        (const char *const *)PT_REGS_PARM2(regs)
    );
#endif
}

int capture_sys_execveat(struct pt_regs *ctx) {
    struct pt_regs *regs = syscall_argument_regs(ctx);
#ifdef CONFIG_ARCH_HAS_SYSCALL_WRAPPER
    u64 filename = 0;
    u64 argv = 0;
    bpf_probe_read_kernel(
        &filename, sizeof(filename), &PT_REGS_PARM2(regs)
    );
    bpf_probe_read_kernel(&argv, sizeof(argv), &PT_REGS_PARM3(regs));
    return capture_enter(
        (const char *)filename,
        (const char *const *)argv
    );
#else
    return capture_enter(
        (const char *)PT_REGS_PARM2(regs),
        (const char *const *)PT_REGS_PARM3(regs)
    );
#endif
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
            fill_identity(e, (struct task_struct *)bpf_get_current_task());
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
    fill_identity(e, (struct task_struct *)bpf_get_current_task());
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
    fill_identity(e, task);
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
    fill_identity(e, task);
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
    """Return the effective CPU quota, or host capacity when unavailable.

    Cgroup v2 does not create ``cpu.max`` in a subtree whose parent has not
    enabled the CPU controller. That is a valid host configuration and does
    not prevent the eBPF probes from collecting per-process CPU time.
    """

    try:
        raw = (cgroup / "cpu.max").read_text(encoding="utf-8").split()
        if len(raw) != 2 or raw[0] == "max":
            return float(_NPROC)
        quota = int(raw[0])
        period = int(raw[1])
        if quota <= 0 or period <= 0:
            return float(_NPROC)
        return float(quota / period)
    except (OSError, ValueError):
        return float(_NPROC)


class RssOracle(threading.Thread):
    """Independent live-RSS reference: sum of VmRSS over distinct cgroup PIDs.

    A userspace poller (analysis-only reference, never a prediction input): at
    each tick it sums current VmRSS across distinct tgids in the cgroup and
    keeps the max. VmRSS is itself a fast kernel estimate, so this is a
    cross-check rather than exact page-table ground truth.
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


def _runtime_response_exit_code(
    replay_response: Mapping[str, Any] | None,
) -> int | None:
    """Read either SDK or scheduler naming for a completed process status."""

    if replay_response is None:
        return None
    raw = replay_response.get("returncode")
    if raw is None:
        raw = replay_response.get("exit_code")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


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
    """Return strict command-lookup evidence, else no evidence.

    Offline replay uses independent source and replay results.  A live
    managed-wrapper call has only one authoritative execution result; requiring
    a synthetic source action there made ordinary ``missing | tail`` pipelines
    permanently invalid even though the anchored shell diagnostic and parser
    semantics were unambiguous.
    """

    from tool_resource.clause_bridge import (
        ShellCommandLookupFailure,
        shell_lookup_exit_semantics,
    )

    if not replay_tool_call_id or replay_exit_code not in {0, 127}:
        return None
    replay_channel = "raw_stderr" if replay_stderr else "tool_result"
    replay_match = _anchored_command_not_found(
        replay_stderr if replay_stderr else replay_result
    )
    if replay_match is None:
        return None
    source_match: tuple[str, str] | None = None
    source_exit_code = replay_exit_code
    evidence_mode = "live_execution"
    source_channel = "unavailable"
    if source_tool_call_id:
        if source_command != command:
            return None
        source_exit_code = _strict_exit_code(source_tool_result)
        if source_exit_code != replay_exit_code:
            return None
        source_match = _anchored_command_not_found(source_tool_result)
        if source_match is None or source_match[0] != replay_match[0]:
            return None
        evidence_mode = "source_replay"
        source_channel = "source_tool_result"
    exit_code_semantics = shell_lookup_exit_semantics(
        command,
        replay_match[0],
        replay_exit_code,
    )
    if exit_code_semantics is None:
        return None
    return ShellCommandLookupFailure(
        executable_head=replay_match[0],
        command=command,
        source_tool_call_id=source_tool_call_id,
        replay_tool_call_id=replay_tool_call_id,
        source_exit_code=source_exit_code,
        replay_exit_code=replay_exit_code,
        source_diagnostic=source_match[1] if source_match is not None else "",
        replay_diagnostic=replay_match[1],
        source_channel=source_channel,
        replay_channel=replay_channel,
        parser="anchored_shell_command_not_found_v1",
        exit_code_semantics=exit_code_semantics,
        evidence_mode=evidence_mode,
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
    cg = Path(f"/sys/fs/cgroup/clause_ebpf_{os.getpid()}_{tag}")
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

    bcc = _ensure_bcc_importable()
    BPF = bcc.BPF
    PerfSWConfig = bcc.PerfSWConfig
    PerfType = bcc.PerfType

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


def validate_clause_telemetry_smoke() -> dict[str, Any]:
    """Exercise the real exec/cgroup path and reject semantically empty BPF data."""

    marker = "clawtune-ebpf-preflight"
    raw = collect_case(
        f"printf {marker}",
        f"preflight_{os.getpid()}",
        marker=marker,
    )
    argv = [
        str(event.get("arg") or "")
        for event in raw.events
        if event.get("type") == "exec_arg"
    ]
    requested_paths = [
        str(event.get("arg") or "")
        for event in raw.events
        if event.get("type") == "exec_meta"
    ]
    exec_boundaries = sum(
        event.get("type") == "exec_boundary" for event in raw.events
    )
    errors: list[str] = []
    if raw.status != 0:
        errors.append(f"smoke command exited {raw.status}")
    if not raw.marker:
        errors.append("smoke stdout marker was not observed")
    if raw.loss_count:
        detail = ", ".join(
            f"{name}={count}" for name, count in raw.loss_counts.items() if count
        )
        errors.append(f"telemetry loss={raw.loss_count} ({detail})")
    if not any(argv):
        errors.append("no non-empty exec argv was captured")
    if not any(requested_paths):
        errors.append("no non-empty requested executable path was captured")
    if exec_boundaries < 1:
        errors.append("no successful exec boundary was captured")
    uncleared = {
        name: count for name, count in raw.lifecycle_map_entries.items() if count
    }
    if uncleared:
        errors.append(f"lifecycle maps were not drained: {uncleared}")
    if errors:
        raise RuntimeError("eBPF semantic smoke failed: " + "; ".join(errors))
    return {
        "ok": True,
        "event_count": len(raw.events),
        "exec_arg_count": len(argv),
        "exec_boundary_count": exec_boundaries,
        "requested_executable_count": len(requested_paths),
        "loss_counts": raw.loss_counts,
    }


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
            trusted_root_setup = (
                seq == SENTINEL
                and pid == entry_pid
                and bool(clause_by_pid.get(pid))
                and ts < min(clause.t_exec_ns for clause in clause_by_pid[pid])
            )
            gaps.append(
                {
                    **e,
                    "reason": (
                        "trusted_root_pre_exec_structural_setup"
                        if trusted_root_setup
                        else (
                            "sentinel_exec_seq_without_active_exec_image_or_owned_ancestor"
                            if seq == SENTINEL
                            else "exec_seq_without_matching_exec_image_or_owned_ancestor"
                        )
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
    backend_names = {
        1: "atomic_long_fast_read",
        2: "percpu_counter_global_approximation",
    }
    backend_ids = sorted(
        {
            int(sample.get("rss_counter_backend", 0) or 0)
            for sample in samples
            if int(sample.get("rss_counter_backend", 0) or 0) > 0
        }
    )
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
        "counter_backends": [
            backend_names.get(value, f"unknown:{value}") for value in backend_ids
        ],
        "counter_exact": False,
        "read_semantics": "linux_get_mm_rss_fast_counter_approximation",
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
    """eBPF data cannot be used without hiding a coverage or lifecycle gap."""

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
    trusted_root_pid: int | None = None,
) -> tuple[int, set[int], dict[str, Any]]:
    """Identify one command tree rooted in observed or launcher-bound identity."""

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
        while current != trusted_root_pid and (
            current in fork_parent
            or (fork_records is not None and current in fork_records)
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
            if trusted_root_pid is not None and current == trusted_root_pid:
                entry_by_root[pid] = trusted_root_pid
            elif chain:
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
        "identity_anchor": (
            {
                "kind": "launcher_started",
                "host_pid": trusted_root_pid,
            }
            if trusted_root_pid is not None
            else {"kind": "observed_fork_ancestry"}
        ),
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


def _isolate_call_events(
    events: list[dict[str, Any]],
    command: str,
    *,
    trusted_root_pid: int | None = None,
    allow_trusted_root_pid_remap: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one exact launcher command tree from a shared-cgroup window.

    Concurrent exec calls can have independent collectors targeting the same
    sandbox cgroup.  Their monotonic windows may overlap, so timestamp and
    cgroup alone do not delimit a call.  The launcher provides a stronger
    boundary: the root shell argv contains the exact registered command.
    Ambiguous or absent matches remain unfiltered and therefore fail closed in
    ``_command_tree_provenance``.
    """

    clauses, fork_parent = _clauses_and_lineage(events)
    exec_pids = {clause.host_pid for clause in clauses}
    root_pids: set[int] = set()
    for pid in exec_pids:
        current = fork_parent.get(pid, 0)
        seen = {pid}
        has_exec_ancestor = False
        while current and current not in seen:
            if current in exec_pids:
                has_exec_ancestor = True
                break
            seen.add(current)
            current = fork_parent.get(current, 0)
        if not has_exec_ancestor:
            root_pids.add(pid)

    candidates = {
        clause.host_pid
        for clause in clauses
        if clause.host_pid in root_pids
        and len(clause.argv) >= 3
        and Path(clause.argv[0]).name in {"sh", "dash", "bash"}
        and clause.argv[1] in {"-c", "-lc"}
        and clause.argv[2] == command
    }
    effective_trusted_root_pid = trusted_root_pid
    trusted_root_remapped = False
    if trusted_root_pid is not None:
        observed_pids = {
            int(event.get(field, 0) or 0)
            for event in events
            for field in ("host_pid", "child_host_pid")
        }
        if (
            allow_trusted_root_pid_remap
            and trusted_root_pid not in observed_pids
            and len(candidates) == 1
        ):
            # A sidecar inside the sandbox PID namespace sees the launcher's
            # namespace-local PID, while bpf_get_current_pid_tgid() reports
            # the init-namespace PID.  The pre-exec gate guarantees that the
            # first exact ``/bin/sh -c <registered command>`` image in the
            # exclusive per-execution cgroup is that same trusted process.
            # Remap only on one exact root match; ambiguity still fails closed.
            effective_trusted_root_pid = next(iter(candidates))
            trusted_root_remapped = True

    if effective_trusted_root_pid is not None:
        selected_pids = {effective_trusted_root_pid}
        changed = True
        while changed:
            changed = False
            for child, parent in fork_parent.items():
                if parent in selected_pids and child not in selected_pids:
                    selected_pids.add(child)
                    changed = True
        selected_events = [
            event
            for event in events
            if int(event.get("host_pid", 0)) in selected_pids
            or (
                event.get("type") == "fork"
                and int(event.get("child_host_pid", 0)) in selected_pids
            )
        ]
        provenance = {
            "mode": (
                "trusted_execution_root_pid_namespace_remap"
                if trusted_root_remapped
                else "trusted_execution_root"
            ),
            "trusted_root_pid": effective_trusted_root_pid,
            "selected_pid_count": len(selected_pids),
            "raw_window_event_count": len(events),
            "selected_event_count": len(selected_events),
        }
        if trusted_root_remapped:
            provenance["claimed_trusted_root_pid"] = trusted_root_pid
            provenance["remap_evidence"] = "exact_registered_root_shell"
        return selected_events, provenance

    selection = {
        "mode": "not_needed" if len(root_pids) <= 1 else "unresolved",
        "window_root_pids": sorted(root_pids),
        "matching_root_pids": sorted(candidates),
        "raw_window_event_count": len(events),
        "selected_event_count": len(events),
    }
    if len(root_pids) <= 1:
        return events, selection
    if len(candidates) != 1:
        return events, selection

    selected_root = next(iter(candidates))
    selected_pids = {selected_root}
    changed = True
    while changed:
        changed = False
        for child, parent in fork_parent.items():
            if parent in selected_pids and child not in selected_pids:
                selected_pids.add(child)
                changed = True

    selected_events = [
        event
        for event in events
        if int(event.get("host_pid", 0)) in selected_pids
        or (
            event.get("type") == "fork"
            and int(event.get("child_host_pid", 0)) in selected_pids
        )
    ]
    selection.update(
        {
            "mode": "exact_launcher_command",
            "selected_root_pid": selected_root,
            "selected_pid_count": len(selected_pids),
            "selected_event_count": len(selected_events),
        }
    )
    return selected_events, selection


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


def _is_root_cgroup_str(cgroup_path: str) -> bool:
    """Return True when *cgroup_path* is the host cgroup v2 root.

    The root cgroup (``/sys/fs/cgroup``) is never the correct eBPF target
    because every process belongs to a leaf cgroup whose inode differs from
    the root.  Using it would cause the BPF ``wanted()`` filter to silently
    match zero events.
    """
    normalized = cgroup_path.replace("\\", "/").rstrip("/")
    return normalized in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}


def _container_cgroup(
    container_id: str,
    container_executable: str,
) -> tuple[Path, int]:
    init_pid = _container_init_pid(container_id, container_executable)
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


def _container_init_pid(container_id: str, container_executable: str) -> int:
    """Resolve the host PID without depending exclusively on Docker CLI API age.

    The task container has the daemon socket mounted for the observer.  Debian
    task images can provide a Docker CLI older than the host daemon's minimum
    API version, even though the mounted socket remains usable.  Preserve the
    CLI as the normal route and use the socket only as a local fallback.
    """
    try:
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
    except OSError as cli_error:
        cli_detail = str(cli_error)
    else:
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
        cli_detail = result.stderr.strip() or result.stdout.strip() or "docker inspect failed"
    try:
        return _container_init_pid_from_socket(container_id)
    except (OSError, ValueError, json.JSONDecodeError) as socket_error:
        raise RuntimeError(
            "cannot resolve container host pid: "
            f"docker CLI: {cli_detail}; Docker socket fallback: {socket_error}"
        ) from socket_error


def _container_init_pid_from_socket(container_id: str) -> int:
    socket_path = os.getenv("CLAWTUNE_DOCKER_SOCKET", "/var/run/docker.sock")
    if not os.path.exists(socket_path):
        raise OSError(f"Docker socket is unavailable: {socket_path}")
    connection = _DockerUnixHTTPConnection(socket_path, timeout=1.0)
    try:
        connection.request("GET", f"/containers/{quote(container_id, safe='')}/json")
        response = connection.getresponse()
        payload = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise OSError(f"Docker socket inspect returned HTTP {response.status}")
    document = json.loads(payload.decode("utf-8"))
    state = document.get("State") if isinstance(document, dict) else None
    pid = state.get("Pid") if isinstance(state, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("Docker socket inspect returned no running container PID")
    return pid


class _DockerUnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connected.settimeout(self.timeout)
        connected.connect(self.socket_path)
        self.sock = connected


def _pid_namespace_inode_for_pid(pid: int) -> int | None:
    try:
        target = os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None
    if target.startswith("pid:[") and target.endswith("]"):
        try:
            return int(target[5:-1])
        except ValueError:
            return None
    return None


def _discover_descendant_cgroup_inodes(cgroup: Path) -> set[int]:
    """Return inode numbers for one cgroup and its descendants only."""

    inodes: set[int] = set()
    try:
        inodes.add(cgroup.stat().st_ino)
    except OSError:
        pass
    try:
        for entry in cgroup.rglob("*"):
            if entry.is_dir():
                try:
                    inodes.add(entry.stat().st_ino)
                except OSError:
                    pass
    except OSError:
        pass
    return inodes


def _discover_leaf_cgroup_inodes(cgroup: Path) -> set[int]:
    """Return inode numbers for a container cgroup and related cgroups.

    On Docker / cgroup v2 with systemd, ``docker exec`` often creates
    transient scopes that may live in a different part of the cgroup
    tree than the container's root scope.  We use three strategies:

    1. Add the container root cgroup inode.
    2. Walk all descendant cgroups.
    3. Extract the container-id prefix from the cgroup directory name
       and scan broadly for any cgroup directory (not just in the parent)
       whose name contains that prefix.  This catches sibling scopes,
       systemd transient scopes, and scopes in other slices.

    The container-id is typically a 64-char hex string embedded in the
    directory name after ``docker-`` or ``cri-containerd-``.
    """
    inodes = _discover_descendant_cgroup_inodes(cgroup)

    # Broad scan: find ALL cgroup directories whose name contains the
    # container-id substring (at least 12 hex chars).  This catches
    # sibling scopes, systemd transient scopes in different slices, and
    # any other container-related cgroups that are not descendants of
    # the root scope.
    _add_container_cgroup_inodes_broad(cgroup, inodes)

    return inodes


def _isolated_pid_namespace_inode(init_pid: int) -> int | None:
    """Return a workload PID namespace only when it differs from the sidecar.

    A direct-host process (and a container using ``--pid=host``) shares the
    sidecar's PID namespace. Treating that namespace as an attribution filter
    would select every host process and expand cgroup discovery across the
    machine.
    """

    if init_pid <= 0:
        return None
    workload_inode = _pid_namespace_inode_for_pid(init_pid)
    sidecar_inode = _pid_namespace_inode_for_pid(os.getpid())
    if workload_inode is None or workload_inode == sidecar_inode:
        return None
    return workload_inode


def _scope_identity_inodes(
    cgroup: Path,
    init_pid: int,
    container_id: str | None,
) -> tuple[set[int], set[int]]:
    """Build cgroup/PID-namespace filters without widening host scopes."""

    if not container_id:
        # A direct-host command may remain in a shared SSH/systemd session
        # cgroup when the kernel refuses an exclusive child cgroup. Its
        # authenticated root PID and fork lineage are the identity boundary;
        # admitting the cgroup inode here would also admit unrelated shells.
        return set(), set()
    cgroup_inodes = _discover_leaf_cgroup_inodes(cgroup)
    pid_namespace_inode = _isolated_pid_namespace_inode(init_pid)
    if pid_namespace_inode is not None:
        cgroup_inodes |= _discover_cgroup_inodes_from_proc(init_pid)
    return (
        cgroup_inodes,
        {pid_namespace_inode} if pid_namespace_inode is not None else set(),
    )


def _discover_cgroup_inodes_from_proc(init_pid: int) -> set[int]:
    """Discover container cgroup inodes by scanning /proc for processes.

    This is more reliable than directory scanning because it finds the
    ACTUAL cgroups where container processes run, regardless of naming
    conventions or cgroup tree layout.

    Walks all PIDs in /proc that share the same PID namespace as
    *init_pid*, reads their cgroup v2 path from /proc/<pid>/cgroup, and
    collects the inode of each unique cgroup directory.
    """
    if init_pid <= 0:
        return set()
    # Get the PID namespace of the container init process.
    try:
        init_ns = os.readlink(f"/proc/{init_pid}/ns/pid")
    except OSError:
        return set()
    inodes: set[int] = set()
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == init_pid:
                continue
            try:
                ns = os.readlink(entry / "ns" / "pid")
            except OSError:
                continue
            if ns != init_ns:
                continue
            # This process is in the same PID namespace → read its cgroup.
            try:
                cgroup_text = (entry / "cgroup").read_text(encoding="utf-8")
            except OSError:
                continue
            for line in cgroup_text.splitlines():
                if not line.startswith("0::"):
                    continue
                cgroup_rel = line[3:]
                cgroup_path = Path("/sys/fs/cgroup") / cgroup_rel.lstrip("/")
                try:
                    inodes.add(cgroup_path.stat().st_ino)
                except OSError:
                    pass
                break  # Only need the unified (v2) hierarchy line.
    except OSError:
        pass
    return inodes


def _add_container_cgroup_inodes_broad(
    cgroup: Path,
    inodes: set[int],
) -> None:
    """Scan the cgroup tree for directories related to the same container.

    Extracts the longest hex substring from *cgroup*'s directory name and
    searches all cgroup directories for names containing that substring.
    Limited to 12+ hex chars to avoid false positives.
    """
    name = cgroup.name
    # Extract the longest hex substring from the directory name.
    # Docker scope names: docker-<64hex>.scope or docker-<64hex>.scope:<uuid>
    # containerd: cri-containerd-<64hex>.scope
    hex_substring = _longest_hex_substring(name)
    if hex_substring is None or len(hex_substring) < 12:
        # Fall back to prefix-based scan in the parent directory.
        _add_sibling_cgroup_inodes(cgroup, inodes)
        return

    # Search from the cgroup root (max depth 4) and also from the
    # container's parent directory.  Most systemd/Docker layouts put
    # scopes within 3 levels of /sys/fs/cgroup.
    cgroup_root = Path("/sys/fs/cgroup")
    _scan_for_container_cgroups(cgroup_root, hex_substring, inodes, depth=3)
    # Also search from the parent for sibling scopes (common pattern).
    parent = cgroup.parent
    if parent is not None and parent != cgroup_root:
        _scan_for_container_cgroups(parent, hex_substring, inodes, depth=2)


def _longest_hex_substring(name: str) -> str | None:
    """Return the longest contiguous hex substring in *name*, or None."""
    best = ""
    current = ""
    for ch in name:
        if ch in "0123456789abcdefABCDEF":
            current += ch
        else:
            if len(current) > len(best):
                best = current
            current = ""
    if len(current) > len(best):
        best = current
    return best.lower() if len(best) >= 12 else None


def _scan_for_container_cgroups(
    root: Path,
    hex_substring: str,
    inodes: set[int],
    *,
    depth: int = 2,
) -> None:
    """Walk *root* up to *depth* levels, adding inodes of matching dirs."""
    if depth <= 0:
        return
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if hex_substring in entry.name.lower():
                try:
                    inodes.add(entry.stat().st_ino)
                except OSError:
                    pass
            # Recurse one level for nested scopes (e.g. systemd transient).
            if depth > 1:
                _scan_for_container_cgroups(entry, hex_substring, inodes, depth=depth - 1)
    except OSError:
        pass


def _add_sibling_cgroup_inodes(
    cgroup: Path,
    inodes: set[int],
) -> None:
    """Add inodes of sibling cgroups whose name starts with the same prefix.

    Docker scope names follow the pattern
    ``docker-<container_id>[(.<suffix>)].scope``.  Systemd transient scopes
    for ``docker exec`` are siblings of the container scope and share the
    container-id prefix.  For containerd/cri-o the prefix may differ
    (e.g. ``cri-containerd-<id>.scope``); we extract any common hex prefix.
    """
    parent = cgroup.parent
    if parent is None:
        return
    name = cgroup.name
    # Try to find a hex-based prefix of at least 12 chars to match
    # container-id substrings in sibling directory names.
    hex_substring = _longest_hex_substring(name)
    if hex_substring is not None and len(hex_substring) >= 12:
        try:
            for entry in parent.iterdir():
                if not entry.is_dir():
                    continue
                if entry == cgroup:
                    continue
                if hex_substring in entry.name.lower():
                    try:
                        inodes.add(entry.stat().st_ino)
                    except OSError:
                        pass
        except OSError:
            pass
        return

    # Legacy: match on "docker-" prefix (at least 32 hex chars).
    if not name.startswith("docker-"):
        return
    try:
        prefix = name[: 7 + 32]  # "docker-" + 32 hex chars
    except IndexError:
        return
    try:
        for entry in parent.iterdir():
            if not entry.is_dir():
                continue
            if entry == cgroup:
                continue
            if entry.name.startswith(prefix):
                try:
                    inodes.add(entry.stat().st_ino)
                except OSError:
                    pass
    except OSError:
        pass


def _container_pid_set(
    events: list[dict[str, Any]],
    init_pid: int,
    *,
    cgroup_inodes: set[int] | None = None,
    pid_namespace_inodes: set[int] | None = None,
) -> set[int]:
    """Return the set of host PIDs belonging to the container rooted at *init_pid*.

    Discovery is done in two modes:

    *When *cgroup_inodes* is provided (non-empty)* — PIDs are discovered
    via cgroup membership: any ``host_pid`` or ``child_host_pid`` whose
    event ``cgroup_id`` matches a known container cgroup inode is added.
    This correctly handles Docker exec processes whose ``real_parent``
    points outside the container (e.g. containerd-shim), breaking the
    fork/exec lineage chain.  Lineage traversal is skipped because
    cgroup membership is authoritative.

    *When *cgroup_inodes* is ``None`` or empty* — the legacy fork/exec
    lineage pass runs: fork events grow the tree, and exec events whose
    ``parent_host_pid`` is already known add their ``host_pid``.  This
    handles long-lived processes that were forked before the collector
    started.
    """
    if init_pid <= 0:
        return set()
    pids: set[int] = {init_pid}

    # --- namespace/cgroup-based discovery (authoritative when available) ---
    if cgroup_inodes or pid_namespace_inodes:
        for event in events:
            cg = event.get("cgroup_id", 0)
            pid_ns = event.get("pid_namespace_inode", 0)
            if (
                (not cgroup_inodes or cg not in cgroup_inodes)
                and (not pid_namespace_inodes or pid_ns not in pid_namespace_inodes)
            ):
                continue
            host = event.get("host_pid", 0)
            child = event.get("child_host_pid", 0)
            if host > 0:
                pids.add(host)
            if child > 0:
                pids.add(child)
        return pids

    # --- lineage pass (legacy, no cgroup info available) ------------------
    changed = True
    while changed:
        changed = False
        for event in events:
            host = event.get("host_pid", 0)
            parent = event.get("parent_host_pid", 0)
            child = event.get("child_host_pid", 0)
            if host > 0 and host not in pids:
                if parent in pids:
                    pids.add(host)
                    changed = True
                elif event.get("type") == "fork" and host in pids:
                    if child > 0 and child not in pids:
                        pids.add(child)
                        changed = True
            elif host > 0 and event.get("type") == "fork" and host in pids:
                if child > 0 and child not in pids:
                    pids.add(child)
                    changed = True
    return pids


def _observed_container_cgroup_ids(
    events: Sequence[Mapping[str, Any]],
    container_pids: set[int],
    pid_namespace_inodes: set[int],
) -> set[int]:
    """Return cgroups supported by container identity, never window timing alone.

    eBPF attaches system-wide probes. An exec boundary merely occurring
    during a tool window is therefore not evidence that its cgroup belongs to
    the sandbox. Accept only events tied to an already authenticated PID or
    the container PID namespace; the launcher-bound trusted root is added to
    ``container_pids`` by the caller before this function runs.
    """

    discovered: set[int] = set()
    for event in events:
        cgroup_id = int(event.get("cgroup_id", 0) or 0)
        if cgroup_id <= 0:
            continue
        host_pid = int(event.get("host_pid", 0) or 0)
        pid_namespace_inode = int(event.get("pid_namespace_inode", 0) or 0)
        if host_pid in container_pids or (
            pid_namespace_inodes
            and pid_namespace_inode in pid_namespace_inodes
        ):
            discovered.add(cgroup_id)
    return discovered


def _event_row(table: Any, data: int) -> dict[str, Any]:
    event = table.event(data)
    row = {
        "type": TYPE_NAMES[int(event.type)],
        "ts_ns": int(event.timestamp_ns),
        "cgroup_id": int(event.cgroup_id),
        "pid_namespace_inode": int(event.pid_namespace_inode),
        "exec_seq": int(event.exec_seq),
        "cpu_ns": int(event.cpu_ns),
        "rss_pages": int(event.rss_pages),
        "rss_counter_backend": int(event.rss_counter_backend),
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


# Cross-instance cache of discovered cgroup inodes keyed by container_id.
# Each ClauseTelemetryCollector instance starts fresh, but Docker exec
# transient cgroups discovered by one instance must be visible to the
# next so that _container_pid_set can match exec events immediately.
_cgroup_inodes_cache: dict[str, set[int]] = {}
_cgroup_inodes_cache_lock = threading.Lock()
_CGROUP_INODES_CACHE_MAX_CONTAINERS = 1_024


def _cached_cgroup_inodes(container_id: str) -> set[int]:
    with _cgroup_inodes_cache_lock:
        return set(_cgroup_inodes_cache.get(container_id, ()))


def _cache_cgroup_inodes(container_id: str, cgroup_ids: set[int]) -> None:
    with _cgroup_inodes_cache_lock:
        _cgroup_inodes_cache.setdefault(container_id, set()).update(cgroup_ids)
        while len(_cgroup_inodes_cache) > _CGROUP_INODES_CACHE_MAX_CONTAINERS:
            oldest = next(iter(_cgroup_inodes_cache))
            if oldest == container_id and len(_cgroup_inodes_cache) > 1:
                oldest = next(
                    key for key in _cgroup_inodes_cache if key != container_id
                )
            _cgroup_inodes_cache.pop(oldest, None)


class _SharedEventBuffer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.active = True

    def append(self, event: dict[str, Any]) -> None:
        with self.lock:
            if self.active:
                self.events.append(event)

    def close(self) -> None:
        with self.lock:
            self.active = False
            self.events.clear()


class _SharedBpfSource:
    """One process-wide BPF attachment with dynamically leased scopes.

    Collectors keep independent delimiters and artifacts.  Only the expensive
    kernel probes, perf event, ring buffer, event list, and poller are shared,
    so their count is O(1) as Gateway/Runtime concurrency grows.
    """

    def __init__(self, BPF: Any, PerfType: Any, PerfSWConfig: Any) -> None:
        self._scope_lock = threading.RLock()
        # Retained as a compatibility/diagnostic aggregate; routed events live
        # only in per-lease buffers so one long task cannot pin every other
        # runtime's history.
        self.events_lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self._leases: dict[
            int,
            tuple[set[int], set[int], _SharedEventBuffer],
        ] = {}
        self._routes: dict[tuple[str, int], tuple[_SharedEventBuffer, ...]] = {}
        self._cgroup_refs: dict[int, int] = {}
        self._pid_namespace_refs: dict[int, int] = {}
        self._next_lease = 1
        self._stop_poll = threading.Event()
        self.poll_error: BaseException | None = None
        self.closed = False
        self.generation = time.monotonic_ns()
        self._perf_type = PerfType
        self._perf_config = PerfSWConfig
        self.bpf = BPF(text=BPF_PROGRAM)
        try:
            _attach_first_kprobe(
                self.bpf,
                BPF,
                syscall="execve",
                fn_name="capture_sys_execve",
            )
            _attach_first_kprobe(
                self.bpf,
                BPF,
                syscall="execveat",
                fn_name="capture_sys_execveat",
            )
            _attach_first_kprobe(
                self.bpf,
                BPF,
                syscall="execve",
                fn_name="capture_sys_execve_return",
                retprobe=True,
            )
            _attach_first_kprobe(
                self.bpf,
                BPF,
                syscall="execveat",
                fn_name="capture_sys_execveat_return",
                retprobe=True,
            )
            self.bpf.attach_kprobe(
                event="bprm_execve",
                fn_name="capture_bprm_argv",
            )
            self.bpf.attach_kprobe(
                event="bprm_change_interp",
                fn_name="capture_interp_change",
            )
            self._table = self.bpf["events"]

            def receive(_ctx: int, data: int, _size: int) -> int:
                row = _event_row(self._table, data)
                self.route_event(row)
                return 0

            self._table.open_ring_buffer(receive)

            def poll() -> None:
                try:
                    while not self._stop_poll.is_set():
                        self.bpf.ring_buffer_poll(timeout=10)
                except BaseException as exc:
                    if not self._stop_poll.is_set():
                        self.poll_error = exc

            self._poller = threading.Thread(
                target=poll,
                name="clause-telemetry-shared-ring-poller",
                daemon=True,
            )
            self._poller.start()
            self.bpf.attach_perf_event(
                ev_type=PerfType.SOFTWARE,
                ev_config=PerfSWConfig.CPU_CLOCK,
                fn_name="on_cpu_clock",
                sample_period=SAMPLE_PERIOD_NS,
            )
        except BaseException:
            self._stop_poll.set()
            poller = getattr(self, "_poller", None)
            if poller is not None:
                poller.join(timeout=2)
            self.bpf.cleanup()
            self.closed = True
            raise

    def acquire(
        self,
        cgroup_ids: set[int],
        pid_namespace_ids: set[int],
    ) -> tuple[int, _SharedEventBuffer]:
        cgroups = {int(value) for value in cgroup_ids if int(value) > 0}
        namespaces = {
            int(value) for value in pid_namespace_ids if int(value) > 0
        }
        if not cgroups and not namespaces:
            raise RuntimeError("telemetry scope has no kernel identity")
        with self._scope_lock:
            if self.closed:
                raise RuntimeError("shared BPF source is closed")
            lease_id = self._next_lease
            self._next_lease += 1
            added_cgroups: list[int] = []
            added_namespaces: list[int] = []
            try:
                for cgroup_id in cgroups:
                    if self._cgroup_refs.get(cgroup_id, 0) == 0:
                        self.bpf["allowed_cgroups"][
                            ctypes.c_ulonglong(cgroup_id)
                        ] = ctypes.c_ubyte(1)
                        added_cgroups.append(cgroup_id)
                    self._cgroup_refs[cgroup_id] = (
                        self._cgroup_refs.get(cgroup_id, 0) + 1
                    )
                for namespace_id in namespaces:
                    if self._pid_namespace_refs.get(namespace_id, 0) == 0:
                        self.bpf["allowed_pid_namespaces"][
                            ctypes.c_ulonglong(namespace_id)
                        ] = ctypes.c_ubyte(1)
                        added_namespaces.append(namespace_id)
                    self._pid_namespace_refs[namespace_id] = (
                        self._pid_namespace_refs.get(namespace_id, 0) + 1
                    )
            except BaseException:
                for cgroup_id in cgroups:
                    count = self._cgroup_refs.get(cgroup_id, 0)
                    if count <= 1:
                        self._cgroup_refs.pop(cgroup_id, None)
                    else:
                        self._cgroup_refs[cgroup_id] = count - 1
                for namespace_id in namespaces:
                    count = self._pid_namespace_refs.get(namespace_id, 0)
                    if count <= 1:
                        self._pid_namespace_refs.pop(namespace_id, None)
                    else:
                        self._pid_namespace_refs[namespace_id] = count - 1
                for cgroup_id in added_cgroups:
                    self._delete_map_key("allowed_cgroups", cgroup_id)
                for namespace_id in added_namespaces:
                    self._delete_map_key("allowed_pid_namespaces", namespace_id)
                raise
            buffer = _SharedEventBuffer()
            self._leases[lease_id] = (cgroups, namespaces, buffer)
            self._rebuild_routes_locked()
            return lease_id, buffer

    def extend_cgroups(self, lease_id: int, cgroup_ids: set[int]) -> None:
        with self._scope_lock:
            scope = self._leases.get(lease_id)
            if scope is None:
                raise RuntimeError("shared BPF scope lease is not active")
            cgroups, namespaces, buffer = scope
            for cgroup_id in {int(value) for value in cgroup_ids if int(value) > 0}:
                if cgroup_id in cgroups:
                    continue
                if self._cgroup_refs.get(cgroup_id, 0) == 0:
                    self.bpf["allowed_cgroups"][
                        ctypes.c_ulonglong(cgroup_id)
                    ] = ctypes.c_ubyte(1)
                self._cgroup_refs[cgroup_id] = (
                    self._cgroup_refs.get(cgroup_id, 0) + 1
                )
                cgroups.add(cgroup_id)
            self._leases[lease_id] = (cgroups, namespaces, buffer)
            self._rebuild_routes_locked()

    def release(self, lease_id: int) -> None:
        with self._scope_lock:
            scope = self._leases.pop(lease_id, None)
            if scope is None:
                return
            cgroups, namespaces, buffer = scope
            for cgroup_id in cgroups:
                count = self._cgroup_refs.get(cgroup_id, 0)
                if count <= 1:
                    self._cgroup_refs.pop(cgroup_id, None)
                    self._delete_map_key("allowed_cgroups", cgroup_id)
                else:
                    self._cgroup_refs[cgroup_id] = count - 1
            for namespace_id in namespaces:
                count = self._pid_namespace_refs.get(namespace_id, 0)
                if count <= 1:
                    self._pid_namespace_refs.pop(namespace_id, None)
                    self._delete_map_key("allowed_pid_namespaces", namespace_id)
                else:
                    self._pid_namespace_refs[namespace_id] = count - 1
            self._rebuild_routes_locked()
            buffer.close()

    def route_event(self, event: dict[str, Any]) -> None:
        """Fan one kernel event only to leases whose scope can own it."""

        routes = self._routes
        targets = (
            routes.get(("cgroup", int(event.get("cgroup_id", 0))), ())
            + routes.get(
                (
                    "pidns",
                    int(event.get("pid_namespace_inode", 0)),
                ),
                (),
            )
        )
        seen: set[int] = set()
        for buffer in targets:
            identity = id(buffer)
            if identity in seen:
                continue
            seen.add(identity)
            buffer.append(event)

    def _rebuild_routes_locked(self) -> None:
        routes: dict[tuple[str, int], list[_SharedEventBuffer]] = {}
        for cgroups, namespaces, buffer in self._leases.values():
            for cgroup_id in cgroups:
                routes.setdefault(("cgroup", cgroup_id), []).append(buffer)
            for namespace_id in namespaces:
                routes.setdefault(("pidns", namespace_id), []).append(buffer)
        # Replacing the entire mapping makes callback reads lock-free under
        # CPython's object assignment semantics. Old buffers reject appends
        # after release, closing the release-vs-callback race safely.
        self._routes = {
            key: tuple(buffers)
            for key, buffers in routes.items()
        }

    def close(self) -> None:
        with self._scope_lock:
            if self.closed:
                return
            self.closed = True
            self._stop_poll.set()
        self._poller.join(timeout=2)
        if self._poller.is_alive():
            raise RuntimeError("shared ring poller did not stop")
        self.bpf.detach_perf_event(
            ev_type=self._perf_type.SOFTWARE,
            ev_config=self._perf_config.CPU_CLOCK,
        )
        self.bpf.cleanup()

    def _delete_map_key(self, table_name: str, value: int) -> None:
        table = self.bpf[table_name]
        key = ctypes.c_ulonglong(value)
        try:
            del table[key]
        except KeyError:
            pass


_shared_bpf_source: _SharedBpfSource | None = None
_shared_bpf_source_lock = threading.Lock()


def _acquire_shared_bpf_source(
    BPF: Any,
    PerfType: Any,
    PerfSWConfig: Any,
    *,
    cgroup_ids: set[int],
    pid_namespace_ids: set[int],
) -> tuple[_SharedBpfSource, int, _SharedEventBuffer]:
    global _shared_bpf_source
    with _shared_bpf_source_lock:
        if _shared_bpf_source is None or _shared_bpf_source.closed:
            try:
                _shared_bpf_source = _SharedBpfSource(
                    BPF,
                    PerfType,
                    PerfSWConfig,
                )
            except BaseException as exc:
                raise RuntimeError(
                    _bpf_setup_error_message("BPF collector attach failed", exc)
                ) from exc
        source = _shared_bpf_source
    lease_id, event_cursor = source.acquire(cgroup_ids, pid_namespace_ids)
    return source, lease_id, event_cursor


def _shutdown_shared_bpf_source() -> None:
    source = _shared_bpf_source
    if source is None:
        return
    try:
        source.close()
    except BaseException:
        pass


atexit.register(_shutdown_shared_bpf_source)


def _lifecycle_synchronized(method: Any) -> Any:
    @wraps(method)
    def synchronized(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)

    return synchronized


class ClauseTelemetryCollector:
    """One BPF program, armed once, delimiting serial exec tool calls."""

    def __init__(
        self,
        *,
        container_id: str | None,
        container_executable: str,
        repo: str,
        artifact_path: Path,
        cgroup_path: str | None = None,
        trusted_root_pid: int | None = None,
        source_actions: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        bcc = _ensure_bcc_importable()
        BPF = bcc.BPF
        PerfSWConfig = bcc.PerfSWConfig
        PerfType = bcc.PerfType
        self._lifecycle_lock = threading.RLock()

        if cgroup_path is not None and not _is_root_cgroup_str(cgroup_path):
            cgroup = Path(cgroup_path)
            if not cgroup.is_dir():
                raise RuntimeError(f"explicit cgroup path does not exist: {cgroup}")
            if trusted_root_pid is not None:
                # Direct host execution has no Docker id. The authenticated
                # launcher supplies a PID identity that the sidecar resolves
                # before constructing this collector.
                init_pid = trusted_root_pid
            elif container_id:
                # Resolve init_pid from Docker for informational purposes only;
                # entry_pid for analysis is derived from events, not init_pid.
                result = subprocess.run(
                    [container_executable, "inspect", container_id, "--format", "{{.State.Pid}}"],
                    capture_output=True, text=True, check=False,
                )
                init_pid = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
            else:
                raise RuntimeError(
                    "trusted_root_pid is required for host cgroup telemetry"
                )
        else:
            if cgroup_path is not None:
                # The host root cgroup (/sys/fs/cgroup) is never the correct
                # eBPF target.  Every process belongs to a leaf cgroup whose
                # inode differs from the root, so the BPF wanted() filter
                # would silently match zero events.  Fall through to
                # container-based discovery instead.
                pass
            if not container_id:
                raise RuntimeError(
                    "container_id is required when no explicit host cgroup is available"
                )
            cgroup, init_pid = _container_cgroup(container_id, container_executable)
        self.container_id = container_id
        self.cgroup = cgroup
        self.cgroup_id = cgroup.stat().st_ino
        # An observer running inside a container can see a namespace-local
        # launcher PID while bpf_get_current_pid_tgid() reports the enclosing
        # kernel namespace PID.  An explicit leaf cgroup is a sufficiently
        # narrow ownership boundary to permit the existing fail-closed remap:
        # it still requires one exact registered root-shell argv match and
        # rejects absent or ambiguous candidates.  This applies both to the
        # Docker sidecar topology and to an observer inside a Kata guest tool
        # container, where there is intentionally no Docker container id.
        self._trusted_root_pid_remap_allowed = (
            cgroup_path is not None and not _is_root_cgroup_str(cgroup_path)
        )
        # Docker may place exec processes in related transient scopes, while
        # direct-host collection already has an exact sidecar-verified cgroup.
        # Never run Docker's broad sibling/proc discovery for host work: in the
        # host PID namespace it would effectively select the entire machine.
        self.cgroup_inodes, self.pid_namespace_inodes = _scope_identity_inodes(
            cgroup,
            init_pid,
            container_id,
        )
        if container_id and not self.cgroup_inodes and self.cgroup_id:
            self.cgroup_inodes = {self.cgroup_id}
        if self.cgroup_id:
            self.cgroup_inodes.add(self.cgroup_id)
        # Seed from cross-instance cache: exec transient cgroups discovered
        # by a previous collector instance for the same container.
        if container_id:
            cached = _cached_cgroup_inodes(container_id)
            if cached:
                self.cgroup_inodes |= cached
        self.init_pid = init_pid
        self.trusted_root_pid: int | None = None
        if trusted_root_pid is not None:
            self.bind_trusted_root(trusted_root_pid)
        self.quota_cores = observed_quota_cores(cgroup)
        self.repo = repo
        self.artifact_path = artifact_path
        self._epoch_offset_s = time.time() - time.monotonic()
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
            (
                self._source,
                self._source_lease_id,
                self._event_buffer,
            ) = _acquire_shared_bpf_source(
                BPF,
                PerfType,
                PerfSWConfig,
                cgroup_ids=set(self.cgroup_inodes),
                pid_namespace_ids=set(self.pid_namespace_inodes),
            )
            self._bpf = self._source.bpf
            self._events = self._event_buffer.events
            self._events_lock = self._event_buffer.lock
            self._event_cursor = 0
            self._loss_baseline = _loss_counts(self._bpf)
            self._kprobe_hits_baseline = _counter(
                self._bpf,
                "kprobe_total_hits",
            )
        except BaseException as exc:
            source = getattr(self, "_source", None)
            lease_id = getattr(self, "_source_lease_id", None)
            if source is not None and lease_id is not None:
                source.release(lease_id)
            self._closed = True
            self._cleanup_status = "not_started"
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
        container_id: str | None = None,
        source_actions: Sequence[Mapping[str, Any]] = (),
    ) -> "ClauseTelemetryCollector":
        """Return a disabled collector when setup cannot arm BPF."""

        collector = object.__new__(cls)
        collector.container_id = container_id
        collector.cgroup = None
        collector.cgroup_id = 0
        collector.cgroup_inodes = set()
        collector.init_pid = 0
        collector.trusted_root_pid = None
        collector._trusted_root_pid_remap_allowed = False
        collector.pid_namespace_inodes = set()
        collector.quota_cores = 0.0
        collector.repo = repo
        collector.artifact_path = artifact_path
        collector._epoch_offset_s = time.time() - time.monotonic()
        collector._lifecycle_lock = threading.RLock()
        collector._events = []
        collector._events_lock = threading.Lock()
        collector._source = None
        collector._source_lease_id = None
        collector._event_cursor = 0
        collector._loss_baseline = dict.fromkeys(LOSS_COUNTER_NAMES, 0)
        collector._kprobe_hits_baseline = 0
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

    @_lifecycle_synchronized
    def bind_trusted_root(self, host_pid: int) -> None:
        if host_pid <= 0:
            raise ValueError("trusted root host PID must be positive")
        current = getattr(self, "trusted_root_pid", None)
        if current is not None and current != host_pid:
            raise ClauseTelemetryIntegrityError(
                f"trusted execution root changed from {current} to {host_pid}"
            )
        self.trusted_root_pid = host_pid

    def _disable(self, reason: str, *, tool_call_id: str | None = None) -> None:
        if self.state == "closed":
            return
        if self.state == "active":
            self.state = "disabled"
            self._disabled_reason = reason
            self._first_disabled_call = tool_call_id
        if reason not in self._integrity_errors:
            self._integrity_errors.append(reason)

    def _shared_poll_error(self) -> BaseException | None:
        source = getattr(self, "_source", None)
        return None if source is None else source.poll_error

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

    @_lifecycle_synchronized
    def begin_tool_call(self, tool_call_id: str, command: str) -> ToolCallToken:
        source_tool_call_id, source_command, source_tool_result = self._source_fields()
        poll_error = self._shared_poll_error()
        if self.state == "active" and poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(poll_error).__name__}: {poll_error}",
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

    @_lifecycle_synchronized
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
        poll_error = self._shared_poll_error()
        if poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(poll_error).__name__}: {poll_error}",
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
                # The process-wide source never prunes while any lease is
                # active.  A collector slices from its own acquire cursor, so
                # simultaneous identical commands cannot consume each
                # other's historical events.
                scope_events = list(self._events[self._event_cursor :])
            container_pids = _container_pid_set(
                scope_events,
                self.init_pid,
                cgroup_inodes=self.cgroup_inodes,
                pid_namespace_inodes=self.pid_namespace_inodes,
            )
            if self.trusted_root_pid is not None:
                container_pids.add(self.trusted_root_pid)
            dynamic_cgroups = (
                _observed_container_cgroup_ids(
                    scope_events,
                    container_pids,
                    self.pid_namespace_inodes,
                )
                if self.container_id
                else set()
            )
            new_cgroups = dynamic_cgroups - self.cgroup_inodes
            if new_cgroups:
                self._source.extend_cgroups(
                    self._source_lease_id,
                    new_cgroups,
                )
                self.cgroup_inodes |= new_cgroups
                if self.container_id:
                    _cache_cgroup_inodes(self.container_id, new_cgroups)
            runtime_identity_available = bool(
                self.cgroup_inodes or self.pid_namespace_inodes
            )
            events = sorted(
                (
                    event
                    for event in scope_events
                    if token.started_ns <= event["ts_ns"] <= ended_ns
                    and (
                        not runtime_identity_available
                        or event.get("cgroup_id", 0) in self.cgroup_inodes
                        or event.get("pid_namespace_inode", 0)
                        in self.pid_namespace_inodes
                        or event.get("host_pid", 0) in container_pids
                        or event.get("child_host_pid", 0) in container_pids
                    )
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
        replay_exit_code = _runtime_response_exit_code(replay_response)
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
            not control_flow_fidelity["source_action_available"]
            or (
                control_flow_fidelity["source_command_matches"]
                and control_flow_fidelity["exit_code_matches"]
                and control_flow_fidelity["tool_result_exact"]
            )
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
            # Event collection and delimiter accounting completed before
            # _summarize_call.  Parser/bridge failures invalidate this call,
            # but must not rewrite a healthy eBPF collector as unavailable or
            # discard the collector's real loss/kprobe counters.
            if message not in self._integrity_errors:
                self._integrity_errors.append(message)
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
                "clauses": [],
                "no_runtime_exec": [],
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

    @_lifecycle_synchronized
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
            # A safety-guard record reaches analysis only after collector
            # counters were read successfully.  Keep analysis availability
            # call-granular for the same reason as finish_tool_call.
            if message not in self._integrity_errors:
                self._integrity_errors.append(message)
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
                "clauses": [],
                "no_runtime_exec": [],
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

        collector_perf_samples = perf_samples
        events, event_isolation = _isolate_call_events(
            events,
            token.command,
            trusted_root_pid=self.trusted_root_pid,
            allow_trusted_root_pid_remap=self._trusted_root_pid_remap_allowed,
        )
        effective_trusted_root_pid = (
            int(event_isolation["trusted_root_pid"])
            if event_isolation.get("trusted_root_pid") is not None
            else self.trusted_root_pid
        )
        perf_samples = sum(event["type"] == "perf" for event in events)
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
                trusted_root_pid=effective_trusted_root_pid,
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
                "trusted_root_pre_exec_structural_setup",
            }
            if event["reason"] in structural_setup_reasons:
                relation = event["reason"]
            elif (
                effective_trusted_root_pid is not None
                and event["host_pid"] == effective_trusted_root_pid
            ):
                relation = "trusted_execution_root"
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
                "trusted_root_pre_exec_structural_setup",
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
                "trusted_root_pre_exec_structural_setup",
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
                "status": bridged.status,
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
                "status": {
                    "state": "not_executed",
                    "exit_code": None,
                    "signal": None,
                    "succeeded": False,
                    "reason": resolved.mapping_evidence,
                    "source": "explicit_no_runtime_evidence",
                },
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
                row["status"]["state"] = "exec_failed"
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
            row["status"].update(
                {
                    "state": "exited",
                    "exit_code": evidence.replay_exit_code,
                    "reason": "shell_command_lookup_failure",
                    "source": (
                        "live_shell_exit_code"
                        if evidence.evidence_mode == "live_execution"
                        else "source_replay_exit_code"
                    ),
                }
            )
            row["provenance"] = {
                "evidence_kind": "shell_command_lookup_failure",
                "evidence_mode": evidence.evidence_mode,
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
        for target in ("latency", "cpu", "memory", "disk_io", "status"):
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
                "collector_perf_sample_count": collector_perf_samples,
            },
            "ring_loss": {
                "reserve_failures": loss,
                "perf_sample_count": perf_samples,
                "collector_perf_sample_count": collector_perf_samples,
            },
            "clauses": clauses,
            "no_runtime_exec": no_runtime_exec,
            "provenance": {
                "collector": "ebpf_ebpf",
                "repo": self.repo,
                "container_cgroup_id": self.cgroup_id,
                "quota_cores": self.quota_cores,
                "page_size_bytes": PAGE,
                "call_started_monotonic_ns": token.started_ns,
                "call_ended_monotonic_ns": ended_ns,
                "raw_event_count": len(events),
                "exec_image_count": len(metrics),
                "command_tree": command_tree,
                "event_isolation": event_isolation,
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

    @_lifecycle_synchronized
    def add_integrity_error(self, message: str) -> None:
        self._disable(message)

    @_lifecycle_synchronized
    def finalize(self, *, replay_execution: str = "completed") -> None:
        if self.state == "closed":
            return
        if replay_execution not in {"completed", "failed", "incomplete"}:
            raise ValueError(f"invalid replay execution state {replay_execution!r}")
        try:
            current_loss_counts = (
                _loss_counts(self._bpf)
                if self.state == "active"
                else dict.fromkeys(LOSS_COUNTER_NAMES, 0)
            )
            total_loss_counts = {
                name: max(
                    0,
                    int(current_loss_counts.get(name, 0))
                    - int(self._loss_baseline.get(name, 0)),
                )
                for name in LOSS_COUNTER_NAMES
            }
        except BaseException as exc:
            total_loss_counts = dict.fromkeys(LOSS_COUNTER_NAMES, 0)
            self._disable(f"collector counter read failed: {type(exc).__name__}: {exc}")
        total_loss = sum(total_loss_counts.values())
        # Read diagnostic kprobe counter to determine if kprobes fired at all.
        try:
            kprobe_hits = (
                max(
                    0,
                    int(
                        self._bpf["kprobe_total_hits"][ctypes.c_int(0)].value
                    )
                    - self._kprobe_hits_baseline,
                )
                if self.state == "active" and hasattr(self, "_bpf")
                else 0
            )
        except BaseException:
            kprobe_hits = 0
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
        poll_error = self._shared_poll_error()
        if poll_error is not None:
            self._disable(
                "ring poller failed: "
                f"{type(poll_error).__name__}: {poll_error}"
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
        if not collector_healthy:
            telemetry_quality = "unavailable"
        elif invalid_count:
            telemetry_quality = "invalid"
        else:
            telemetry_quality = "ok"
        formal_completeness = (
            "unavailable"
            if not collector_healthy
            else ("complete" if eligible_count == len(self.calls) else "partial")
        )
        collection_validity = (
            "valid"
            if collector_healthy and invalid_count == 0
            else "invalid"
        )
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
                    "schema": "clause_telemetry_v2",
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
                        "kprobe_total_hits": kprobe_hits,
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
                        "status": (
                            "ok"
                            if collector_healthy and invalid_count == 0
                            else "failed"
                        ),
                        "errors": collector_errors,
                    },
                    "provenance": {
                        "collector": "ebpf_ebpf",
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
        source = getattr(self, "_source", None)
        lease_id = getattr(self, "_source_lease_id", None)
        if source is not None and lease_id is not None:
            source.release(lease_id)
            self._source_lease_id = None
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
    "validate_clause_telemetry_smoke",
    "validate_clause_telemetry_runtime",
]
