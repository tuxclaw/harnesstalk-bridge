"""Core bridge unit tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adapters.base import Adapter
from bridge.audit import AuditLog, AuditQuery
from bridge.protocol import (
    AdapterKind,
    AuditEntry,
    Brief,
    Capability,
    ConsultChunk,
    Outcome,
    TargetStatus,
    TokenCount,
    Urgency,
)
from bridge.registry import Registry
from bridge.sessions import SessionManager


class MockAdapter(Adapter):
    """Simple adapter for registry tests."""

    kind = AdapterKind.HTTP_API
    capabilities = frozenset({Capability.SESSIONS_REPLAY})

    def __init__(self, target_id: str = "mock") -> None:
        self.id = target_id
        self.model = "mock-model"

    async def health(self) -> TargetStatus:
        return TargetStatus.READY

    async def consult(
        self,
        brief,
        urgency,
        session,
        timeout_s,
        max_response_bytes,
    ):
        del brief, urgency, session, timeout_s, max_response_bytes
        yield ConsultChunk(type="text", text="ok")
        yield ConsultChunk(
            type="done",
            tokens_in=TokenCount.estimated(1),
            tokens_out=TokenCount.estimated(1),
        )

    async def open_session(self, purpose):
        del purpose
        raise NotImplementedError

    async def close_session(self, session):
        del session


def sample_brief() -> Brief:
    """Return a valid test brief."""
    return Brief(
        goal="Finish the bridge",
        tried=["Read the spec"],
        failing="Need implementation",
        ask="Return a concise answer",
    )


def test_registry_validates_capabilities() -> None:
    registry = Registry()
    adapter = MockAdapter()
    entry = registry.register(
        target_id="mock",
        adapter=adapter,
        model=adapter.model,
        kind=adapter.kind,
        capabilities=adapter.capabilities,
        max_concurrent=1,
    )

    assert entry.to_target().id == "mock"
    assert registry.list_targets()[0].capabilities == [
        Capability.SESSIONS_REPLAY
    ]


def test_registry_rejects_invalid_capabilities() -> None:
    registry = Registry()
    adapter = MockAdapter()

    with pytest.raises(ValueError):
        registry.register(
            target_id="bad",
            adapter=adapter,
            model=adapter.model,
            kind=AdapterKind.CLI_SUBPROCESS,
            capabilities=frozenset(
                {Capability.SESSIONS_REPLAY, Capability.EXACT_TOKENS}
            ),
        )


@pytest.mark.asyncio
async def test_target_semaphore_busy() -> None:
    registry = Registry()
    adapter = MockAdapter()
    entry = registry.register(
        target_id="mock",
        adapter=adapter,
        model=adapter.model,
        kind=adapter.kind,
        capabilities=adapter.capabilities,
        max_concurrent=1,
    )

    assert await entry.try_acquire() is True
    assert await entry.try_acquire() is False
    await entry.release()
    assert await entry.try_acquire() is True


@pytest.mark.asyncio
async def test_sessions_lifecycle_and_turn_cap(tmp_path) -> None:
    manager = SessionManager(tmp_path / "sessions.json")
    session = await manager.open_session(
        target="mock",
        purpose="test",
        max_turns=1,
    )

    assert manager.get(session.session_id) is not None
    assert await manager.try_acquire(session.session_id) is True
    assert await manager.try_acquire(session.session_id) is False
    await manager.release(session.session_id)

    await manager.touch(session.session_id)
    assert manager.get(session.session_id).is_exhausted()
    assert await manager.close_session(session.session_id) is True
    assert manager.list_sessions() == []


@pytest.mark.asyncio
async def test_audit_writes_jsonl_and_body(tmp_path) -> None:
    audit = AuditLog(
        tmp_path / "audit.jsonl",
        tmp_path / "audit-bodies",
    )
    brief = sample_brief()
    entry = AuditEntry(
        target="mock",
        urgency=Urgency.QUICK,
        brief_hash=brief.fingerprint(),
        elapsed_ms=10,
        tokens_in=TokenCount.estimated(2),
        tokens_out=TokenCount.estimated(3),
        outcome=Outcome.OK,
    )

    await audit.write(entry, brief, "answer", ["note"])
    rows = await audit.query(
        AuditQuery(limit=10, since=datetime.now(timezone.utc))
    )
    assert rows == []

    rows = await audit.query(AuditQuery(limit=10, target="mock"))
    assert rows[0].brief_hash == brief.fingerprint()
    body = audit.read_body(brief.fingerprint())
    assert body is not None
    assert body["response"] == "answer"


@pytest.mark.asyncio
async def test_claude_adapter_keeps_full_history(monkeypatch) -> None:
    """Claude history keeps the full text even when caller output truncates."""
    from adapters.claude_api import ClaudeApiAdapter

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {
                "content": [
                    {"type": "text", "text": "full response text"},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }

    class FakeClient:
        async def post(self, *args, **kwargs) -> FakeResponse:
            del args, kwargs
            return FakeResponse()

        async def aclose(self) -> None:
            pass

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    adapter = ClaudeApiAdapter(
        target_id="claude",
        api_key_env="ANTHROPIC_API_KEY",
        model="claude-test",
    )
    adapter._client = FakeClient()
    session = await adapter.open_session("test")

    chunks = [
        chunk
        async for chunk in adapter.consult(
            sample_brief(),
            Urgency.QUICK,
            session,
            timeout_s=1,
            max_response_bytes=4,
        )
    ]

    assert chunks[0].text.startswith("full")
    assert chunks[-1].truncated is True
    assert adapter._messages[session.session_id][-1]["content"] == (
        "full response text"
    )
