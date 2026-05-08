"""Sessions panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from bridge.protocol import Session


class SessionsPanel(Vertical):
    """Read-only active session table."""

    def __init__(self) -> None:
        super().__init__(id="sessions-panel")
        self.table = DataTable(id="sessions-table", cursor_type="row")

    def compose(self) -> ComposeResult:
        yield Static("SESSIONS", classes="panel-title")
        yield self.table

    def on_mount(self) -> None:
        self.table.add_columns("id", "target", "turns", "age", "ttl", "purpose")

    def update_sessions(self, sessions: list[Session], *, stale: bool = False) -> None:
        title = "SESSIONS (stale)" if stale else "SESSIONS"
        self.query_one(".panel-title", Static).update(title)
        cursor_row = self.table.cursor_row
        self.table.clear()
        for session in sessions:
            ttl = _ttl(session.last_used_at, session.ttl_seconds)
            turns = f"{session.turn_count}/{session.max_turns}"
            self.table.add_row(
                _short(session.session_id),
                session.target,
                turns,
                _relative(session.created_at),
                ttl,
                _truncate(session.purpose, 36),
                key=session.session_id,
            )
        if sessions and cursor_row is not None:
            self.table.move_cursor(row=min(cursor_row, len(sessions) - 1), scroll=False)


def _short(value: str) -> str:
    return value if len(value) <= 12 else f"{value[:10]}.."


def _truncate(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: width - 1]}…"


def _relative(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _ttl(last_used_at: datetime, ttl_seconds: int) -> str:
    if last_used_at.tzinfo is None:
        last_used_at = last_used_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    expires = last_used_at + timedelta(seconds=ttl_seconds)
    seconds = int((expires - now).total_seconds())
    if seconds <= 0:
        return "expired"
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m"
