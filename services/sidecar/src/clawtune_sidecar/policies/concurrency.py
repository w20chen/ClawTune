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
    version = "3"

    def __init__(self, leases: LeaseManager, admission_wait_ms: int) -> None:
        self.leases = leases
        self.admission_wait_ms = admission_wait_ms

    async def decide(self, request: ToolBeforeRequest, context: SchedulingContext) -> ToolDecision:
        lease_id = await self.leases.acquire(
            context.prediction.resource_class,
            self.admission_wait_ms,
            demand_mcpu=_predicted_cpu_millis(context.prediction.tool or context.prediction.call_prediction),
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


def _predicted_cpu_millis(call_prediction: Any) -> int:
    """Reserve peak-p90, then average-p90; 1 core is a policy default only.

    No native KB fields or clause diagnostics are admission inputs. Empirical
    quantiles remain uncalibrated estimates, not capacity guarantees.
    """
    if hasattr(call_prediction, "model_dump"):
        call_prediction = call_prediction.model_dump()
    if not isinstance(call_prediction, dict) or call_prediction.get("scope") != "tool_call":
        return 1_000
    targets = call_prediction.get("targets", {})
    if not isinstance(targets, dict):
        return 1_000
    for target in ("cpu_peak_cores", "cpu_avg_cores"):
        cpu = targets.get(target)
        if not isinstance(cpu, dict) or cpu.get("status") != "available" or cpu.get("unit") != "cores":
            continue
        if target == "cpu_peak_cores" and call_prediction.get("cpu_peak_window_ms") != 500:
            continue
        value = cpu.get("p90")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            continue
        return max(1, math.ceil(value * 1_000.0))
    return 1_000
