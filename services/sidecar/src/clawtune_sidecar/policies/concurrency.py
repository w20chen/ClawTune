from __future__ import annotations

import math
import uuid
from typing import Any

from clawtune_sidecar.admission.leases import LeaseManager
from clawtune_sidecar.contracts.models import ToolBeforeRequest, ToolDecision
from clawtune_sidecar.identity import owner_key
from clawtune_sidecar.policies.base import SchedulingContext


class ConcurrencyPolicy:
    name = "concurrency"
    version = "2"

    def __init__(self, leases: LeaseManager, admission_wait_ms: int) -> None:
        self.leases = leases
        self.admission_wait_ms = admission_wait_ms

    async def decide(self, request: ToolBeforeRequest, context: SchedulingContext) -> ToolDecision:
        lease_id = await self.leases.acquire(
            context.prediction.resource_class,
            self.admission_wait_ms,
            demand_mcpu=_predicted_cpu_millis(context.prediction.tool_resource),
            owner=owner_key(request),
        )
        if lease_id is None:
            return ToolDecision(
                decision_id=str(uuid.uuid4()),
                action="block",
                reason_code="admission_timeout",
                reason="Admission wait limit elapsed before a lease became available.",
                policy_name=self.name,
                policy_version=self.version,
                lease_id=None,
                prediction=context.prediction,
                placement_advice=context.placement,
            )
        return ToolDecision(
            decision_id=str(uuid.uuid4()),
            action="allow",
            reason_code="lease_acquired",
            reason="A bounded concurrency lease was acquired.",
            policy_name=self.name,
            policy_version=self.version,
            lease_id=lease_id,
            prediction=context.prediction,
            placement_advice=context.placement,
        )


def _predicted_cpu_millis(tool_resource: Any) -> int:
    """Translate shared-KB CPU p90 into a weighted admission reservation."""

    if not isinstance(tool_resource, dict):
        return 1_000
    continuous = tool_resource.get("continuous_predictions")
    if not isinstance(continuous, dict):
        return 1_000
    cpu = continuous.get("peak_cpu_cores")
    if not isinstance(cpu, dict):
        return 1_000
    value = cpu.get("conditional_p90")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1_000
    cores = float(value)
    if not math.isfinite(cores) or cores <= 0.0:
        return 1_000
    return max(1, math.ceil(cores * 1_000.0))
