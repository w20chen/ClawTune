from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from agent_scheduler.identity import OwnerKey


@dataclass
class Lease:
    lease_id: str
    resource_class: str
    demand_mcpu: int
    owner: OwnerKey | None
    execution_id: str | None
    expires_at: float


class LeaseManager:
    def __init__(
        self,
        max_global: int,
        ttl_ms: int,
        cpu_budget_mcpu: int | None = None,
    ) -> None:
        self.max_global = max_global
        self.ttl_ms = ttl_ms
        self.cpu_budget_mcpu = cpu_budget_mcpu
        self._leases: dict[str, Lease] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        resource_class: str,
        wait_ms: int,
        *,
        demand_mcpu: int = 1_000,
        owner: OwnerKey | None = None,
    ) -> str | None:
        demand_mcpu = max(1, demand_mcpu)
        if self.cpu_budget_mcpu is not None:
            # An oversized job must still be able to run alone.  Reserving the
            # full machine budget prevents other weighted leases from being
            # admitted beside it without turning a prediction into a
            # permanent admission deadlock.
            demand_mcpu = min(demand_mcpu, self.cpu_budget_mcpu)
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            async with self._lock:
                self._expire_locked()
                budget_available = (
                    self.cpu_budget_mcpu is None
                    or self._used_mcpu_locked() + demand_mcpu
                    <= self.cpu_budget_mcpu
                )
                if len(self._leases) < self.max_global and budget_available:
                    lease_id = str(uuid.uuid4())
                    self._leases[lease_id] = Lease(
                        lease_id=lease_id,
                        resource_class=resource_class,
                        demand_mcpu=demand_mcpu,
                        owner=owner,
                        execution_id=None,
                        expires_at=time.monotonic() + self.ttl_ms / 1000,
                    )
                    return lease_id
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def release(
        self,
        lease_id: str | None,
        *,
        owner: OwnerKey | None = None,
    ) -> bool:
        if lease_id is None:
            return False
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            if owner is not None and not _owner_satisfies(lease.owner, owner):
                return False
            self._leases.pop(lease_id, None)
            return True

    async def active_count(self) -> int:
        async with self._lock:
            self._expire_locked()
            return len(self._leases)

    async def active_mcpu(self) -> int:
        async with self._lock:
            self._expire_locked()
            return self._used_mcpu_locked()

    async def bind_execution(
        self,
        lease_id: str | None,
        execution_id: str,
        *,
        owner: OwnerKey | None = None,
    ) -> bool:
        if lease_id is None:
            return False
        async with self._lock:
            self._expire_locked()
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            if owner is not None and not _owner_satisfies(lease.owner, owner):
                return False
            if lease.execution_id not in {None, execution_id}:
                return False
            lease.execution_id = execution_id
            # Registered executions are lifecycle-owned.  Expiring a running
            # 30-minute benchmark after the short provisional decision TTL
            # would silently over-admit.  Completion, launcher exit, or exact
            # runtime teardown releases a bound lease instead.
            lease.expires_at = float("inf")
            return True

    async def renew(self, lease_id: str | None) -> bool:
        if lease_id is None:
            return False
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            if lease.execution_id is None:
                lease.expires_at = time.monotonic() + self.ttl_ms / 1000
            return True

    async def release_execution(self, execution_id: str) -> int:
        async with self._lock:
            matches = [
                lease_id
                for lease_id, lease in self._leases.items()
                if lease.execution_id == execution_id
            ]
            for lease_id in matches:
                self._leases.pop(lease_id, None)
            return len(matches)

    async def release_runtime(
        self,
        runtime_id: str,
        gateway_id: str | None = None,
    ) -> int:
        """Release only leases owned by one exact Gateway/Runtime pair."""

        async with self._lock:
            matches = [
                lease_id
                for lease_id, lease in self._leases.items()
                if lease.owner is not None
                and lease.owner[0] == gateway_id
                and lease.owner[1] == runtime_id
            ]
            for lease_id in matches:
                self._leases.pop(lease_id, None)
            return len(matches)

    def _used_mcpu_locked(self) -> int:
        return sum(lease.demand_mcpu for lease in self._leases.values())

    def _expire_locked(self) -> None:
        now = time.monotonic()
        expired = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.execution_id is None and lease.expires_at <= now
        ]
        for lease_id in expired:
            self._leases.pop(lease_id, None)


def _owner_satisfies(
    expected: OwnerKey | None,
    actual: OwnerKey,
) -> bool:
    """Require every identity component captured at admission to match."""

    if expected is None:
        return True
    return all(
        left is None or left == right
        for left, right in zip(expected, actual, strict=True)
    )
