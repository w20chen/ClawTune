from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_scheduler.contracts.models import ResourceScope, ToolBeforeRequest, ToolCompletedEvent


@dataclass(frozen=True)
class DockerExecRecord:
    exec_id: str
    container_id: str | None
    container_name: str | None
    pid: int | None
    cgroup_path: str | None
    command: str | None
    root_starttime_ticks: int | None
    started_monotonic_s: float
    started_wall_s: float


@dataclass(frozen=True)
class _ActiveTool:
    request: ToolBeforeRequest
    started_monotonic_s: float
    started_wall_s: float


class DockerExecObserver:
    """Infer per-tool scopes from Docker exec events without changing OpenClaw.

    This is intentionally best-effort and explicit: matched scopes are labelled
    ``docker-exec-inferred``; callers should keep a sandbox cgroup fallback for
    events that are too short-lived or not implemented as Docker execs.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        docker_socket: str = "/var/run/docker.sock",
        docker_bin: str | None = None,
        container_id: str | None = None,
        container_prefix: str | None = None,
        match_window_s: float = 1.0,
        max_records: int = 2_000,
        on_scope: Callable[[str | None, ResourceScope], bool | None] | None = None,
        autostart: bool = True,
    ) -> None:
        self.enabled = enabled
        self.docker_socket = docker_socket
        self.docker_bin = docker_bin or shutil.which("docker")
        self.container_id = _short_container_id(container_id)
        self.container_prefix = container_prefix
        self.match_window_s = match_window_s
        self.max_records = max_records
        self.on_scope = on_scope
        self._active: dict[str, _ActiveTool] = {}
        self._matched: dict[str, DockerExecRecord] = {}
        self._records: list[DockerExecRecord] = []
        self._consumed_exec_ids: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.enabled and autostart and self.docker_bin is not None:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run_events_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update_container(self, *, container_id: str | None, container_name: str | None = None) -> None:
        with self._lock:
            if container_id:
                self.container_id = _short_container_id(container_id)
            if container_name and self.container_prefix is None:
                self.container_prefix = container_name

    def begin_tool(self, request: ToolBeforeRequest) -> None:
        if not self.enabled or request.tool_name == "exec":
            return
        if request.resource_scope is not None and not _is_shared_runtime_scope(request.resource_scope):
            return
        with self._lock:
            key = _tool_key(request.tool_call_id, request.event_id)
            active = _ActiveTool(
                request=request,
                started_monotonic_s=time.monotonic(),
                started_wall_s=time.time(),
            )
            self._active[key] = active
            record = self._match_record(active, None)
            if record is not None:
                self._matched[key] = record
                self._consumed_exec_ids.add(record.exec_id)
        self._notify_scope(key, active, record)

    def infer_scope(self, event: ToolCompletedEvent) -> ResourceScope | None:
        if not self.enabled or event.execution_id is not None or event.tool_name == "exec":
            return None
        if event.resource_scope is not None and not _is_shared_runtime_scope(event.resource_scope):
            return None
        key = _tool_key(event.tool_call_id, event.event_id)
        with self._lock:
            active = self._active.pop(key, None)
            if active is None and event.tool_call_id is not None:
                active = self._pop_by_tool_call_id(event.tool_call_id)
            if active is None:
                active = self._pop_unique_by_tool_name(event.tool_name)
            if active is None:
                return None
            matched_key = _tool_key(active.request.tool_call_id, active.request.event_id)
            record = self._matched.pop(matched_key, None)
            if record is None:
                record = self._match_record(active, event)
            if record is None:
                return None
            self._consumed_exec_ids.add(record.exec_id)
        return _scope_for_record(record)

    def record_exec_start(
        self,
        *,
        exec_id: str,
        container_id: str | None,
        container_name: str | None = None,
        pid: int | None = None,
        cgroup_path: str | None = None,
        command: str | None = None,
        started_monotonic_s: float | None = None,
        started_wall_s: float | None = None,
    ) -> None:
        if not self._container_matches(container_id, container_name):
            return
        if pid is not None and cgroup_path is None:
            cgroup_path = _read_host_cgroup_path(pid)
        record = DockerExecRecord(
            exec_id=exec_id,
            container_id=_short_container_id(container_id),
            container_name=container_name,
            pid=pid,
            cgroup_path=cgroup_path,
            command=command,
            root_starttime_ticks=_read_pid_starttime_ticks(pid),
            started_monotonic_s=started_monotonic_s if started_monotonic_s is not None else time.monotonic(),
            started_wall_s=started_wall_s if started_wall_s is not None else time.time(),
        )
        notification: tuple[str, _ActiveTool, DockerExecRecord] | None = None
        with self._lock:
            if any(
                existing.exec_id == exec_id and existing.pid is not None
                for existing in self._records
            ):
                return
            self._records.append(record)
            if len(self._records) > self.max_records:
                del self._records[: len(self._records) - self.max_records]
            if record.pid is not None:
                notification = self._match_active_for_record(record)
        if notification is not None:
            self._notify_scope(*notification)

    def _match_record(
        self,
        active: _ActiveTool,
        event: ToolCompletedEvent | None,
    ) -> DockerExecRecord | None:
        ended = time.monotonic()
        candidates = [
            record
            for record in self._records
            if record.exec_id not in self._consumed_exec_ids
            and record.pid is not None
            and active.started_monotonic_s - 0.25 <= record.started_monotonic_s <= ended + self.match_window_s
            and self._container_matches(record.container_id, record.container_name)
        ]
        if not candidates:
            return None
        tool_rank = _tool_command_rank(
            event.tool_name if event is not None else active.request.tool_name
        )
        candidates.sort(
            key=lambda record: (
                tool_rank(record.command),
                abs(record.started_monotonic_s - active.started_monotonic_s),
            )
        )
        return candidates[0]

    def _match_active_for_record(
        self,
        record: DockerExecRecord,
    ) -> tuple[str, _ActiveTool, DockerExecRecord] | None:
        if record.exec_id in self._consumed_exec_ids:
            return None
        candidates = [
            (key, active)
            for key, active in self._active.items()
            if key not in self._matched
            and active.started_monotonic_s - 0.25
            <= record.started_monotonic_s
            <= time.monotonic() + self.match_window_s
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                _tool_command_rank(item[1].request.tool_name)(record.command),
                abs(record.started_monotonic_s - item[1].started_monotonic_s),
            )
        )
        key, active = candidates[0]
        self._matched[key] = record
        self._consumed_exec_ids.add(record.exec_id)
        return key, active, record

    def _notify_scope(
        self,
        key: str,
        active: _ActiveTool,
        record: DockerExecRecord | None,
    ) -> None:
        if record is None or self.on_scope is None:
            return
        scope = _scope_for_record(record)
        if scope is not None:
            bound = self.on_scope(active.request.tool_call_id, scope)
            if bound is False:
                # The process exited before the sampler could take a PID
                # baseline.  Keep the shared fallback and do not later relabel
                # those samples as exact PID attribution.
                with self._lock:
                    self._matched.pop(key, None)

    def _container_matches(self, container_id: str | None, container_name: str | None) -> bool:
        short = _short_container_id(container_id)
        if self.container_id and short:
            return short.startswith(self.container_id) or self.container_id.startswith(short)
        if self.container_prefix and container_name:
            return container_name.startswith(self.container_prefix)
        return self.container_id is None and self.container_prefix is None

    def _pop_by_tool_call_id(self, tool_call_id: str) -> _ActiveTool | None:
        for key, active in list(self._active.items()):
            if active.request.tool_call_id == tool_call_id:
                self._active.pop(key, None)
                return active
        return None

    def _pop_unique_by_tool_name(self, tool_name: str) -> _ActiveTool | None:
        matches = [(key, active) for key, active in self._active.items() if active.request.tool_name == tool_name]
        if len(matches) != 1:
            return None
        key, active = matches[0]
        self._active.pop(key, None)
        return active

    def _run_events_loop(self) -> None:
        if self.docker_bin is None:
            return
        while not self._stop.is_set():
            process = subprocess.Popen(
                _docker_events_command(self.docker_bin),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                if process.stdout is None:
                    return
                for line in process.stdout:
                    if self._stop.is_set():
                        break
                    self._handle_event_line(line)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            self._stop.wait(1.0)

    def _handle_event_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        actor = event.get("Actor") if isinstance(event.get("Actor"), dict) else {}
        attrs = actor.get("Attributes") if isinstance(actor.get("Attributes"), dict) else {}
        exec_id = _as_str(attrs.get("execID")) or _as_str(attrs.get("execId"))
        if not exec_id:
            return
        container_id = (
            _as_str(attrs.get("container"))
            or _as_str(attrs.get("containerID"))
            or _as_str(actor.get("ID"))
            or _as_str(event.get("id"))
        )
        container_name = _as_str(attrs.get("name"))
        if not self._container_matches(container_id, container_name):
            return
        info = self._inspect_exec(exec_id)
        if info is None or _optional_int(info.get("Pid")) in {None, 0}:
            threading.Thread(
                target=self._inspect_exec_until_started,
                kwargs={
                    "exec_id": exec_id,
                    "container_id": container_id,
                    "container_name": container_name,
                },
                daemon=True,
            ).start()
            return
        pid = _optional_int(info.get("Pid")) if info else None
        if container_id is None and info:
            container_id = _as_str(info.get("ContainerID"))
        command = _exec_command(info)
        self.record_exec_start(
            exec_id=exec_id,
            container_id=container_id,
            container_name=container_name,
            pid=pid,
            command=command,
        )

    def _inspect_exec_until_started(
        self,
        *,
        exec_id: str,
        container_id: str | None,
        container_name: str | None,
    ) -> None:
        # exec_create precedes exec_start.  A short high-frequency watcher
        # catches PIDs for native file tools that may exit before an
        # exec_start-only subscriber can inspect them.
        deadline = time.monotonic() + 1.0
        while not self._stop.is_set() and time.monotonic() < deadline:
            info = self._inspect_exec(exec_id)
            if info is None:
                return
            pid = _optional_int(info.get("Pid"))
            if pid not in {None, 0}:
                self.record_exec_start(
                    exec_id=exec_id,
                    container_id=container_id or _as_str(info.get("ContainerID")),
                    container_name=container_name,
                    pid=pid,
                    command=_exec_command(info),
                )
                return
            time.sleep(0.005)

    def _inspect_exec(self, exec_id: str) -> dict[str, Any] | None:
        if not os.path.exists(self.docker_socket):
            return None
        try:
            conn = _UnixHTTPConnection(self.docker_socket, timeout=0.5)
            conn.request("GET", f"/exec/{exec_id}/json")
            response = conn.getresponse()
            body = response.read()
            conn.close()
        except OSError:
            return None
        if response.status != 200:
            return None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _docker_events_command(docker_bin: str) -> list[str]:
    return [
        docker_bin,
        "events",
        "--format",
        "{{json .}}",
        "--filter",
        "type=container",
        "--filter",
        "event=exec_create",
        "--filter",
        "event=exec_start",
    ]


def _exec_command(info: dict[str, Any] | None) -> str | None:
    if not info:
        return None
    config = info.get("ProcessConfig")
    if not isinstance(config, dict):
        return None
    parts: list[str] = []
    entrypoint = config.get("entrypoint")
    if isinstance(entrypoint, str) and entrypoint:
        parts.append(entrypoint)
    args = config.get("arguments")
    if isinstance(args, list):
        parts.extend(str(item) for item in args)
    elif isinstance(args, str) and args:
        parts.append(args)
    return " ".join(parts) if parts else None


def _tool_command_rank(tool_name: str):
    needles = {
        "read": ("openclaw-sandbox-fs", "readlink", "cat ", "sed "),
        "write": ("openclaw-sandbox-fs", "python3", "mv "),
        "edit": ("openclaw-sandbox-fs", "python3", "patch"),
        "apply_patch": ("openclaw-sandbox-fs", "python3", "patch"),
        "process": ("ps ", "kill", "tail", "openclaw"),
    }.get(tool_name, ())

    def rank(command: str | None) -> int:
        if not command:
            return 10
        lowered = command.lower()
        for idx, needle in enumerate(needles):
            if needle in lowered:
                return idx
        return 5

    return rank


def _read_host_cgroup_path(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("0::"):
            continue
        path = line[3:]
        if not path or path == "/":
            return "/sys/fs/cgroup"
        return f"/sys/fs/cgroup{path}"
    return None


def _read_pid_starttime_ticks(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _scope_for_record(record: DockerExecRecord) -> ResourceScope | None:
    if record.pid is None:
        return None
    # Docker exec children share the container cgroup, so sampling that cgroup
    # is not per-tool attribution.  The event PID is the honest isolation unit;
    # include descendants and retain the cgroup only in the observer record for
    # diagnostics.
    return ResourceScope(
        kind="pid",
        execution_id=None,
        pid=record.pid,
        root_pid=record.pid,
        root_starttime_ticks=record.root_starttime_ticks,
        cgroup_path=None,
        container_id=record.container_id,
        include_children=True,
        source="docker-events",
        attribution_source="docker-exec-pid",
    )


def _tool_key(tool_call_id: str | None, event_id: str) -> str:
    return tool_call_id or event_id


def _is_shared_runtime_scope(scope: ResourceScope) -> bool:
    return (
        scope.source in {"openclaw-runtime", "openclaw-sandbox"}
        or scope.attribution_source
        in {"shared-runtime-process", "shared-sandbox-container"}
    )


def _short_container_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lstrip("/")[:12]


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
