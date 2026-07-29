from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from agent_scheduler.contracts.models import (
    ExecutionClaimRequest,
    ExecutionClaimResponse,
    ExecutionExitedRequest,
    ExecutionRegistrationRequest,
    ExecutionRegistrationResponse,
    ExecutionStartedRequest,
    ExecutionUpdateResponse,
    ResourceScope,
)


@dataclass
class ExecutionRecord:
    request: ExecutionRegistrationRequest
    token: str
    expires_at: datetime
    update_token: str | None = None
    scope: ResourceScope | None = None
    claimed: bool = False
    launcher_pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    owned_cgroup_path: str | None = None
    trusted_root_pid: int | None = None


class ExecutionRegistry:
    def __init__(self, token_ttl_s: int = 60) -> None:
        self.token_ttl_s = token_ttl_s
        self._by_execution_id: dict[str, ExecutionRecord] = {}
        self._by_token: dict[str, str] = {}

    def register(self, request: ExecutionRegistrationRequest) -> ExecutionRegistrationResponse:
        self._sweep()
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.token_ttl_s)
        record = ExecutionRecord(request=request, token=token, expires_at=expires_at)
        previous = self._by_execution_id.get(request.execution_id)
        if previous is not None:
            self._by_token.pop(previous.token, None)
        self._by_execution_id[request.execution_id] = record
        self._by_token[token] = request.execution_id
        return ExecutionRegistrationResponse(
            execution_id=request.execution_id,
            one_time_token=token,
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )

    def scope(self, execution_id: str) -> ResourceScope | None:
        self._sweep()
        record = self._by_execution_id.get(execution_id)
        return None if record is None else record.scope

    def get(self, execution_id: str) -> ExecutionRecord | None:
        self._sweep()
        return self._by_execution_id.get(execution_id)

    def active(self) -> list[ExecutionRecord]:
        self._sweep()
        return [
            record
            for record in self._by_execution_id.values()
            if record.claimed and record.exit_code is None and record.signal is None
        ]

    def pending_marker(self) -> list[ExecutionRecord]:
        """Return unclaimed marker-backend executions that haven't expired.

        In marker mode, executions are registered but never claimed by
        a launcher.  These records still need Stage-2 telemetry activation
        once the sandbox container scope is discovered.
        """
        self._sweep()
        return [
            record
            for record in self._by_execution_id.values()
            if not record.claimed
            and getattr(record.request, "backend", None) == "marker"
            and record.expires_at > datetime.now(UTC)
        ]

    def claim(self, request: ExecutionClaimRequest) -> ExecutionClaimResponse:
        self._sweep()
        record = self._by_execution_id.get(request.execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution_not_found")
        if record.claimed:
            raise HTTPException(status_code=409, detail="execution_already_claimed")
        if record.token != request.token:
            raise HTTPException(status_code=403, detail="invalid_execution_token")
        if record.expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=410, detail="execution_token_expired")
        record.claimed = True
        record.launcher_pid = request.launcher_pid
        record.update_token = secrets.token_urlsafe(32)
        self._by_token.pop(record.token, None)
        spec = record.request
        return ExecutionClaimResponse(
            execution_id=spec.execution_id,
            update_token=record.update_token,
            command=spec.command,
            command_digest=spec.command_digest,
            workdir=spec.workdir,
            host=spec.host,
            placement=spec.placement,
            profiling=spec.profiling,
        )

    def started(self, execution_id: str, request: ExecutionStartedRequest) -> ExecutionUpdateResponse:
        record = self._require_update(execution_id, request.update_token)
        record.launcher_pid = request.launcher_pid
        record.scope = ResourceScope(
            kind="cgroup-v2" if request.cgroup_path else "pid",
            execution_id=execution_id,
            pid=request.child_pid,
            root_pid=request.child_pid,
            process_start_time=None,
            root_starttime_ticks=request.process_starttime_ticks,
            cgroup_path=request.cgroup_path,
            pid_namespace_inode=request.pid_namespace_inode,
            container_id=request.container_id,
            include_children=True,
            source="claw-launch",
            attribution_source="claw-launch",
        )
        return ExecutionUpdateResponse(stored=True)

    def update_scope(
        self,
        execution_id: str,
        scope: ResourceScope,
        *,
        owned_cgroup_path: str | None = None,
    ) -> None:
        record = self._by_execution_id.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution_not_found")
        record.scope = scope
        if owned_cgroup_path is not None:
            record.owned_cgroup_path = owned_cgroup_path

    def bind_trusted_root(self, execution_id: str, host_pid: int) -> None:
        record = self._by_execution_id.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution_not_found")
        if host_pid <= 0:
            raise HTTPException(status_code=422, detail="invalid_trusted_root_pid")
        if (
            record.trusted_root_pid is not None
            and record.trusted_root_pid != host_pid
        ):
            raise HTTPException(status_code=409, detail="trusted_execution_root_changed")
        record.trusted_root_pid = host_pid

    def exited(self, execution_id: str, request: ExecutionExitedRequest) -> ExecutionUpdateResponse:
        record = self._require_update(execution_id, request.update_token)
        record.exit_code = request.exit_code
        record.signal = request.signal
        return ExecutionUpdateResponse(stored=True)

    def _require_update(self, execution_id: str, update_token: str) -> ExecutionRecord:
        self._sweep()
        record = self._by_execution_id.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution_not_found")
        if not record.claimed or record.update_token is None:
            raise HTTPException(status_code=409, detail="execution_not_claimed")
        if record.update_token != update_token:
            raise HTTPException(status_code=403, detail="invalid_execution_update_token")
        return record

    def _sweep(self) -> None:
        now = datetime.now(UTC)
        expired = [
            execution_id
            for execution_id, record in self._by_execution_id.items()
            if record.expires_at <= now and record.scope is None
        ]
        for execution_id in expired:
            record = self._by_execution_id.pop(execution_id)
            self._by_token.pop(record.token, None)
