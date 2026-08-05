"""Per-process network accounting (fail-soft BCC).

The psutil process-tree sampler reads ``/proc/<pid>/net/dev``, which is
network-namespace-wide: it charges the whole container's traffic to every tool
and to the sidecar itself.  This module attaches a small, *isolated* BCC
program (kretprobes on ``tcp_sendmsg`` / ``tcp_recvmsg``) that accumulates
rx/tx bytes per host tgid for processes inside a target PID namespace, so
cpu / mem / disk / net are all attributed to the same process tree.

Lifecycle contract used by ``ProcessResourceSampler``:

- ``begin``  -> :meth:`ProcessNetAccounting.reset` for the tool's PIDs: their
  per-tgid counters are zeroed so the next snapshot only counts traffic after
  this point (handles long-lived in-process tools such as read/edit that share
  the agent process).
- ``complete`` -> :meth:`ProcessNetAccounting.rx_tx_for` for the tool's PIDs:
  sum of per-tgid counters since the reset == the tool's own network traffic.

The program is fully additive and fail-soft: if BCC is unavailable, the kernel
symbols are missing, or attach fails, :attr:`available` stays ``False`` and
callers fall back to the previous namespace-wide behaviour (or ``None``).
Nothing else in the scheduler is affected by a failure here.
"""
from __future__ import annotations

import ctypes
import threading
from typing import Any, Iterable

_BPF_SOURCE = r"""
#include <linux/nsproxy.h>
#include <linux/pid_namespace.h>
#include <linux/sched.h>
#include <uapi/linux/ptrace.h>

struct claw_net_key_t {
    u32 tgid;
    u32 pad;
};
BPF_HASH(claw_net_rx, struct claw_net_key_t, u64, 65536);
BPF_HASH(claw_net_tx, struct claw_net_key_t, u64, 65536);
BPF_HASH(claw_allowed_pid_namespaces, u64, u8, 256);

static int claw_net_wanted(void) {
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct nsproxy *nsproxy = 0;
    struct pid_namespace *pid_ns = 0;
    u32 inum = 0;
    u64 inum_key = 0;
    bpf_probe_read_kernel(&nsproxy, sizeof(nsproxy), &task->nsproxy);
    if (!nsproxy) return 0;
    bpf_probe_read_kernel(&pid_ns, sizeof(pid_ns), &nsproxy->pid_ns_for_children);
    if (!pid_ns) return 0;
    bpf_probe_read_kernel(&inum, sizeof(inum), &pid_ns->ns.inum);
    if (!inum) return 0;
    inum_key = (u64)inum;
    return claw_allowed_pid_namespaces.lookup(&inum_key) != 0;
}

static void claw_net_add_rx(u32 tgid, u64 bytes) {
    struct claw_net_key_t key = { .tgid = tgid, .pad = 0 };
    u64 *value = claw_net_rx.lookup(&key);
    if (value) { __sync_fetch_and_add(value, bytes); return; }
    u64 zero = 0;
    claw_net_rx.update(&key, &zero);
    value = claw_net_rx.lookup(&key);
    if (value) __sync_fetch_and_add(value, bytes);
}

static void claw_net_add_tx(u32 tgid, u64 bytes) {
    struct claw_net_key_t key = { .tgid = tgid, .pad = 0 };
    u64 *value = claw_net_tx.lookup(&key);
    if (value) { __sync_fetch_and_add(value, bytes); return; }
    u64 zero = 0;
    claw_net_tx.update(&key, &zero);
    value = claw_net_tx.lookup(&key);
    if (value) __sync_fetch_and_add(value, bytes);
}

int claw_net_send_ret(struct pt_regs *ctx) {
    if (!claw_net_wanted()) return 0;
    s64 ret = PT_REGS_RC(ctx);
    if (ret <= 0) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    claw_net_add_tx((u32)(pid_tgid >> 32), (u64)ret);
    return 0;
}

int claw_net_recv_ret(struct pt_regs *ctx) {
    if (!claw_net_wanted()) return 0;
    s64 ret = PT_REGS_RC(ctx);
    if (ret <= 0) return 0;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    claw_net_add_rx((u32)(pid_tgid >> 32), (u64)ret);
    return 0;
}
"""


