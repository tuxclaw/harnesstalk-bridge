"""Registry v3 health state-machine tests."""

from __future__ import annotations

import asyncio

import pytest

from bridge.protocol import (
    Adapter,
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    HealthStatus,
    Session,
    TargetStatus,
    Urgency,
)
from bridge.registry import HealthConfig, Registry, TargetLimits


class HealthAdapter(Adapter):
    kind = AdapterKind.HTTP_API
    capabilities = frozenset({Capability.SESSIONS_NONE})

    def __init__(
        self,
        statuses: list[TargetStatus],
        delay: float = 0.0,
        target_id: str = "health",
    ) -> None:
        self.id = target_id
        self.model = "health-model"
        self.statuses = list(statuses)
        self.delay = delay
        self.calls = 0

    async def health(self) -> HealthStatus:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        status = self.statuses.pop(0) if self.statuses else TargetStatus.READY
        return HealthStatus(status=status, error_message=None if status == TargetStatus.READY else "down")

    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ):
        del brief, urgency, session, timeout_s, max_response_bytes
        yield ConsultChunk(type="done")

    async def open_session(self, purpose: str) -> Session:
        del purpose
        raise NotImplementedError

    async def close_session(self, session: Session) -> None:
        del session


@pytest.mark.asyncio
async def test_lazy_checking_lazy_happy_path() -> None:
    registry = Registry(HealthConfig(lazy_cache_seconds=60))
    adapter = HealthAdapter([TargetStatus.READY])
    registry.register(adapter, TargetLimits())

    targets = await registry.list_targets()

    assert targets[0].status == TargetStatus.READY
    assert adapter.calls == 1
    await registry.aclose()


@pytest.mark.asyncio
async def test_unhealthy_enters_polled() -> None:
    registry = Registry(HealthConfig(polled_interval_seconds=60))
    adapter = HealthAdapter([TargetStatus.DEGRADED])
    registry.register(adapter, TargetLimits())

    targets = await registry.list_targets()
    second = await registry.list_targets()

    assert targets[0].status == TargetStatus.DEGRADED
    assert second[0].status == TargetStatus.DEGRADED
    assert adapter.calls == 1
    await registry.aclose()


@pytest.mark.asyncio
async def test_polled_stays_polled_on_continued_failure() -> None:
    registry = Registry(HealthConfig(polled_interval_seconds=0.01))
    adapter = HealthAdapter([
        TargetStatus.DEGRADED,
        TargetStatus.UNREACHABLE,
        TargetStatus.UNREACHABLE,
        TargetStatus.UNREACHABLE,
    ])
    registry.register(adapter, TargetLimits())

    await registry.list_targets()
    await asyncio.sleep(0.03)
    targets = await registry.list_targets()

    assert targets[0].status == TargetStatus.UNREACHABLE
    assert adapter.calls >= 2
    await registry.aclose()


@pytest.mark.asyncio
async def test_polled_recovers_to_lazy_on_single_ready() -> None:
    registry = Registry(HealthConfig(polled_interval_seconds=0.01))
    adapter = HealthAdapter([TargetStatus.DEGRADED, TargetStatus.READY])
    registry.register(adapter, TargetLimits())

    await registry.list_targets()
    await asyncio.sleep(0.03)
    targets = await registry.list_targets()
    calls_after_recovery = adapter.calls
    await asyncio.sleep(0.03)

    assert targets[0].status == TargetStatus.READY
    assert adapter.calls == calls_after_recovery
    await registry.aclose()


@pytest.mark.asyncio
async def test_lazy_cache_ttl_respected() -> None:
    registry = Registry(HealthConfig(lazy_cache_seconds=60))
    adapter = HealthAdapter([TargetStatus.READY, TargetStatus.DEGRADED])
    registry.register(adapter, TargetLimits())

    await registry.list_targets()
    await registry.list_targets()

    assert adapter.calls == 1
    await registry.aclose()


@pytest.mark.asyncio
async def test_concurrent_list_targets_single_flight() -> None:
    registry = Registry(HealthConfig(lazy_cache_seconds=60))
    adapter = HealthAdapter([TargetStatus.READY], delay=0.02)
    registry.register(adapter, TargetLimits())

    first, second = await asyncio.gather(registry.list_targets(), registry.list_targets())

    assert first[0].status == TargetStatus.READY
    assert second[0].status == TargetStatus.READY
    assert adapter.calls == 1
    await registry.aclose()


@pytest.mark.asyncio
async def test_health_timeout_becomes_degraded() -> None:
    registry = Registry(HealthConfig(check_timeout_seconds=0.01))
    adapter = HealthAdapter([TargetStatus.READY], delay=0.1)
    registry.register(adapter, TargetLimits())

    targets = await registry.list_targets()

    assert targets[0].status == TargetStatus.DEGRADED
    assert "timed out" in (targets[0].note or "")
    await registry.aclose()
