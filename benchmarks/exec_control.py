"""Authenticated execution lifecycle for bridge-executed task tools.

Terminal Bench tools run inside the task's own Compose container instead of an
OpenClaw sandbox, so the plugin's managed launcher (``clawtune-launch``) never
runs and the sidecar cannot arm PMU counters or bind resource sampling for
them.  This module mirrors the launcher protocol for the host harness:

1. ``register`` mints an execution owned by the real OpenClaw runtime/tool call;
2. ``claim``/``started`` hand the sidecar the container-namespace identity of
   the gated payload process, which the sidecar resolves to a verified host PID
   before arming perf with ``enable_on_exec``;
3. ``exited`` finalizes the PMU profile before the plugin reports completion.

Only the sidecar may turn a container-namespace PID into a host PID.  The
harness never passes a raw container PID to ``perf_event_open``.

The in-container gate emits this identity on its first stdout line and blocks
until the harness writes the release token, so no payload instruction runs
before the collector is armed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: The benchmark harness reports to the same gateway as the plugin runtime.
GATEWAY_ID = "swe-rebench"

GATE_IDENTITY_PREFIX = "CLAWTUNE-GATE"
GATE_IDENTITY_PATTERN = re.compile(r"^CLAWTUNE-GATE (\d+) (\d+) (\d+)$")
GATE_ERROR_PREFIX = "CLAWTUNE-GATE-ERROR"
#: The gate could not produce a trustable identity (exotic image); degrade.
GATE_EXIT_IDENTITY_UNAVAILABLE = 126
#: The harness never released the gate (sidecar path failed); abort.
GATE_EXIT_NOT_RELEASED = 125
#: Container path of the injected gate script.
GATE_CONTAINER_PATH = "/tmp/.clawtune-exec-gate.sh"

#: POSIX-only: Terminal Bench images are arbitrary, so the gate must not depend
#: on Python, bash or non-busybox coreutils.  `read` is a shell builtin and the
#: identity is parsed out of /proc, which every supported image provides.
GATE_SCRIPT = """#!/bin/sh
# ClawTune Terminal Bench execution gate (injected by the host harness).
#
# The first stdout line carries this process's container-namespace identity so
# the host sidecar can resolve the matching host PID and arm perf counters with
# enable_on_exec before the payload runs.  The payload only starts after the
# harness writes the release token on stdin, which happens once the sidecar has
# acknowledged /started.
starttime=$(cut -d')' -f2 /proc/$$/stat 2>/dev/null | awk '{print $20}')
namespace=$(readlink /proc/$$/ns/pid 2>/dev/null)
namespace=${namespace#pid:[}
namespace=${namespace%]}
if [ -z "$starttime" ] || [ -z "$namespace" ]; then
  printf '%s identity_unavailable\\n' "CLAWTUNE-GATE-ERROR"
  exit 126
fi
printf '%s %s %s %s\\n' "CLAWTUNE-GATE" "$$" "$namespace" "$starttime"
IFS= read -r release || exit 125
[ "$release" = "go" ] || exit 125
exec "$@"
"""

# Docker client termination does not cancel an exec in the container. Verify
# the root identity again before killing its descendants and then the root.
ABORT_SCRIPT = r"""
pid=$1
namespace=$(readlink /proc/$pid/ns/pid 2>/dev/null)
[ "$namespace" = "pid:[$2]" ] || exit 0
starttime=$(cut -d')' -f2 /proc/$pid/stat 2>/dev/null | awk '{print $20}')
[ "$starttime" = "$3" ] || exit 0
kill_tree() (
  target=$1
  children=
  read -r children < /proc/$target/task/$target/children 2>/dev/null || :
  for child in $children; do kill_tree "$child"; done
  kill -KILL "$target" 2>/dev/null || :
)
kill_tree "$pid"
"""


def parse_gate_identity(line: str) -> tuple[int, int, int] | None:
    """Parse ``CLAWTUNE-GATE <container-pid> <namespace-inode> <starttime>``.

    Every field must be a positive integer: a malformed identity must degrade
    the call instead of handing an unverifiable PID to the collector.
    """

    match = GATE_IDENTITY_PATTERN.match(line.strip())
    if match is None:
        return None
    pid, namespace, starttime = (int(value) for value in match.groups())
    if pid <= 0 or namespace <= 0 or starttime <= 0:
        return None
    return pid, namespace, starttime


def command_digest(command: str) -> str:
    """Return the ``sha256:`` digest the plugin uses for registrations."""

    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


class SidecarUnavailable(RuntimeError):
    """A sidecar round trip failed; the caller decides how to degrade."""


class ExecutionStartRejected(RuntimeError):
    """The sidecar refused the authenticated execution boundary.

    Unlike a transport failure this is a deliberate refusal (for example the
    required eBPF collector could not be attached), so the gated payload must
    not run unobserved and the tool call fails closed.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _StatusError(SidecarUnavailable):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"sidecar_http_{status}: {detail}")
        self.status = status
        self.detail = detail


