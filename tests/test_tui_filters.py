from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bridge.protocol import AuditEntry, Outcome, TokenCount, Urgency
from tui.widgets.audit_panel import apply_audit_filter, parse_audit_filter


def entry(
    target: str,
    *,
    urgency: Urgency = Urgency.QUICK,
    outcome: Outcome = Outcome.OK,
    truncated: bool = False,
    streamed: bool = False,
    age_minutes: int = 0,
) -> AuditEntry:
    return AuditEntry(
        ts=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        target=target,
        urgency=urgency,
        brief_hash=f"sha256:{target}",
        elapsed_ms=10,
        tokens_in=TokenCount.estimated(3),
        tokens_out=TokenCount.estimated(5),
        outcome=outcome,
        truncated=truncated,
        streamed=streamed,
    )


def test_parse_and_apply_all_filter_operators() -> None:
    entries = [
        entry("hermes", urgency=Urgency.BLOCKER, streamed=True, age_minutes=3),
        entry("claude", urgency=Urgency.DEEP, outcome=Outcome.ERROR, truncated=True, age_minutes=2),
        entry("hermes-old", urgency=Urgency.BLOCKER, outcome=Outcome.TIMEOUT, age_minutes=90),
    ]

    parsed = parse_audit_filter("hermes urgency:blocker outcome:!timeout streamed since:5m")

    assert parsed.ok
    assert [item.target for item in apply_audit_filter(entries, parsed.filter)] == ["hermes"]


def test_truncated_filter_matches_boolean_flag() -> None:
    entries = [entry("a", truncated=True), entry("b", truncated=False)]
    parsed = parse_audit_filter("truncated")

    assert parsed.ok
    assert [item.target for item in apply_audit_filter(entries, parsed.filter)] == ["a"]


def test_bad_filter_syntax_returns_clear_error() -> None:
    parsed = parse_audit_filter("outcome:! since:5x unknown:value")

    assert not parsed.ok
    assert parsed.error is not None
    assert "missing outcome" in parsed.error
