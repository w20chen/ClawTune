"""Low-overhead, per-execution Linux PMU counting.

The collector deliberately uses task-scoped ``perf_event_open`` groups with
``inherit`` and ``enable_on_exec``.  A gated execution root therefore owns one
fixed four-FD group which starts at the payload exec and follows future
threads/processes.  This avoids both polling and the ``events x online CPUs``
FD growth of perf's cgroup mode.

The public result is ``pmu_profile_v1``.  Counts are never silently scaled:
``raw_count`` is what hardware actually counted and ``scaled_count`` is kept
separate.  Multiplexed and partial profiles are not eligible for the online
KB.
"""
from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import platform
import struct
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows import smoke
    fcntl = None  # type: ignore[assignment]
try:
    import resource
except ImportError:  # pragma: no cover - exercised by Windows import smoke
    resource = None  # type: ignore[assignment]


PERF_TYPE_HARDWARE = 0
PERF_TYPE_HW_CACHE = 3
PERF_COUNT_HW_CPU_CYCLES = 0
PERF_COUNT_HW_INSTRUCTIONS = 1
PERF_COUNT_HW_CACHE_LL = 2
PERF_COUNT_HW_CACHE_OP_READ = 0
PERF_COUNT_HW_CACHE_RESULT_ACCESS = 0
PERF_COUNT_HW_CACHE_RESULT_MISS = 1

PERF_FORMAT_TOTAL_TIME_ENABLED = 1 << 0
PERF_FORMAT_TOTAL_TIME_RUNNING = 1 << 1
PERF_EVENT_IOC_DISABLE = 0x2401
PERF_IOC_FLAG_GROUP = 1

_FLAG_DISABLED = 1 << 0
_FLAG_INHERIT = 1 << 1
_FLAG_EXCLUDE_KERNEL = 1 << 5
_FLAG_EXCLUDE_HV = 1 << 6
_FLAG_ENABLE_ON_EXEC = 1 << 12
_READ_FORMAT = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING
_EVENT_FDS = 4
_SCHEMA = "pmu_profile_v1"


def _cache_config(result: int) -> int:
    return (
        PERF_COUNT_HW_CACHE_LL
        | (PERF_COUNT_HW_CACHE_OP_READ << 8)
        | (result << 16)
    )


@dataclass(frozen=True)
class EventSpec:
    name: str
    event_type: int
    config: int
    semantics: str


EVENT_SPECS = (
    EventSpec("cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES,
              "PERF_COUNT_HW_CPU_CYCLES"),
    EventSpec("instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS,
              "PERF_COUNT_HW_INSTRUCTIONS"),
    EventSpec("llc_read_misses", PERF_TYPE_HW_CACHE,
              _cache_config(PERF_COUNT_HW_CACHE_RESULT_MISS),
              "PERF_COUNT_HW_CACHE_LL:READ:MISS"),
    EventSpec("llc_read_accesses", PERF_TYPE_HW_CACHE,
              _cache_config(PERF_COUNT_HW_CACHE_RESULT_ACCESS),
              "PERF_COUNT_HW_CACHE_LL:READ:ACCESS"),
)


class _PerfEventAttr(ctypes.Structure):
    # PERF_ATTR_SIZE_VER5.  All fields after ``sample_regs_intr`` are unused,
    # but retaining the stable 112-byte ABI works on the supported kernels.
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample_period", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("wakeup_events", ctypes.c_uint32),
        ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),
        ("config2", ctypes.c_uint64),
        ("branch_sample_type", ctypes.c_uint64),
        ("sample_regs_user", ctypes.c_uint64),
        ("sample_stack_user", ctypes.c_uint32),
        ("clockid", ctypes.c_int32),
        ("sample_regs_intr", ctypes.c_uint64),
        ("aux_watermark", ctypes.c_uint32),
        ("sample_max_stack", ctypes.c_uint16),
        ("reserved_2", ctypes.c_uint16),
    ]


class PerfBackend(Protocol):
    def open_event(self, spec: EventSpec, pid: int, group_fd: int, *,
                   leader: bool, exclude_kernel: bool) -> int: ...
    def disable_group(self, leader_fd: int) -> None: ...
    def read_event(self, fd: int) -> tuple[int, int, int]: ...
    def close(self, fd: int) -> None: ...


