"""Long-lived client for the pinned mvdan shell clause adapter."""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
from typing import Any


PARSER_NAME = "mvdan.cc/sh/v3"
PARSER_VERSION = "v3.13.1"
ADAPTER_PROTOCOL_VERSION = 3
REQUIRED_CAPABILITIES = frozenset({"structural_context", "word_intents"})
_BUILD_SCRIPT = Path(__file__).with_name("_mvdan_adapter") / "build.sh"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class MvdanClientError(RuntimeError):
    """The mvdan adapter is unavailable or violated its protocol."""


class _RetryableProcessError(MvdanClientError):
    pass


def default_binary_path() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return (
        cache
        / "agent-sched-bench"
        / (
            f"mvdan-clause-adapter-protocol-{ADAPTER_PROTOCOL_VERSION}"
            f"-mvdan-{PARSER_VERSION}"
        )
    )


class MvdanClient:
    """Serialize requests through one adapter process and restart it on failure."""

    def __init__(
        self,
        binary_path: Path | None = None,
        *,
        timeout_s: float = 5.0,
        max_restarts: int = 1,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_restarts < 0:
            raise ValueError("max_restarts must be non-negative")
        self.binary_path = binary_path or default_binary_path()
        self.timeout_s = timeout_s
        self.max_restarts = max_restarts
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout_buffer = b""
        self._next_id = 0
        self._lock = threading.Lock()
        self.start_count = 0

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def __enter__(self) -> MvdanClient:
        with self._lock:
            self._ensure_started()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def parse(self, command: str) -> dict[str, Any]:
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        with self._lock:
            for attempt in range(self.max_restarts + 1):
                try:
                    self._ensure_started()
                    return self._exchange("parse", command)
                except _RetryableProcessError:
                    self._stop()
                    if attempt == self.max_restarts:
                        raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        if not self.binary_path.is_file() or not os.access(self.binary_path, os.X_OK):
            raise MvdanClientError(
                f"mvdan adapter is missing at {self.binary_path}; "
                f"run bundled builder {_BUILD_SCRIPT}"
            )
        try:
            self._process = subprocess.Popen(
                [str(self.binary_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as error:
            raise MvdanClientError(
                f"failed to launch mvdan adapter at {self.binary_path}: {error}"
            ) from error
        self._stdout_buffer = b""
        self.start_count += 1
        try:
            response = self._exchange("handshake", "")
        except Exception:
            self._stop()
            raise
        parser = response.get("parser")
        expected = {"name": PARSER_NAME, "version": PARSER_VERSION}
        if parser != expected:
            self._stop()
            raise MvdanClientError(
                f"mvdan parser version mismatch: expected {expected}, got {parser}"
            )
        protocol = response.get("protocol")
        version = protocol.get("version") if isinstance(protocol, dict) else None
        capabilities = (
            protocol.get("capabilities") if isinstance(protocol, dict) else None
        )
        if version != ADAPTER_PROTOCOL_VERSION or not isinstance(
            capabilities, list
        ):
            self._stop()
            raise MvdanClientError(
                "mvdan adapter protocol mismatch: expected version "
                f"{ADAPTER_PROTOCOL_VERSION} with capabilities "
                f"{sorted(REQUIRED_CAPABILITIES)}, got {protocol}"
            )
        missing = REQUIRED_CAPABILITIES.difference(capabilities)
        if missing:
            self._stop()
            raise MvdanClientError(
                "mvdan adapter lacks required capabilities: "
                f"{sorted(missing)}; advertised {capabilities}"
            )

    def _exchange(self, operation: str, command: str) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise _RetryableProcessError("mvdan adapter is not running")
        request_id = self._next_id
        self._next_id += 1
        payload = (
            json.dumps(
                {"id": request_id, "op": operation, "command": command},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        deadline = time.monotonic() + self.timeout_s
        self._write(process.stdin.fileno(), payload, deadline)
        line = self._readline(process.stdout.fileno(), deadline)
        try:
            response = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _RetryableProcessError(
                f"mvdan adapter returned invalid JSON: {error}"
            ) from error
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise _RetryableProcessError(
                f"mvdan adapter response id mismatch for request {request_id}"
            )
        return response

    def _write(self, file_descriptor: int, payload: bytes, deadline: float) -> None:
        written = 0
        while written < len(payload):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise _RetryableProcessError("mvdan adapter request timed out")
            _, ready, _ = select.select([], [file_descriptor], [], timeout)
            if not ready:
                raise _RetryableProcessError("mvdan adapter request timed out")
            try:
                written += os.write(file_descriptor, payload[written:])
            except BrokenPipeError as error:
                raise _RetryableProcessError("mvdan adapter crashed") from error

    def _readline(self, file_descriptor: int, deadline: float) -> bytes:
        while b"\n" not in self._stdout_buffer:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise _RetryableProcessError("mvdan adapter request timed out")
            ready, _, _ = select.select([file_descriptor], [], [], timeout)
            if not ready:
                raise _RetryableProcessError("mvdan adapter request timed out")
            chunk = os.read(file_descriptor, 64 * 1024)
            if not chunk:
                raise _RetryableProcessError("mvdan adapter crashed")
            self._stdout_buffer += chunk
            if len(self._stdout_buffer) > _MAX_RESPONSE_BYTES:
                raise _RetryableProcessError("mvdan adapter response is too large")
        line, self._stdout_buffer = self._stdout_buffer.split(b"\n", 1)
        return line

    def _stop(self) -> None:
        process, self._process = self._process, None
        self._stdout_buffer = b""
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def ensure_compatible_adapter() -> Path:
    """Validate the exact runtime cache, rebuilding it atomically if stale."""

    binary_path = default_binary_path()
    try:
        with MvdanClient(binary_path):
            return binary_path
    except MvdanClientError:
        try:
            subprocess.run(
                [str(_BUILD_SCRIPT)],
                cwd=_BUILD_SCRIPT.parent,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise MvdanClientError(
                f"failed to build compatible mvdan adapter with {_BUILD_SCRIPT}"
            ) from error
        with MvdanClient(binary_path):
            return binary_path


_default_client: MvdanClient | None = None
_default_client_lock = threading.Lock()


def get_client() -> MvdanClient:
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = MvdanClient()
        return _default_client


def _close_default_client() -> None:
    if _default_client is not None:
        _default_client.close()


atexit.register(_close_default_client)


__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "MvdanClient",
    "MvdanClientError",
    "PARSER_NAME",
    "PARSER_VERSION",
    "REQUIRED_CAPABILITIES",
    "default_binary_path",
    "ensure_compatible_adapter",
    "get_client",
]
