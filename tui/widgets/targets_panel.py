"""Targets panel."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from bridge.protocol import AdapterKind, Capability, Target, TargetStatus

_KIND = {
    AdapterKind.CLI_SUBPROCESS: "CLI",
    AdapterKind.HTTP_API: "HTTP",
    AdapterKind.MCP_PROXY: "MCP",
    AdapterKind.PTY_ATTACHED: "PTY",
}
_STATUS = {
    TargetStatus.READY: "[green]● READY[/]",
    TargetStatus.DEGRADED: "[yellow]● DEGRADED[/]",
    TargetStatus.UNREACHABLE: "[red]● UNREACHABLE[/]",
    TargetStatus.DISABLED: "[grey50]● DISABLED[/]",
}
_CAPS = {
    Capability.SESSIONS_NATIVE: "S·n",
    Capability.SESSIONS_REPLAY: "S·r",
    Capability.SESSIONS_NONE: "S·-",
    Capability.STREAMING: "S·t",
    Capability.EXACT_TOKENS: "E·t",
    Capability.STRONG_MODEL: "T·s",
}


class TargetsPanel(Vertical):
    """Read-only target health table."""

    def __init__(self) -> None:
        super().__init__(id="targets-panel")
        self.table = DataTable(id="targets-table", cursor_type="row")

    def compose(self) -> ComposeResult:
        yield Static("TARGETS", classes="panel-title")
        yield self.table

    def on_mount(self) -> None:
        self.table.add_columns("id", "kind", "status", "latency", "checked", "caps")

    def update_targets(self, targets: list[Target], *, stale: bool = False) -> None:
        title = "TARGETS (stale)" if stale else "TARGETS"
        self.query_one(".panel-title", Static).update(title)
        cursor_row = self.table.cursor_row
        self.table.clear()
        for target in targets:
            self.table.add_row(
                target.id,
                _KIND.get(target.kind, target.kind.value),
                _STATUS.get(target.status, target.status.value),
                f"{target.latency_ms}ms" if target.latency_ms is not None else "-",
                _relative(target.last_checked_at),
                " ".join(_CAPS.get(cap, cap.value) for cap in target.capabilities),
                key=target.id,
            )
        if targets and cursor_row is not None:
            self.table.move_cursor(row=min(cursor_row, len(targets) - 1), scroll=False)


def _relative(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    return f"{seconds // 60}m ago"