class LinuxPerfBackend:
    """Minimal libc wrapper; no mmap ring buffer and no sampling setup."""

    _SYSCALLS = {
        "x86_64": 298,
        "amd64": 298,
        "aarch64": 241,
        "arm64": 241,
    }

    def __init__(self) -> None:
        machine = platform.machine().lower()
        if not sys_platform_linux():
            raise OSError(errno.ENOSYS, "perf_event_open requires Linux")
        if fcntl is None:
            raise OSError(errno.ENOSYS, "fcntl is unavailable")
        try:
            self._syscall_number = self._SYSCALLS[machine]
        except KeyError as exc:
            raise OSError(errno.ENOSYS, f"unsupported perf syscall architecture: {machine}") from exc
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.syscall.restype = ctypes.c_long

    def open_event(self, spec: EventSpec, pid: int, group_fd: int, *,
                   leader: bool, exclude_kernel: bool) -> int:
        attr = _PerfEventAttr()
        attr.type = spec.event_type
        attr.size = ctypes.sizeof(_PerfEventAttr)
        attr.config = spec.config
        attr.read_format = _READ_FORMAT
        attr.flags = _FLAG_INHERIT | _FLAG_EXCLUDE_HV
        if leader:
            attr.flags |= _FLAG_DISABLED | _FLAG_ENABLE_ON_EXEC
        if exclude_kernel:
            attr.flags |= _FLAG_EXCLUDE_KERNEL
        fd = int(self._libc.syscall(
            ctypes.c_long(self._syscall_number),
            ctypes.byref(attr),
            ctypes.c_int(pid),
            ctypes.c_int(-1),
            ctypes.c_int(group_fd),
            ctypes.c_ulong(1 << 3),  # PERF_FLAG_FD_CLOEXEC
        ))
        if fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return fd

    def disable_group(self, leader_fd: int) -> None:
        assert fcntl is not None
        fcntl.ioctl(leader_fd, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP)

    def read_event(self, fd: int) -> tuple[int, int, int]:
        payload = os.read(fd, 24)
        if len(payload) != 24:
            raise OSError(errno.EIO, f"short perf counter read: {len(payload)}")
        return struct.unpack("=QQQ", payload)

    def close(self, fd: int) -> None:
        os.close(fd)


@dataclass(frozen=True)
class PmuEventReading:
    supported: bool
    semantics: str
    raw_count: int | None = None
    scaled_count: float | None = None
    time_enabled_ns: int | None = None
    time_running_ns: int | None = None
    running_ratio: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class PmuCoverage:
    status: str
    reason: str
    running_ratio: float | None
    multiplexed: bool
    kernel_included: bool
    root_and_future_descendants: bool
    eligible_for_kb: bool


@dataclass(frozen=True)
class PmuProfile:
    schema: str
    execution_id: str
    source: str
    mode: str
    scope: str
    root_pid: int | None
    started_at: float | None
    ended_at: float | None
    architecture: str
    pmu_devices: tuple[str, ...]
    llc_semantics: str
    llc_semantics_confirmed: bool
    events: dict[str, PmuEventReading]
    derived: dict[str, float | None]
    coverage: PmuCoverage
    collector_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _ActiveGroup:
    execution_id: str
    root_pid: int
    started_at: float
    fds: dict[str, int]
    errors: dict[str, str]
    kernel_included: bool
    process_identity: str | None


def sys_platform_linux() -> bool:
    return platform.system().lower() == "linux"


@lru_cache(maxsize=4)
def _pmu_devices(root: Path = Path("/sys/bus/event_source/devices")) -> tuple[str, ...]:
    try:
        names = sorted(
            item.name for item in root.iterdir()
            if item.name in {"cpu", "armv8_pmuv3"}
            or item.name.startswith(("armv8_pmuv3_", "arm_", "hisi_"))
        )
    except OSError:
        return ()
    return tuple(names)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"errno={exc.errno}:{errno.errorcode.get(exc.errno, 'UNKNOWN')}"
    return type(exc).__name__


