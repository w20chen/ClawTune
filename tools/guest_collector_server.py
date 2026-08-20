"""Authenticated Unix-socket service for guest-local ClawTune collection."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = REPO_ROOT / "services" / "sidecar" / "src"
if str(SIDECAR_SRC) not in sys.path:
    sys.path.insert(0, str(SIDECAR_SRC))

from tool_resource.telemetry import (  # noqa: E402
    ClauseTelemetryCollector,
    _bpf_runtime_diagnostics,
)

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
MAX_COMMAND_BYTES = 128 * 1024


def _prepare_guest_mounts() -> None:
    """Make the guest kernel interfaces required by BCC usable."""
    if os.geteuid() != 0:
        raise RuntimeError("guest collector must run as root")
    # Mount the kernel's canonical tracefs. ClawTune normalizes older packaged
    # BCC bindings whose Python TRACEFS constant still names debugfs.
    tracing = Path("/sys/kernel/tracing")
    tracing.mkdir(parents=True, exist_ok=True)
    exit_tracepoint = tracing / "events" / "sched" / "sched_process_exit" / "id"
    if not exit_tracepoint.exists():
        subprocess.run(
            ["mount", "-t", "tracefs", "tracefs", "/sys/kernel/tracing"],
            check=True,
        )
    # Ubuntu 22.04's BCC 0.18 Python wrapper asks libbcc to resolve
    # tracepoints through the historical debugfs path even when its Python
    # TRACEFS constant is corrected.  A Firecracker guest starts with debugfs
    # unmounted, so provide the legacy view as a second tracefs mount.  Mount
    # debugfs first because /sys itself is read-only inside the pod and the
    # tracing mountpoint must therefore be created on a writable filesystem.
    legacy_tracing = Path("/sys/kernel/debug/tracing")
    legacy_exit_tracepoint = (
        legacy_tracing / "events" / "sched" / "sched_process_exit" / "id"
    )
    if not legacy_exit_tracepoint.exists():
        # Kata may present /sys/kernel/debug as an unusable read-only mask
        # which still reports itself as a mount point.  The tracepoint is the
        # readiness signal; overlay debugfs whenever that signal is absent.
        subprocess.run(
            ["mount", "-t", "debugfs", "debugfs", "/sys/kernel/debug"],
            check=True,
        )
        legacy_tracing.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["mount", "-t", "tracefs", "tracefs", "/sys/kernel/debug/tracing"],
            check=True,
        )
    subprocess.run(
        ["mount", "-o", "remount,rw", "/sys/fs/cgroup"],
        check=True,
    )


def _safe_execution_id(value: Any) -> str:
    execution_id = str(value or "")
    if not execution_id or len(execution_id) > 128:
        raise ValueError("invalid_execution_id")
    if any(not (char.isalnum() or char in "-_.:") for char in execution_id):
        raise ValueError("invalid_execution_id")
    return execution_id


class CollectorService:
    def __init__(self, *, token: str, artifact_root: Path, max_active: int) -> None:
        if len(token) < 32:
            raise ValueError("collector token must contain at least 32 characters")
        self._token = token
        self._artifact_root = artifact_root.resolve()
        self._artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._max_active = max_active
        self._lock = threading.RLock()
        self._active: dict[str, tuple[ClauseTelemetryCollector, Any, Path]] = {}
        self._closed = False

    def _authenticate(self, request: Mapping[str, Any]) -> None:
        supplied = str(request.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise PermissionError("authentication_failed")
        if request.get("v") != PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol_version")

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._authenticate(request)
        op = str(request.get("op") or "")
        if op == "health":
            with self._lock:
                return {
                    "ok": True,
                    "v": PROTOCOL_VERSION,
                    "state": "closed" if self._closed else "ready",
                    "active": len(self._active),
                    "max_active": self._max_active,
                    "bpf_runtime": _bpf_runtime_diagnostics(),
                }
        if op == "begin":
            return self.begin(request)
        if op == "finish":
            return self.finish(request)
        if op == "abort":
            return self.abort(request)
        if op == "shutdown":
            self.close()
            return {"ok": True, "v": PROTOCOL_VERSION, "state": "closed"}
        raise ValueError("unsupported_operation")

    def begin(self, request: Mapping[str, Any]) -> dict[str, Any]:
        execution_id = _safe_execution_id(request.get("execution_id"))
        command = str(request.get("command") or "")
        if not command or len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise ValueError("invalid_command")
        cgroup_path = str(request.get("cgroup_path") or "")
        if not cgroup_path or Path(cgroup_path).resolve() == Path("/sys/fs/cgroup"):
            raise ValueError("invalid_cgroup_path")
        trusted_root_pid = int(request.get("trusted_root_pid") or 0)
        if trusted_root_pid <= 0:
            raise ValueError("invalid_trusted_root_pid")
        repo = str(request.get("repo") or "unknown/unknown")
        artifact_path = self._artifact_root / f"clause-telemetry-{execution_id}.json"
        with self._lock:
            if self._closed:
                raise RuntimeError("collector_service_closed")
            if execution_id in self._active:
                raise ValueError("execution_already_active")
            if len(self._active) >= self._max_active:
                raise RuntimeError("active_execution_limit_reached")
            collector = ClauseTelemetryCollector(
                container_id=None,
                container_executable="docker",
                repo=repo,
                artifact_path=artifact_path,
                cgroup_path=cgroup_path,
                trusted_root_pid=trusted_root_pid,
            )
            try:
                token = collector.begin_tool_call(execution_id, command)
            except BaseException:
                collector.finalize(replay_execution="incomplete")
                raise
            self._active[execution_id] = (collector, token, artifact_path)
        return {
            "ok": True,
            "v": PROTOCOL_VERSION,
            "execution_id": execution_id,
            "artifact_path": str(artifact_path),
            "state": "observing",
        }

    def _pop(self, execution_id: str) -> tuple[ClauseTelemetryCollector, Any, Path]:
        with self._lock:
            active = self._active.pop(execution_id, None)
        if active is None:
            raise ValueError("execution_not_active")
        return active

    def finish(self, request: Mapping[str, Any]) -> dict[str, Any]:
        execution_id = _safe_execution_id(request.get("execution_id"))
        return_code = int(request.get("return_code") or 0)
        collector, token, artifact_path = self._pop(execution_id)
        try:
            call = collector.finish_tool_call(
                token,
                replay_response={
                    "result": str(request.get("result") or ""),
                    "stderr": str(request.get("stderr") or ""),
                    "returncode": return_code,
                },
            )
            collector.finalize(replay_execution="completed")
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            return {
                "ok": True,
                "v": PROTOCOL_VERSION,
                "execution_id": execution_id,
                "artifact_path": str(artifact_path),
                "eligible_for_kb": call.get("eligible_for_kb") is True,
                "telemetry_quality": call.get("telemetry_quality"),
                "collection_validity": artifact.get("collection_validity"),
                "cleanup": artifact.get("cleanup"),
                "loss_total": int(
                    (artifact.get("telemetry_loss_total") or {}).get("total") or 0
                ),
            }
        except BaseException:
            try:
                collector.finalize(replay_execution="incomplete")
            except BaseException:
                pass
            raise

    def abort(self, request: Mapping[str, Any]) -> dict[str, Any]:
        execution_id = _safe_execution_id(request.get("execution_id"))
        collector, _token, artifact_path = self._pop(execution_id)
        collector.finalize(replay_execution="incomplete")
        return {
            "ok": True,
            "v": PROTOCOL_VERSION,
            "execution_id": execution_id,
            "artifact_path": str(artifact_path),
            "state": "aborted",
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = list(self._active.items())
            self._active.clear()
        for _execution_id, (collector, _token, _path) in active:
            try:
                collector.finalize(replay_execution="incomplete")
            except BaseException:
                pass


if hasattr(socketserver, "ThreadingUnixStreamServer"):

    class _Server(socketserver.ThreadingUnixStreamServer):  # type: ignore[attr-defined]
        daemon_threads = True
        allow_reuse_address = False

        def __init__(self, path: str, service: CollectorService) -> None:
            self.service = service
            super().__init__(path, _Handler)

else:

    class _Server:  # pragma: no cover - the service only runs in Linux guests
        def __init__(self, path: str, service: CollectorService) -> None:
            raise RuntimeError("Unix sockets are unavailable on this platform")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            response = {"ok": False, "error": "invalid_request_frame"}
        else:
            try:
                request = json.loads(raw)
                if not isinstance(request, Mapping):
                    raise ValueError("request_must_be_object")
                response = self.server.service.dispatch(request)  # type: ignore[attr-defined]
                if request.get("op") == "shutdown" and response.get("ok") is True:
                    threading.Thread(
                        target=self.server.shutdown, daemon=True  # type: ignore[attr-defined]
                    ).start()
            except BaseException as exc:
                response = {
                    "ok": False,
                    "v": PROTOCOL_VERSION,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        self.wfile.write(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--max-active", type=int, default=16)
    args = parser.parse_args(argv)
    _prepare_guest_mounts()
    token = os.environ.get("CLAWTUNE_GUEST_COLLECTOR_TOKEN", "")
    service = CollectorService(
        token=token,
        artifact_root=args.artifact_root,
        max_active=max(1, args.max_active),
    )
    args.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        args.socket.unlink()
    except FileNotFoundError:
        pass
    server = _Server(str(args.socket), service)
    os.chmod(args.socket, 0o600)

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        service.close()
        try:
            args.socket.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
