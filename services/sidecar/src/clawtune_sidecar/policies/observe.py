from __future__ import annotations

import uuid

from clawtune_sidecar.contracts.models import ToolBeforeRequest, ToolDecision
from clawtune_sidecar.policies.base import SchedulingContext


class ObserveOnlyPolicy:
    name = "observe-only"
    version = "1"

    async def decide(self, request: ToolBeforeRequest, context: SchedulingContext) -> ToolDecision:
        return ToolDecision(
            decision_id=str(uuid.uuid4()),
            action="allow",
            reason_code="observe_only",
            reason="Observe-only policy records advice but does not block tool execution.",
            policy_name=self.name,
            policy_version=self.version,
            lease_id=None,
            prediction=context.prediction,
            placement_advice=context.placement,
        )
