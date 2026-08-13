from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clawtune_sidecar.contracts.models import ResourceScope
from clawtune_sidecar.monitoring.net_accounting import (
    ProcessNetAccounting,
    _pid_namespace_inode,
)


@dataclass(frozen=True)
class ResourceSnapshot:
    captured_at: float
    monotonic_s: float
    process_cpu_time_s: float | None
    rss_bytes: int | None
    read_bytes: int | None
    write_bytes: int | None
    net_rx_bytes: int | None
    net_tx_bytes: int | None
    ctx_switches: int | None
    target_pid: int | None
    process_count: int | None
    available: bool
    source: str


def _net_accounting_isolated(scope: ResourceScope) -> bool:
    """True when a scope's pid set is per-tool disjoint.

    Only per-PID trees and per-execution cgroups (``exclusive-execution-cgroup``)
    isolate one tool's processes.  The shared sandbox/runtime container cgroup,
    and any host-derived container cgroup, overlap every concurrent tool, so the
    per-tgid BCC tracker's reset() must not be used for them.
    """
    if scope.kind == "pid":
        return True
    return (
        scope.kind == "cgroup-v2"
        and scope.attribution_source == "exclusive-execution-cgroup"
    )


class ProcessResourceSampler:
    """Best-effort sampler for a target process tree.

    This mirrors the important agent-test-bench invariant: resource samples must
    belong to the agent/tool process tree. If OpenClaw does not provide a PID,
    the result is explicitly unattributed instead of sampling the sidecar.
    """

    def __init__(self) -> None:
        self._psutil = self._load_psutil()
        self._net: ProcessNetAccounting | None = None
        self._net_lock = threading.Lock()

    def snapshot(
        self,
        scope: ResourceScope | None = None,
        *,
        net_mode: str = "ignore",
    ) -> ResourceSnapshot:
        now = time.time()
        mono = time.monotonic()
        if scope is None:
            return self._empty(now, mono, None, "unattributed")
        if scope.kind == "cgroup-v2" and scope.cgroup_path:
            if _is_cgroup_root(scope.cgroup_path):
                return self._empty(now, mono, None, "cgroup-root-unattributed")
            cgroup = self._snapshot_cgroup(now, mono, scope, net_mode=net_mode)
            return cgroup
        if scope.pid is None:
            return self._empty(now, mono, scope.root_pid, "pid-unavailable")
        if self._psutil is None:
            return self._empty(now, mono, scope.pid, "psutil-unavailable")
        try:
            if scope.root_starttime_ticks is not None:
                observed_starttime = self._read_pid_starttime_ticks(scope.root_pid or scope.pid)
                if observed_starttime is not None and observed_starttime != int(scope.root_starttime_ticks):
                    return self._empty(now, mono, scope.pid, "pid-reused")
            process = self._psutil.Process(scope.pid)
            if scope.process_start_time is not None:
                create_time = float(process.create_time())
                if abs(create_time - scope.process_start_time) > 1.0:
                    return self._empty(now, mono, scope.pid, "pid-reused")
            processes = [process]
            if scope.include_children:
                try:
                    processes.extend(process.children(recursive=True))
                except Exception:
                    pass
            pids = [p.pid for p in processes]
            net_rx_bytes, net_tx_bytes = self._snapshot_net(
                scope, pids, net_mode
            )
            return self._snapshot_processes(
                now,
                mono,
                scope.pid,
                processes,
                net_rx_bytes=net_rx_bytes,
                net_tx_bytes=net_tx_bytes,
            )
        except Exception:
            return self._empty(now, mono, scope.pid, "pid-unavailable")

    def _net_accounting(self, scope: ResourceScope) -> ProcessNetAccounting | None:
        """Lazily create (and namespace-seed) the shared per-process net tracker."""
        probe_pid = scope.root_pid or scope.pid
        ns_inode = _pid_namespace_inode(probe_pid) if probe_pid else None
        if ns_inode is None:
            return None
        with self._net_lock:
            if self._net is None:
                self._net = ProcessNetAccounting([ns_inode])
            else:
                self._net.add_namespace(ns_inode)
            return self._net

    def _snapshot_net(
        self,
        scope: ResourceScope,
        pids: list[int],
        net_mode: str,
    ) -> tuple[int | None, int | None]:
        """Return per-process (rx, tx) for the tool's pid tree.

        net_mode:
          - ``reset``   tool begin: zero the pids' counters so the window only
                        counts traffic after this point.
          - ``read``    tool complete: sum the pids' counters since reset.
          - ``ignore``  polling / rebasing: do not disturb the counters and
                        emit None (the window aggregate is read at complete).
        Falls back to the namespace-wide ``/proc/net/dev`` view when the
        isolated per-process BCC tracker is unavailable.
        """
        # The per-tgid BCC tracker's reset() deletes counters, which is only
        # safe when the scope's pid set is per-tool disjoint (a per-execution
        # cgroup or a per-PID tree).  Shared container/runtime cgroups overlap
        # every concurrent tool, so resets would clobber the other tools'
        # windows and complete() would read a non-deterministic slice.  Keep
        # the namespace-wide cumulative delta (begin baseline vs complete) for
        # shared scopes instead.
        if not _net_accounting_isolated(scope):
            net = self._read_proc_net_dev(scope.root_pid or scope.pid)
            if net is None:
                return None, None
            return net[0], net[1]
        accounting = self._net_accounting(scope)
        if accounting is not None and accounting.available:
            if net_mode == "reset":
                accounting.reset(pids)
                return 0, 0
            if net_mode == "read":
                return accounting.rx_tx_for(pids)
            return None, None
        net = self._read_proc_net_dev(scope.root_pid or scope.pid)
        if net is None:
            return None, None
        return net[0], net[1]

    def _snapshot_cgroup(
        self,
        now: float,
        mono: float,
        scope: ResourceScope,
        *,
        net_mode: str = "ignore",
    ) -> ResourceSnapshot:
        cgroup_path = Path(scope.cgroup_path or "")
        cpu_usec = self._read_cgroup_cpu_usec(cgroup_path)
        memory_current = self._read_int_file(cgroup_path / "memory.current")
        io = self._read_cgroup_io_stat(cgroup_path)
        pids = self._read_cgroup_pids(cgroup_path)
        ctx_switches = self._aggregate_context_switches(pids)
        # cgroup v2 exposes no native network counter; use the per-process BCC
        # tracker summed over the cgroup's own pid set so net stays per-PID.
        net_rx_bytes, net_tx_bytes = self._snapshot_net(scope, pids, net_mode)
        cgroup_available = (
            (cpu_usec is not None and cpu_usec > 0)
            or memory_current is not None
            or io is not None
            or bool(pids)
            or ctx_switches is not None
        )
        return ResourceSnapshot(
            captured_at=now,
            monotonic_s=mono,
            process_cpu_time_s=(cpu_usec / 1_000_000) if cpu_usec is not None else None,
            rss_bytes=memory_current,
            read_bytes=None if io is None else io[0],
            write_bytes=None if io is None else io[1],
            net_rx_bytes=net_rx_bytes,
            net_tx_bytes=net_tx_bytes,
            ctx_switches=ctx_switches,
            target_pid=scope.root_pid or scope.pid,
            process_count=len(pids) if pids else None,
            available=cgroup_available,
            source="cgroup-v2",
        )

    @staticmethod
    def _read_int_file(path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_cgroup_cpu_usec(cgroup_path: Path) -> int | None:
        try:
            text = (cgroup_path / "cpu.stat").read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "usage_usec":
                try:
                    return int(parts[1])
                except ValueError:
                    return None
        return None

    @staticmethod
    def _read_cgroup_io_stat(cgroup_path: Path) -> tuple[int, int] | None:
        try:
            text = (cgroup_path / "io.stat").read_text(encoding="utf-8")
        except OSError:
            return None
        read_bytes = 0
        write_bytes = 0
        found = False
        for line in text.splitlines():
            for field in line.split():
                key, sep, value = field.partition("=")
                if sep != "=":
                    continue
                try:
                    parsed = int(value)
                except ValueError:
                    continue
                if key == "rbytes":
                    read_bytes += parsed
                    found = True
                elif key == "wbytes":
                    write_bytes += parsed
                    found = True
        return (read_bytes, write_bytes) if found else None

    @staticmethod
    def _read_cgroup_pids(cgroup_path: Path) -> list[int]:
        try:
            text = (cgroup_path / "cgroup.procs").read_text(encoding="utf-8")
        except OSError:
            return []
        pids: list[int] = []
        for line in text.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                pass
        return pids

    @staticmethod
    def _aggregate_context_switches(pids: list[int]) -> int | None:
        total = 0
        found = False
        for pid in pids:
            try:
                text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                key, sep, value = line.partition(":")
                if sep != ":" or key not in {"voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}:
                    continue
                try:
                    total += int(value.strip())
                    found = True
                except ValueError:
                    pass
        return total if found else None

    @staticmethod
    def _read_proc_net_dev(pid: int | None) -> tuple[int, int] | None:
        if pid is None or pid <= 0:
            return None
        try:
            text = Path(f"/proc/{pid}/net/dev").read_text(encoding="utf-8")
        except OSError:
            return None
        rx_total = 0
        tx_total = 0
        found = False
        for line in text.splitlines():
            if "|" in line or line.startswith("Inter-"):
                continue
            iface, sep, rest = line.partition(":")
            if sep != ":" or iface.strip() == "lo":
                continue
            fields = rest.split()
            if len(fields) < 9:
                continue
            try:
                rx_total += int(fields[0])
                tx_total += int(fields[8])
                found = True
            except ValueError:
                pass
        return (rx_total, tx_total) if found else None

    @staticmethod
    def _read_pid_starttime_ticks(pid: int | None) -> int | None:
        if pid is None or pid <= 0:
            return None
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        close = text.rfind(")")
        if close < 0:
            return None
        fields = text[close + 1 :].split()
        if len(fields) <= 19:
            return None
        try:
            return int(fields[19])
        except ValueError:
            return None

    def _snapshot_processes(
        self,
        now: float,
        mono: float,
        target_pid: int,
        processes: list[Any],
        *,
        net_rx_bytes: int | None = None,
        net_tx_bytes: int | None = None,
    ) -> ResourceSnapshot:
        cpu_time = 0.0
        rss_bytes = 0
        read_bytes = 0
        write_bytes = 0
        ctx_switches = 0
        found_cpu = False
        found_memory = False
        found_io = False
        found_ctx = False
        process_count = 0
        for process in processes:
            try:
                cpu = process.cpu_times()
                cpu_time += float(cpu.user + cpu.system)
                found_cpu = True
            except Exception:
                pass
            try:
                rss_bytes += int(process.memory_info().rss)
                found_memory = True
            except Exception:
                pass
            try:
                io = process.io_counters()
                read_bytes += int(getattr(io, "read_bytes", 0))
                write_bytes += int(getattr(io, "write_bytes", 0))
                found_io = True
            except Exception:
                pass
            try:
                ctx = process.num_ctx_switches()
                ctx_switches += int(ctx.voluntary + ctx.involuntary)
                found_ctx = True
            except Exception:
                pass
            process_count += 1
        return ResourceSnapshot(
            captured_at=now,
            monotonic_s=mono,
            process_cpu_time_s=cpu_time if found_cpu else None,
            rss_bytes=rss_bytes if found_memory else None,
            read_bytes=read_bytes if found_io else None,
            write_bytes=write_bytes if found_io else None,
            net_rx_bytes=net_rx_bytes,
            net_tx_bytes=net_tx_bytes,
            ctx_switches=ctx_switches if found_ctx else None,
            target_pid=target_pid,
            process_count=process_count,
            available=(
                found_cpu
                or found_memory
                or found_io
                or found_ctx
                or net_rx_bytes is not None
                or net_tx_bytes is not None
            ),
            source="psutil-process-tree",
        )

    @staticmethod
    def _empty(now: float, mono: float, target_pid: int | None, source: str) -> ResourceSnapshot:
        return ResourceSnapshot(
            captured_at=now,
            monotonic_s=mono,
            process_cpu_time_s=None,
            rss_bytes=None,
            read_bytes=None,
            write_bytes=None,
            net_rx_bytes=None,
            net_tx_bytes=None,
            ctx_switches=None,
            target_pid=target_pid,
            process_count=None,
            available=False,
            source=source,
        )

    @staticmethod
    def _load_psutil() -> Any | None:
        try:
            import psutil  # type: ignore[import-not-found,import-untyped]

            return psutil
        except Exception:
            return None


def _is_cgroup_root(cgroup_path: str) -> bool:
    normalized = cgroup_path.replace("\\", "/").rstrip("/")
    return normalized in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}
