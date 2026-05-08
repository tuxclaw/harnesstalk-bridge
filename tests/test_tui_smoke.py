from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bridge.protocol import (
    AdapterKind,
    AuditEntry,
    Capability,
    Outcome,
    Session,
    Target,
    TargetStatus,
    TokenCount,
    Urgency,
)
from tui.app import AgentBridgeInspector, TuiSettings


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.calls: list[str] = []

    async def list_targets(self) -> list[Target]:
        self.calls.append("list_targets")
        return [
            Target(
                id="hermes",
                model="mimo",
                kind=AdapterKind.CLI_SUBPROCESS,
                status=TargetStatus.READY,
                capabilities=[Capability.SESSIONS_NATIVE, Capability.STREAMING],
                last_checked_at=datetime.now(timezone.utc),
                latency_ms=12,
            )
        ]

    async def list_sessions(self) -> list[Session]:
        self.calls.append("list_sessions")
        return [Session(target="hermes", purpose="test", turn_count=1)]

    async def get_audit(self, *, limit: int = 200, target: str | None = None, since: str | None = None) -> list[AuditEntry]:
        self.calls.append("get_audit")
        return [
            AuditEntry(
                target="hermes",
                urgency=Urgency.QUICK,
                brief_hash="sha256:test",
                elapsed_ms=42,
                tokens_in=TokenCount.estimated(3),
                tokens_out=TokenCount.estimated(4),
                outcome=Outcome.OK,
            )
        ]

    async def close(self) -> None:
        self.calls.append("close")


@pytest.mark.asyncio
async def test_tui_renders_refreshes_and_quits() -> None:
    client = FakeClient()
    app = AgentBridgeInspector(TuiSettings(mouse=False), client=client)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        assert app.query_one("#targets-panel")
        assert app.query_one("#sessions-panel")
        assert app.query_one("#audit-panel")
        await pilot.press("r")
        await pilot.press("q")

    assert client.calls.count("list_targets") >= 2
    assert "close" in client.calls
