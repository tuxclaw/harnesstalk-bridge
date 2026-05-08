"""Server orchestration tests."""

from __future__ import annotations

import asyncio

import pytest

from adapters.base import Adapter
from bridge.audit import AuditLog
from bridge.protocol import (
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    HealthStatus,
    Outcome,
    Session,
    TargetStatus,
    TokenCount,
    Urgency,
)
from bridge.registry import Registry, TargetLimits
from bridge.sessions import SessionManager
from server import AgentBridge


class MockAdapter(Adapter):
    """Mock adapter with controllable delay."""

    kind = AdapterKind.HTTP_API
    capabilities = frozenset({Capability.SESSIONS_REPLAY})

    def __init__(self, delay: float = 0.0) -> None:
        self.id = "mock"
        self.model = "mock-model"
        self.delay = delay

    async def health(self) -> HealthStatus:
        return HealthStatus(status=TargetStatus.READY)

    async def consult(self, brief, urgency, session, timeout_s, max_response_bytes):
        del urgency, session, timeout_s, max_response_bytes
        if self.delay:
            await asyncio.sleep(self.delay)
        yield ConsultChunk(type="text", text=f"answer: {brief.ask}")
        yield ConsultChunk(
            type="done",
            tokens_in=TokenCount.estimated(4),
            tokens_out=TokenCount.estimated(5),
        )

    async def open_session(self, purpose: str) -> Session:
        return Session(target=self.id, purpose=purpose)

    async def close_session(self, session: Session) -> None:
        del session


def brief() -> Brief:
    """Return a valid consult brief."""
    return Brief(
        goal="Test",
        tried=["Set up mock"],
        failing="Nothing",
        ask="What next?",
    )


async def make_bridge(tmp_path, delay: float = 0.0) -> AgentBridge:
    """Build a test bridge with one mock target."""
    adapter = MockAdapter(delay=delay)
    registry = Registry()
    registry.register(
        adapter,
        TargetLimits(max_concurrent=1, max_response_bytes=1024, max_session_turns=1),
    )
    return AgentBridge(
        registry=registry,
        sessions=SessionManager(tmp_path / "sessions.json"),
        audit=AuditLog(tmp_path / "audit.jsonl", tmp_path / "bodies"),
        timeouts={Urgency.QUICK: 1, Urgency.DEEP: 1, Urgency.BLOCKER: 1},
    )


@pytest.mark.asyncio
async def test_consult_with_mock_adapter(tmp_path) -> None:
    bridge = await make_bridge(tmp_path)

    result = await bridge.consult("mock", brief())

    assert result.outcome == Outcome.OK
    assert result.response == "answer: What next?"
    assert result.tokens_in.value == 4


@pytest.mark.asyncio
async def test_session_lifecycle_consult_close(tmp_path) -> None:
    bridge = await make_bridge(tmp_path)
    opened = await bridge.open_session("mock", "testing", max_turns=2)

    first = await bridge.consult("mock", brief(), Urgency.DEEP, opened.session_id)
    second = await bridge.consult("mock", brief(), Urgency.DEEP, opened.session_id)
    closed = await bridge.close_session(opened.session_id)

    assert first.outcome == Outcome.OK
    assert second.outcome == Outcome.OK
    assert closed.closed is True
    assert await bridge.list_sessions() == []


@pytest.mark.asyncio
async def test_turn_cap_returns_rejected(tmp_path) -> None:
    bridge = await make_bridge(tmp_path)
    opened = await bridge.open_session("mock", "testing", max_turns=1)
    await bridge.consult("mock", brief(), Urgency.DEEP, opened.session_id)

    rejected = await bridge.consult("mock", brief(), Urgency.DEEP, opened.session_id)

    assert rejected.outcome == Outcome.REJECTED
    assert "turn cap" in rejected.warnings[0]


@pytest.mark.asyncio
async def test_busy_when_target_saturated(tmp_path) -> None:
    bridge = await make_bridge(tmp_path, delay=0.1)
    first = asyncio.create_task(bridge.consult("mock", brief()))
    await asyncio.sleep(0)

    second = await bridge.consult("mock", brief())
    await first

    assert second.outcome == Outcome.BUSY


@pytest.mark.asyncio
async def test_get_audit_rejects_invalid_since(tmp_path) -> None:
    bridge = await make_bridge(tmp_path)

    with pytest.raises(Exception, match="invalid since value"):
        await bridge.get_audit(since="not-a-date")