def _unavailable_profile(execution_id: str, reason: str, *, root_pid: int | None = None,
                         errors: tuple[str, ...] = ()) -> PmuProfile:
    readings = {
        spec.name: PmuEventReading(False, spec.semantics, error=reason)
        for spec in EVENT_SPECS
    }
    return PmuProfile(
        schema=_SCHEMA,
        execution_id=execution_id,
        source="perf_event_open",
        mode="counting",
        scope="task-inherit-enable-on-exec",
        root_pid=root_pid,
        started_at=None,
        ended_at=None,
        architecture=platform.machine().lower(),
        pmu_devices=_pmu_devices(),
        llc_semantics=(
            "Linux PERF_TYPE_HW_CACHE last-level cache read accesses/misses; "
            "never PERF_COUNT_HW_CACHE_MISSES and never uncore hisi_l3c"
        ),
        llc_semantics_confirmed=False,
        events=readings,
        derived={"ipc": None, "llc_mpki": None, "llc_miss_rate": None},
        coverage=PmuCoverage(
            status="unavailable",
            reason=reason,
            running_ratio=None,
            multiplexed=False,
            kernel_included=False,
            root_and_future_descendants=False,
            eligible_for_kb=False,
        ),
        collector_errors=errors,
    )


class PmuCollector:
    """Process-wide concurrency/FD budget for per-execution PMU groups."""

    def __init__(self, *, enabled: bool = True, max_active: int = 1,
                 max_fds: int | None = None, reliable_ratio: float = 0.95,
                 backend: PerfBackend | None = None) -> None:
        self.enabled = enabled
        self.max_active = max(1, int(max_active))
        self.max_fds = max(_EVENT_FDS, int(max_fds or self.max_active * _EVENT_FDS))
        self.reliable_ratio = min(1.0, max(0.0, float(reliable_ratio)))
        self._backend = backend
        self._backend_error: str | None = None
        self._active: dict[str, _ActiveGroup] = {}
        self._completed: dict[str, PmuProfile] = {}
        self._lock = threading.RLock()

    def _remember_completed_locked(self, profile: PmuProfile) -> None:
        self._completed[profile.execution_id] = profile
        while len(self._completed) > max(256, self.max_active * 8):
            self._completed.pop(next(iter(self._completed)))

    def _reap_exited_locked(self) -> None:
        """Recover capacity after a lost exit callback, without polling."""
        stale = [
            execution_id for execution_id, group in self._active.items()
            if group.process_identity is not None
            and _process_identity(group.root_pid) != group.process_identity
        ]
        for execution_id in stale:
            group = self._active.pop(execution_id)
            try:
                profile = self._finish_group(group, "root_exited_before_callback")
            except BaseException as exc:
                profile = _unavailable_profile(
                    execution_id, "collector_failure", root_pid=group.root_pid,
                    errors=(_error_text(exc),),
                )
            self._remember_completed_locked(profile)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            active = len(self._active)
        paranoid: int | None = None
        try:
            paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
        except (OSError, ValueError):
            pass
        return {
            "enabled": self.enabled,
            "mode": "counting",
            "scope": "task-inherit-enable-on-exec",
            "events_per_execution": _EVENT_FDS,
            "max_active": self.max_active,
            "max_fds": self.max_fds,
            "active": active,
            "architecture": platform.machine().lower(),
            "pmu_devices": list(_pmu_devices()),
            "perf_event_paranoid": paranoid,
            "llc_event_semantics": [EVENT_SPECS[2].semantics, EVENT_SPECS[3].semantics],
            "generic_cache_miss_fallback": False,
        }

    def _get_backend(self) -> PerfBackend:
        if self._backend is None:
            self._backend = LinuxPerfBackend()
        return self._backend

    def begin(self, execution_id: str, root_pid: int) -> PmuProfile | None:
        """Arm one group while the execution root is still behind its exec gate."""
        with self._lock:
            if execution_id in self._active:
                return None
            if execution_id in self._completed:
                return self._completed[execution_id]
            self._reap_exited_locked()
            if not self.enabled:
                profile = _unavailable_profile(execution_id, "disabled", root_pid=root_pid)
                self._remember_completed_locked(profile)
                return profile
            if len(self._active) >= self.max_active or (
                len(self._active) + 1
            ) * _EVENT_FDS > self.max_fds:
                profile = _unavailable_profile(
                    execution_id, "resource_budget", root_pid=root_pid
                )
                self._remember_completed_locked(profile)
                return profile
            try:
                backend = self._get_backend()
            except BaseException as exc:
                reason = "platform_unsupported"
                self._backend_error = _error_text(exc)
                profile = _unavailable_profile(
                    execution_id, reason, root_pid=root_pid,
                    errors=(self._backend_error,),
                )
                self._remember_completed_locked(profile)
                return profile

            group, permission_error = self._open_group(
                backend, execution_id, root_pid, exclude_kernel=False
            )
            if permission_error:
                self._close_group(backend, group)
                group, _ = self._open_group(
                    backend, execution_id, root_pid, exclude_kernel=True
                )
            if not group.fds:
                errors = tuple(sorted(set(group.errors.values())))
                profile = _unavailable_profile(
                    execution_id, "events_unavailable", root_pid=root_pid,
                    errors=errors,
                )
                self._remember_completed_locked(profile)
                return profile
            self._active[execution_id] = group
            return None

    def _open_group(self, backend: PerfBackend, execution_id: str, root_pid: int,
                    *, exclude_kernel: bool) -> tuple[_ActiveGroup, bool]:
        fds: dict[str, int] = {}
        errors: dict[str, str] = {}
        leader_fd = -1
        permission_error = False
        for spec in EVENT_SPECS:
            try:
                fd = backend.open_event(
                    spec, root_pid, leader_fd,
                    leader=leader_fd < 0,
                    exclude_kernel=exclude_kernel,
                )
            except BaseException as exc:
                errors[spec.name] = _error_text(exc)
                if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}:
                    permission_error = permission_error or not exclude_kernel
                # A missing leader cannot yield a schedulable group. Do not
                # silently promote another semantic to "cycles".
                if leader_fd < 0:
                    break
                continue
            if leader_fd < 0:
                leader_fd = fd
            fds[spec.name] = fd
        return _ActiveGroup(
            execution_id=execution_id,
            root_pid=root_pid,
            started_at=time.time(),
            fds=fds,
            errors=errors,
            kernel_included=not exclude_kernel,
            process_identity=_process_identity(root_pid),
        ), permission_error

    def finish(self, execution_id: str, *, reason: str = "execution_exited") -> PmuProfile:
        with self._lock:
            previous = self._completed.get(execution_id)
            if previous is not None:
                return previous
            group = self._active.pop(execution_id, None)
            if group is None:
                profile = _unavailable_profile(execution_id, "not_started")
            else:
                try:
                    profile = self._finish_group(group, reason)
                except BaseException as exc:
                    profile = _unavailable_profile(
                        execution_id, "collector_failure", root_pid=group.root_pid,
                        errors=(_error_text(exc),),
                    )
            self._remember_completed_locked(profile)
        return profile

    def _finish_group(self, group: _ActiveGroup, reason: str) -> PmuProfile:
        backend = self._get_backend()
        errors = dict(group.errors)
        leader_fd = group.fds.get("cycles")
        if leader_fd is not None:
            try:
                backend.disable_group(leader_fd)
            except BaseException as exc:
                errors["disable"] = _error_text(exc)
        readings: dict[str, PmuEventReading] = {}
        try:
            for spec in EVENT_SPECS:
                fd = group.fds.get(spec.name)
                if fd is None:
                    readings[spec.name] = PmuEventReading(
                        False, spec.semantics, error=errors.get(spec.name, "unsupported")
                    )
                    continue
                try:
                    raw, enabled, running = backend.read_event(fd)
                except BaseException as exc:
                    error = _error_text(exc)
                    errors[spec.name] = error
                    readings[spec.name] = PmuEventReading(
                        False, spec.semantics, error=error
                    )
                    continue
                if any(type(value) is not int or value < 0 for value in (raw, enabled, running)) or running > enabled:
                    errors[spec.name] = "invalid_counter_times_or_count"
                    readings[spec.name] = PmuEventReading(False, spec.semantics, error=errors[spec.name])
                    continue
                ratio = None if enabled <= 0 else running / enabled
                scaled = (
                    None if running <= 0
                    else float(raw) * (float(enabled) / float(running))
                )
                readings[spec.name] = PmuEventReading(
                    supported=running > 0,
                    semantics=spec.semantics,
                    raw_count=int(raw),
                    scaled_count=scaled,
                    time_enabled_ns=int(enabled),
                    time_running_ns=int(running),
                    running_ratio=ratio,
                    error=None if running > 0 else "not_scheduled",
                )
        finally:
            self._close_group(backend, group)

        supported = [value for value in readings.values() if value.supported]
        ratios = [value.running_ratio for value in supported if value.running_ratio is not None]
        min_ratio = min(ratios) if ratios else None
        all_supported = len(supported) == len(EVENT_SPECS)
        multiplexed = min_ratio is not None and min_ratio < 1.0
        if not supported:
            status, quality_reason = "unavailable", "not_scheduled"
        elif not all_supported:
            status, quality_reason = "partial", "event_unsupported"
        elif min_ratio is None or multiplexed:
            status, quality_reason = (
                "multiplexed",
                (
                    "running_ratio_below_threshold"
                    if min_ratio is None or min_ratio < self.reliable_ratio
                    else "time_running_below_time_enabled"
                ),
            )
        elif errors:
            status, quality_reason = "partial", "collector_error"
        elif reason != "execution_exited":
            status, quality_reason = "partial", reason
        elif not group.kernel_included:
            status, quality_reason = "partial", "kernel_excluded"
        else:
            status, quality_reason = "reliable", reason

        values = {
            name: reading.scaled_count if reading.supported else None
            for name, reading in readings.items()
        }
        derived = {
            "ipc": _ratio(values.get("instructions"), values.get("cycles")),
            "llc_mpki": _ratio(values.get("llc_read_misses"), values.get("instructions"), 1000.0),
            "llc_miss_rate": _ratio(values.get("llc_read_misses"), values.get("llc_read_accesses")),
        }
        if derived["llc_miss_rate"] is not None and derived["llc_miss_rate"] > 1:
            derived["llc_miss_rate"] = None
            if status == "reliable":
                status, quality_reason = "partial", "inconsistent_llc_counts"
        llc_confirmed = bool(
            readings["llc_read_misses"].supported
            and readings["llc_read_accesses"].supported
            and readings["llc_read_misses"].semantics.startswith("PERF_COUNT_HW_CACHE_LL")
            and readings["llc_read_accesses"].semantics.startswith("PERF_COUNT_HW_CACHE_LL")
        )
        return PmuProfile(
            schema=_SCHEMA,
            execution_id=group.execution_id,
            source="perf_event_open",
            mode="counting",
            scope="task-inherit-enable-on-exec",
            root_pid=group.root_pid,
            started_at=group.started_at,
            ended_at=time.time(),
            architecture=platform.machine().lower(),
            pmu_devices=_pmu_devices(),
            llc_semantics=(
                "Linux PERF_TYPE_HW_CACHE last-level cache read accesses/misses; "
                "arm64 maps these to architectural LL_CACHE_RD/LL_CACHE_MISS_RD; "
                "hisi_l3c uncore events are intentionally not substituted"
            ),
            llc_semantics_confirmed=llc_confirmed,
            events=readings,
            derived=derived,
            coverage=PmuCoverage(
                status=status,
                reason=quality_reason,
                running_ratio=min_ratio,
                multiplexed=multiplexed,
                kernel_included=group.kernel_included,
                root_and_future_descendants=True,
                eligible_for_kb=status == "reliable" and llc_confirmed,
            ),
            collector_errors=tuple(f"{key}:{value}" for key, value in sorted(errors.items())),
        )

    @staticmethod
    def _close_group(backend: PerfBackend, group: _ActiveGroup) -> None:
        for fd in group.fds.values():
            try:
                backend.close(fd)
            except BaseException:
                pass

    def take(self, execution_id: str) -> PmuProfile | None:
        # Keep a bounded idempotency tombstone: a delayed duplicate exit must
        # not replace an already-consumed profile with a synthetic not_started
        # result. _remember_completed_locked caps this cache.
        with self._lock:
            return self._completed.get(execution_id)

    def abort(self, execution_id: str) -> PmuProfile:
        return self.finish(execution_id, reason="aborted")

    def close(self) -> None:
        with self._lock:
            execution_ids = tuple(self._active)
        for execution_id in execution_ids:
            self.abort(execution_id)


