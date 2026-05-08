"""Top connection status bar."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.widgets import Static


class BridgeStatusBar(Static):
    """Single-line status display."""

    def __init__(self, version: str) -> None:
        super().__init__(id="status-bar")
        self.version = version
        self.connected = False
        self.last_refresh: datetime | None = None
        self.failures = 0

    def set_state(self, *, connected: bool, failures: int = 0) -> None:
        self.connected = connected
        self.failures = failures
        if connected:
            self.last_refresh = datetime.now(timezone.utc)
        self.refresh_status()

    def refresh_status(self) -> None:
        if self.connected:
            age = self._age_text()
            state = f"[green]connected[/] · {age}"
        elif self.failures >= 3:
            state = "[yellow]reconnecting…[/]"
        else:
            state = "[red]disconnected[/]"
        self.update(f"Agent Bridge — Inspector    v{self.version}    {state}")

    def _age_text(self) -> str:
        if self.last_refresh is None:
            return "never"
        seconds = int((datetime.now(timezone.utc) - self.last_refresh).total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        return f"{seconds // 60}m ago"