class SidecarExecutions:
    """Launcher-protocol client owned by the host harness process."""

    def __init__(
        self,
        port: int,
        *,
        timeout: float = 5.0,
        start_timeout: float = 60.0,
        exit_timeout: float = 15.0,
    ) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.timeout = timeout
        # /started attaches the clause collector on the sidecar, which can take
        # seconds on its first run; the launcher waits the same way.
        self.start_timeout = start_timeout
        self.exit_timeout = exit_timeout

    # ── execution lifecycle ─────────────────────────────────────────────
    def register(
        self,
        *,
        execution_id: str,
        runtime_id: str,
        tool_call_id: str,
        command: str,
        repo: str | None = None,
        gateway_id: str | None = None,
    ) -> str:
        """Register the execution and return its one-time launcher token."""

        payload: dict[str, Any] = {
            "execution_id": execution_id,
            "gateway_id": gateway_id or GATEWAY_ID,
            "runtime_id": runtime_id or None,
            "repo": repo or None,
            "agent_id": None,
            "session_id": None,
            "tool_call_id": tool_call_id,
            "lease_id": None,
            "run_id": None,
            "session_key_hash": None,
            "command_digest": command_digest(command),
            "command": command,
            "workdir": None,
            "host": "gateway",
            "backend": "managed-wrapper",
        }
        data = self._request("POST", "/v2/executions", payload, timeout=self.start_timeout)
        token = data.get("one_time_token")
        if not isinstance(token, str) or not token:
            raise SidecarUnavailable("execution registration returned no token")
        return token

    def claim(self, execution_id: str, token: str, *, launcher_pid: int) -> str:
        """Claim the execution and return the update token."""

        data = self._request(
            "POST",
            "/v2/executions/claim",
            {
                "execution_id": execution_id,
                "token": token,
                "launcher_pid": max(0, launcher_pid),
            },
            timeout=self.start_timeout,
        )
        update_token = data.get("update_token")
        if not isinstance(update_token, str) or not update_token:
            raise SidecarUnavailable("execution claim returned no update token")
        return update_token

    def started(
        self,
        execution_id: str,
        update_token: str,
        *,
        launcher_pid: int,
        container_pid: int,
        namespace_inode: int,
        starttime_ticks: int,
        container_id: str,
    ) -> None:
        """Report the gated root so the sidecar can arm its collectors."""

        payload = {
            "update_token": update_token,
            "launcher_pid": max(0, launcher_pid),
            "child_pid": container_pid,
            "process_starttime_ticks": starttime_ticks,
            "cgroup_path": None,
            "pid_namespace_inode": namespace_inode,
            "container_id": container_id,
            "host_cgroup_gate": False,
            "cgroup_required": False,
        }
        try:
            response = self._request(
                "POST",
                f"/v2/executions/{_quote(execution_id)}/started",
                payload,
                timeout=self.start_timeout,
            )
            if response.get("stored") is not True:
                raise ExecutionStartRejected("sidecar did not acknowledge execution start")
        except _StatusError as exc:
            # 503 means the required collector was not armed; every other
            # explicit refusal is a protocol violation.  Both fail closed.
            if exc.status == 503 or exc.status < 500:
                raise ExecutionStartRejected(exc.detail) from exc
            raise SidecarUnavailable(str(exc)) from exc

    def exited(
        self,
        execution_id: str,
        update_token: str,
        *,
        exit_code: int | None,
        term_signal: int | None = None,
    ) -> None:
        """Finalize the execution (PMU profile, clause artifacts, scope)."""

        self._request(
            "POST",
            f"/v2/executions/{_quote(execution_id)}/exited",
            {
                "update_token": update_token,
                "exit_code": exit_code,
                "signal": term_signal,
            },
            timeout=self.exit_timeout,
        )

    # ── runtime sampling scope ──────────────────────────────────────────
    def store_container_scope(
        self,
        runtime_id: str,
        scope: dict[str, Any],
        *,
        gateway_id: str | None = None,
    ) -> None:
        """Bind the task's client container to this OpenClaw runtime.

        The observer, the tool monitor and the clause collector all resolve
        their target through this per-runtime scope, so parallel tasks keep
        their container/cgroup/PMU mapping one-to-one.
        """

        self._request(
            "POST",
            _scope_endpoint(runtime_id, gateway_id or GATEWAY_ID),
            scope,
        )

    def delete_container_scope(
        self,
        runtime_id: str,
        *,
        gateway_id: str | None = None,
    ) -> None:
        self._request(
            "DELETE",
            _scope_endpoint(runtime_id, gateway_id or GATEWAY_ID),
        )

    # ── transport ───────────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{endpoint}",
            data=data,
            method=method,
            headers={"content-type": "application/json"},
        )
        bearer = os.getenv("CLAWTUNE_TOKEN")
        if bearer:
            request.add_header("authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            preview = ""
            try:
                preview = exc.read().decode("utf-8", errors="replace")[:1000]
            except OSError:
                pass
            if endpoint in {"/v2/executions", "/v2/executions/claim"} and (
                exc.code < 500 or (endpoint.endswith("/claim") and exc.code == 503)
            ):
                raise ExecutionStartRejected(preview or str(exc.reason)) from exc
            raise _StatusError(int(exc.code), preview or exc.reason) from exc
        except (OSError, ValueError) as exc:
            raise SidecarUnavailable(f"{method} {endpoint}: {exc}") from exc
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _scope_endpoint(runtime_id: str, gateway_id: str) -> str:
    return (
        "/v1/gateways/"
        + _quote(gateway_id)
        + "/runtimes/"
        + _quote(runtime_id)
        + "/sandbox-scope"
    )
