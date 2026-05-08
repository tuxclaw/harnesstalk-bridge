"""Audit panel and filter parser."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from bridge.protocol import AuditEntry, Outcome, TokenMethod, Urgency

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """Parsed audit filter expression."""

    target_terms: tuple[str, ...] = ()
    urgency: Urgency | None = None
    outcome: Outcome | None = None
    outcome_negated: bool = False
    truncated: bool | None = None
    streamed: bool | None = None
    since: timedelta | None = None
    expression: str = ""

    def matches(self, entry: AuditEntry, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self.target_terms and not all(term in entry.target.lower() for term in self.target_terms):
            return False
        if self.urgency is not None and entry.urgency != self.urgency:
            return False
        if self.outcome is not None:
            is_match = entry.outcome == self.outcome
            if is_match == self.outcome_negated:
                return False
        if self.truncated is not None and entry.truncated != self.truncated:
            return False
        if self.streamed is not None and entry.streamed != self.streamed:
            return False
        if self.since is not None:
            ts = entry.ts if entry.ts.tzinfo else entry.ts.replace(tzinfo=timezone.utc)
            if ts < now - self.since:
                return False
        return True

    @property
    def active(self) -> bool:
        return bool(self.expression.strip())


@dataclass(frozen=True, slots=True)
class FilterParseResult:
    """Result of parsing a filter string."""

    filter: AuditFilter | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.filter is not None


def parse_audit_filter(expression: str) -> FilterParseResult:
    """Parse the small audit filter DSL without raising parse exceptions."""
    terms: list[str] = []
    urgency: Urgency | None = None
    outcome: Outcome | None = None
    outcome_negated = False
    truncated: bool | None = None
    streamed: bool | None = None
    since: timedelta | None = None

    for token in expression.split():
        if token == "truncated":
            truncated = True
            continue
        if token == "streamed":
            streamed = True
            continue
        if ":" not in token:
            terms.append(token.lower())
            continue
        key, value = token.split(":", 1)
        if not value:
            return FilterParseResult(error=f"missing value for {key} filter")
        try:
            if key == "urgency":
                urgency = Urgency(value)
            elif key == "outcome":
                if value.startswith("!"):
                    outcome_negated = True
                    value = value[1:]
                if not value:
                    return FilterParseResult(error="missing outcome after !")
                outcome = Outcome(value)
            elif key == "since":
                since = _parse_window(value)
            else:
                return FilterParseResult(error=f"unknown filter: {key}")
        except ValueError as exc:
            return FilterParseResult(error=str(exc))

    return FilterParseResult(
        filter=AuditFilter(
            target_terms=tuple(terms),
            urgency=urgency,
            outcome=outcome,
            outcome_negated=outcome_negated,
            truncated=truncated,
            streamed=streamed,
            since=since,
            expression=expression.strip(),
        )
    )


def apply_audit_filter(entries: list[AuditEntry], audit_filter: AuditFilter | None) -> list[AuditEntry]:
    """Apply a parsed filter to audit entries."""
    if audit_filter is None or not audit_filter.active:
        return entries
    now = datetime.now(timezone.utc)
    return [entry for entry in entries if audit_filter.matches(entry, now=now)]


class AuditPanel(Vertical):
    """Read-only audit table with detail pane support."""

    def __init__(self, bodies_dir: str | Path = "state/audit-bodies") -> None:
        super().__init__(id="audit-panel")
        self.bodies_dir = Path(bodies_dir)
        self.table = DataTable(id="audit-table", cursor_type="row")
        self.detail = Static("Detail: [select an entry to view brief + response]", id="audit-detail")
        self.entries: list[AuditEntry] = []
        self.filtered_entries: list[AuditEntry] = []
        self.audit_filter: AuditFilter | None = None
        self.filter_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("AUDIT", classes="panel-title")
        yield self.table
        yield self.detail

    def on_mount(self) -> None:
        self.table.add_columns("time", "target", "urgency", "outcome", "elapsed", "tokens", "streamed")

    def update_entries(self, entries: list[AuditEntry], *, stale: bool = False) -> None:
        self.entries = entries
        self.filtered_entries = apply_audit_filter(entries, self.audit_filter)
        self._update_header(stale=stale)
        self._render_rows()

    def set_filter(self, audit_filter: AuditFilter | None, error: str | None = None) -> None:
        self.audit_filter = audit_filter
        self.filter_error = error
        self.update_entries(self.entries)

    def show_selected_detail(self) -> None:
        if not self.filtered_entries or self.table.cursor_row is None:
            return
        index = min(self.table.cursor_row, len(self.filtered_entries) - 1)
        entry = self.filtered_entries[index]
        self.detail.update(_detail_text(entry, self._read_body(entry.brief_hash)))

    def close_detail(self) -> None:
        self.detail.update("Detail: [select an entry to view brief + response]")

    def _update_header(self, *, stale: bool = False) -> None:
        title = "AUDIT"
        if self.audit_filter and self.audit_filter.active:
            title += f" — filtered: {self.audit_filter.expression} ({len(self.filtered_entries)} of {len(self.entries)})"
        if self.filter_error:
            title += f" — filter error: {self.filter_error}"
        if stale:
            title += " (stale)"
        self.query_one(".panel-title", Static).update(title)

    def _read_body(self, brief_hash: str) -> dict[str, object] | None:
        path = self.bodies_dir / f"{brief_hash.replace(':', '_')}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("failed to read audit body %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    def _render_rows(self) -> None:
        self.table.clear()
        for entry in self.filtered_entries:
            self.table.add_row(
                _time(entry.ts),
                entry.target,
                entry.urgency.value,
                _outcome(entry),
                _elapsed(entry.elapsed_ms),
                _tokens(entry),
                "[pink]→[/]" if entry.streamed else "",
                key=entry.id,
            )


def _parse_window(value: str) -> timedelta:
    if len(value) < 2 or value[-1] not in {"m", "h"}:
        raise ValueError("since must use minutes or hours, e.g. since:5m")
    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise ValueError("since amount must be an integer") from exc
    if amount <= 0:
        raise ValueError("since amount must be positive")
    return timedelta(minutes=amount) if value[-1] == "m" else timedelta(hours=amount)


def _time(value: datetime) -> str:
    return value.astimezone().strftime("%H:%M:%S")


def _outcome(entry: AuditEntry) -> str:
    color = {
        Outcome.OK: "green",
        Outcome.ERROR: "red",
        Outcome.TIMEOUT: "yellow",
        Outcome.BUSY: "yellow",
        Outcome.REJECTED: "red",
    }.get(entry.outcome, "white")
    text = entry.outcome.value
    if entry.truncated:
        text = f"{text} ✂"
    return f"[{color}]{text}[/]"


def _elapsed(ms: int) -> str:
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _tokens(entry: AuditEntry) -> str:
    if entry.tokens_in.method == TokenMethod.UNKNOWN or entry.tokens_out.method == TokenMethod.UNKNOWN:
        return "-"
    prefix = "~" if TokenMethod.ESTIMATED in {entry.tokens_in.method, entry.tokens_out.method} else ""
    return f"{prefix}{entry.tokens_out.value}/{prefix}{entry.tokens_in.value} tok"


def _detail_text(entry: AuditEntry, body: dict[str, object] | None = None) -> str:
    session = entry.session_id or "-"
    brief = body.get("brief") if body else None
    response = body.get("response") if body else None
    brief_text = _brief_text(brief) if isinstance(brief, dict) else f"  {entry.brief_hash}"
    response_text = str(response) if response is not None else "-"
    return (
        "DETAIL\n"
        f"Target:   {entry.target}        Outcome: {entry.outcome.value}       Elapsed: {_elapsed(entry.elapsed_ms)}\n"
        f"Urgency:  {entry.urgency.value}       Streamed: {'yes' if entry.streamed else 'no'}     Truncated: {'yes' if entry.truncated else 'no'}\n"
        f"Session:  {session}   Tokens: {_tokens(entry)}\n"
        f"Time:     {entry.ts.isoformat()}\n"
        "\nBRIEF\n"
        f"{brief_text}\n"
        "\nRESPONSE\n"
        f"{response_text}\n"
        f"Error:    {entry.error_message or '-'}"
    )


def _brief_text(brief: dict[object, object]) -> str:
    tried = brief.get("tried", [])
    tried_lines = "\n".join(f"    - {item}" for item in tried) if isinstance(tried, list) else "    -"
    return (
        f"  Goal: {brief.get('goal', '-')}\n"
        "  Tried:\n"
        f"{tried_lines}\n"
        f"  Failing: {brief.get('failing', '-')}\n"
        f"  Ask: {brief.get('ask', '-')}"
    )
