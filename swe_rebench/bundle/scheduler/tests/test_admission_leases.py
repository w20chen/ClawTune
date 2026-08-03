from __future__ import annotations

import asyncio

from agent_scheduler.admission.leases import LeaseManager
from agent_scheduler.policies.concurrency import _predicted_cpu_millis


def test_weighted_cpu_budget_blocks_until_capacity_is_released() -> None:
    async def exercise() -> None:
        leases = LeaseManager(
            max_global=128,
            ttl_ms=60_000,
            cpu_budget_mcpu=4_000,
        )
        owner_a = ("gateway-a", "runtime-a", "agent", "session", "run")
        owner_b = ("gateway-b", "runtime-b", "agent", "session", "run")
        first = await leases.acquire(
            "cpu",
            0,
            demand_mcpu=3_000,
            owner=owner_a,
        )
        assert first is not None
        assert await leases.active_mcpu() == 3_000
        assert (
            await leases.acquire(
                "cpu",
                0,
                demand_mcpu=1_001,
                owner=owner_b,
            )
            is None
        )
        assert await leases.release(first, owner=owner_b) is False
        assert await leases.release(first, owner=owner_a) is True
        assert (
            await leases.acquire(
                "cpu",
                0,
                demand_mcpu=1_001,
                owner=owner_b,
            )
            is not None
        )

    asyncio.run(exercise())


def test_bound_execution_does_not_expire_on_provisional_decision_ttl() -> None:
    async def exercise() -> None:
        leases = LeaseManager(max_global=1, ttl_ms=1, cpu_budget_mcpu=1_000)
        owner = ("gateway", "runtime", "agent", "session", "run")
        lease_id = await leases.acquire("cpu", 0, owner=owner)
        assert lease_id is not None
        assert await leases.bind_execution(
            lease_id,
            "execution",
            owner=owner,
        )
        await asyncio.sleep(0.01)
        assert await leases.active_count() == 1
        assert await leases.release_execution("execution") == 1
        assert await leases.active_count() == 0

    asyncio.run(exercise())


def test_runtime_teardown_is_exact_across_gateways() -> None:
    async def exercise() -> None:
        leases = LeaseManager(max_global=4, ttl_ms=60_000)
        for gateway in ("gateway-a", "gateway-b"):
            assert await leases.acquire(
                "cpu",
                0,
                owner=(gateway, "same-runtime", None, None, None),
            )
        assert await leases.release_runtime("same-runtime", "gateway-a") == 1
        assert await leases.active_count() == 1
        assert await leases.release_runtime("same-runtime", "gateway-b") == 1

    asyncio.run(exercise())


def test_cpu_prediction_is_converted_to_integer_millicores() -> None:
    assert (
        _predicted_cpu_millis(
            {
                "continuous_predictions": {
                    "peak_cpu_cores": {"conditional_p90": 1.2341}
                }
            }
        )
        == 1_235
    )
    assert _predicted_cpu_millis(None) == 1_000
    assert _predicted_cpu_millis(
        {"continuous_predictions": {"peak_cpu_cores": {"conditional_p90": None}}}
    ) == 1_000