def _ratio(numerator: float | None, denominator: float | None,
           scale: float = 1.0) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = scale * numerator / denominator
    return value if math.isfinite(value) and value >= 0 else None


def quality_gated_pmu_metrics(profile: Any) -> dict[str, Any]:
    """Recheck raw evidence, not just a producer's reliability label."""
    unavailable = {"ipc": None, "llc_mpki": None, "llc_miss_rate": None, "eligible": False}
    if not isinstance(profile, dict) or any(profile.get(key) != value for key, value in {
        "schema": _SCHEMA, "source": "perf_event_open", "mode": "counting",
        "scope": "task-inherit-enable-on-exec", "llc_semantics_confirmed": True,
    }.items()):
        return unavailable
    if profile.get("llc_semantics_confirmed") is not True:
        return unavailable
    coverage, events = profile.get("coverage"), profile.get("events")
    # asdict() retains the collector's tuple before JSON serialization.
    if not isinstance(coverage, dict) or not isinstance(events, dict) or profile.get("collector_errors") not in ([], ()):
        return unavailable
    expected = {"status": "reliable", "eligible_for_kb": True, "multiplexed": False,
                "kernel_included": True, "root_and_future_descendants": True,
                "running_ratio": 1.0, "reason": "execution_exited"}
    if any(coverage.get(key) != value or (type(value) is bool and coverage.get(key) is not value)
           for key, value in expected.items()) or isinstance(coverage.get("running_ratio"), bool):
        return unavailable
    counts = {}
    for spec in EVENT_SPECS:
        event = events.get(spec.name)
        if not isinstance(event, dict) or event.get("supported") is not True or event.get("semantics") != spec.semantics or event.get("error") is not None:
            return unavailable
        raw, enabled, running = (event.get(key) for key in ("raw_count", "time_enabled_ns", "time_running_ns"))
        if (type(raw) is not int or raw < 0 or type(enabled) is not int or enabled <= 0
                or type(running) is not int or running != enabled or event.get("running_ratio") != 1.0
                or isinstance(event.get("running_ratio"), bool)):
            return unavailable
        counts[spec.name] = raw
    if counts["llc_read_misses"] > counts["llc_read_accesses"]:
        return unavailable
    # Eligible events have no multiplexing, so ratios use raw counts directly.
    return {"ipc": _ratio(counts["instructions"], counts["cycles"]),
            "llc_mpki": _ratio(counts["llc_read_misses"], counts["instructions"], 1000.),
            "llc_miss_rate": _ratio(counts["llc_read_misses"], counts["llc_read_accesses"]),
            "eligible": True}


def _process_identity(pid: int) -> str | None:
    """Return Linux PID start time so PID reuse does not keep a stale group."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def auto_fd_budget(max_active: int, *, reserve: int = 64) -> int:
    """Bound PMU FDs below RLIMIT_NOFILE while preserving unrelated service FDs."""
    wanted = max(_EVENT_FDS, max_active * _EVENT_FDS)
    if resource is None:
        return wanted
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return wanted
    if soft == resource.RLIM_INFINITY:
        return wanted
    return max(_EVENT_FDS, min(wanted, max(0, int(soft) - reserve)))


def write_pmu_profile(path: Path, profile: PmuProfile | dict[str, Any]) -> bool:
    """Best-effort artifact write; PMU telemetry must never fail execution."""
    payload = profile.to_dict() if isinstance(profile, PmuProfile) else dict(profile)
    temporary = path.with_name(path.name + ".next")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


__all__ = [
    "EVENT_SPECS",
    "PmuCollector",
    "PmuCoverage",
    "PmuEventReading",
    "PmuProfile",
    "auto_fd_budget",
    "write_pmu_profile",
]