def _ensure_bcc_importable() -> Any:
    """Return usable BCC bindings (module name ``bcc`` or ``bpfcc``)."""
    import importlib
    import sys

    for module_name in ("bcc", "bpfcc"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        if all(hasattr(module, attr) for attr in ("BPF", "PerfSWConfig", "PerfType")):
            if module_name != "bcc" and "bcc" not in sys.modules:
                sys.modules["bcc"] = module
            return module
    raise RuntimeError(
        "BCC Python bindings are unavailable; tried modules 'bcc' and 'bpfcc'"
    )


def _pid_namespace_inode(pid: int) -> int | None:
    import os

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


def _map_value(table: Any, key: Any) -> int:
    """Read a scalar value from a BCC hash map robustly across BCC versions."""
    try:
        value = table[key]
    except Exception:
        return 0
    if isinstance(value, (list, tuple)):
        try:
            return int(value[0] or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class ProcessNetAccounting:
    """Per-process TCP byte accounting backed by an isolated BCC program."""

    def __init__(
        self,
        pid_namespace_inodes: Iterable[int],
    ) -> None:
        self.available = False
        self._bpf: Any | None = None
        self._rx: Any | None = None
        self._tx: Any | None = None
        self._allowed: Any | None = None
        self._lock = threading.RLock()
        self._attach_error: str | None = None
        try:
            bcc = _ensure_bcc_importable()
            BPF = bcc.BPF
            bpf = BPF(text=_BPF_SOURCE)
            bpf.attach_kretprobe(
                event="tcp_sendmsg", fn_name="claw_net_send_ret"
            )
            bpf.attach_kretprobe(
                event="tcp_recvmsg", fn_name="claw_net_recv_ret"
            )
            allowed = bpf["claw_allowed_pid_namespaces"]
            for inode in pid_namespace_inodes:
                if inode:
                    allowed[ctypes.c_uint64(inode)] = ctypes.c_ubyte(1)
            self._bpf = bpf
            self._rx = bpf["claw_net_rx"]
            self._tx = bpf["claw_net_tx"]
            self._allowed = allowed
            self.available = True
        except Exception as exc:  # noqa: BLE001 - fail-soft by design
            self.available = False
            self._attach_error = str(exc)

    def add_namespace(self, pid_namespace_inode: int | None) -> None:
        """Allow another PID namespace (e.g. a parallel sandbox container)."""
        if not self.available or not self._allowed or not pid_namespace_inode:
            return
        with self._lock:
            try:
                self._allowed[ctypes.c_uint64(pid_namespace_inode)] = (
                    ctypes.c_ubyte(1)
                )
            except Exception:
                pass

    def reset(self, pids: Iterable[int]) -> None:
        """Zero the per-tgid counters for *pids* (tool begin / baseline)."""
        if not self.available or not self._rx or not self._tx:
            return
        with self._lock:
            for pid in pids:
                if not pid or pid <= 0:
                    continue
                key = ctypes.c_uint32(pid)
                try:
                    del self._rx[key]
                except Exception:
                    pass
                try:
                    del self._tx[key]
                except Exception:
                    pass

    def rx_tx_for(self, pids: Iterable[int]) -> tuple[int, int]:
        """Sum rx/tx bytes for *pids* since their last reset."""
        if not self.available or not self._rx or not self._tx:
            return 0, 0
        rx = 0
        tx = 0
        with self._lock:
            for pid in pids:
                if not pid or pid <= 0:
                    continue
                key = ctypes.c_uint32(pid)
                rx += _map_value(self._rx, key)
                tx += _map_value(self._tx, key)
        return rx, tx

    def close(self) -> None:
        if self._bpf is not None:
            try:
                self._bpf.cleanup()
            except Exception:
                pass
            self._bpf = None
            self.available = False
