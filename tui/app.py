"""Textual application for the Agent Bridge read-only inspector."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static

from tui import __version__
from tui.client import BridgeClient
from tui.widgets.audit_panel import AuditPanel, parse_audit_filter
from tui.widgets.sessions_panel import SessionsPanel
from tui.widgets.status_bar import BridgeStatusBar
from tui.widgets.targets_panel import TargetsPanel

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TuiSettings:
    """Runtime settings for the inspector."""

    bridge_url: str = "http://127.0.0.1:7878/mcp"
    poll_targets_seconds: float = 5.0
    poll_sessions_seconds: float = 5.0
    poll_audit_seconds: float = 2.0
    audit_initial_limit: int = 200
    audit_bodies_dir: str = "state/audit-bodies"
    mouse: bool = True


class HelpScreen(ModalScreen[None]):
    """Read-only key reference modal."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Static(
            "KEYS\n"
            "q quit · r refresh · / filter audit · esc clear/close\n"
            "tab/shift-tab cycle focus · 1 targets · 2 sessions · 3 audit\n"
            "↑/↓ or j/k select · enter detail · g/G top/bottom · pgup/pgdn page",
            id="help-modal",
        )

    def action_dismiss(self) -> None:
        self.dismiss()


class AgentBridgeInspector(App[None]):
    """Read-only Textual TUI for Agent Bridge MCP."""

    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("/", "filter_audit", "Filter audit"),
        ("?", "help", "Help"),
        ("escape", "escape", "Close/clear"),
        ("enter", "detail", "Detail"),
        ("1", "focus_targets", "Targets"),
        ("2", "focus_sessions", "Sessions"),
        ("3", "focus_audit", "Audit"),
    ]

    def __init__(
        self,
        settings: TuiSettings | None = None,
        *,
        client: BridgeClient | None = None,
    ) -> None:
        self.settings = settings or TuiSettings()
        super().__init__()
        self.client = client or BridgeClient(self.settings.bridge_url)
        self.status = BridgeStatusBar(__version__)
        self.targets_panel = TargetsPanel()
        self.sessions_panel = SessionsPanel()
        self.audit_panel = AuditPanel(self.settings.audit_bodies_dir)
        self.filter_input = Input(placeholder="audit filter", id="filter-input")
        self.failures = 0
        self._poll_results: dict[str, bool] = {}

    def compose(self) -> ComposeResult:
        yield self.status
        with Vertical(id="main-layout"):
            with Horizontal(id="top-panels"):
                yield self.targets_panel
                yield self.sessions_panel
            yield self.audit_panel
            yield self.filter_input
        yield Footer()

    async def on_mount(self) -> None:
        self.filter_input.display = False
        self._sync_responsive_layout(self.size.width)
        self.set_interval(1.0, self.status.refresh_status)
        self.set_interval(self.settings.poll_targets_seconds, self.refresh_targets)
        self.set_interval(self.settings.poll_sessions_seconds, self.refresh_sessions)
        self.set_interval(self.settings.poll_audit_seconds, self.refresh_audit)
        await self.action_refresh()

    async def action_refresh(self) -> None:
        await self.refresh_targets()
        await self.refresh_sessions()
        await self.refresh_audit()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_responsive_layout(event.size.width)

    async def refresh_targets(self) -> None:
        targets = await self.client.list_targets()
        self._record_poll("targets", self.client.connected)
        if targets is not None:
            self.targets_panel.update_targets(targets, stale=not self.client.connected)

    async def refresh_sessions(self) -> None:
        sessions = await self.client.list_sessions()
        self._record_poll("sessions", self.client.connected)
        if sessions is not None:
            self.sessions_panel.update_sessions(sessions, stale=not self.client.connected)

    async def refresh_audit(self) -> None:
        entries = await self.client.get_audit(limit=self.settings.audit_initial_limit)
        self._record_poll("audit", self.client.connected)
        if entries is not None:
            self.audit_panel.ingest_entries(entries, stale=not self.client.connected)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_filter_audit(self) -> None:
        self.filter_input.display = True
        self.filter_input.value = ""
        self.filter_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter-input":
            return
        parsed = parse_audit_filter(event.value)
        if parsed.ok:
            self.audit_panel.set_filter(parsed.filter)
        else:
            self.audit_panel.set_filter(None, parsed.error)
        self.filter_input.display = False
        self.audit_panel.focus()

    def action_escape(self) -> None:
        if self.filter_input.display:
            self.filter_input.display = False
            return
        if self.audit_panel.audit_filter or self.audit_panel.filter_error:
            self.audit_panel.set_filter(None)
            return
        self.audit_panel.close_detail()

    async def action_detail(self) -> None:
        if self.focused and self.focused.id == "audit-table":
            await self.audit_panel.show_selected_detail()

    def action_focus_targets(self) -> None:
        self.targets_panel.table.focus()

    def action_focus_sessions(self) -> None:
        self.sessions_panel.table.focus()

    def action_focus_audit(self) -> None:
        self.audit_panel.table.focus()

    async def on_unmount(self) -> None:
        await self.client.close()

    def _sync_responsive_layout(self, width: int) -> None:
        self.set_class(width < 100, "narrow")

    def _record_poll(self, name: str, connected: bool) -> None:
        self._poll_results[name] = connected
        if {"targets", "sessions", "audit"} <= self._poll_results.keys():
            cycle_connected = all(self._poll_results.values())
            self.failures = 0 if cycle_connected else self.failures + 1
            self.status.set_state(connected=cycle_connected, failures=self.failures)
            self._poll_results.clear()
