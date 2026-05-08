"""Hermes CLI subprocess adapter."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from adapters.base import Adapter, bound_response, estimate_tokens, format_brief
from bridge.protocol import (
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    Session,
    TargetStatus,
    Urgency,
)

HERMES_DB_PATH = Path.home() / ".hermes" / "state.db"
# Schema version: tracked by column set. If Hermes adds/removes columns,
# update HERMES_REQUIRED_SESSION_COLUMNS and this comment. The adapter
# validates via PRAGMA table_info, not a version number — Hermes doesn't
# store a schema version constant we can read.
HERMES_REQUIRED_SESSION_COLUMNS = frozenset(
    {"id", "started_at", "source", "ended_at", "message_count"}
)


class HermesAdapter(Adapter):
    """Run Hermes through its CLI with native session resume support.

    Discovery notes for schema version 1:
    - Hermes stores session metadata in ``~/.hermes/state.db``; the older
      ``sessions.db`` path can exist as a 0-byte file and is not the store.
    - The adapter depends on the ``sessions`` table columns ``id``,
      ``started_at``, ``source``, ``ended_at``, and ``message_count``.
    - Session ids use the ``YYYYMMDD_HHMMSS_hex6`` format.
    - ``hermes chat --quiet`` / ``-Q`` suppresses the banner and spinner.
    """

    kind = AdapterKind.CLI_SUBPROCESS

    def __init__(
        self,
        target_id: str,
        command: Sequence[str],
        cwd: str | None = None,
        model: str = "hermes",
        strong_model: str | None = None,
    ) -> None:
        self.id = target_id
        self.command = list(command)
        self.cwd = cwd
        self.model = model
        self.strong_model = strong_model
        self._schema_error = self._validate_schema()
        caps = {Capability.SESSIONS_NATIVE}
        if strong_model:
            caps.add(Capability.STRONG_MODEL)
        self.capabilities = frozenset(caps)

    async def health(self) -> TargetStatus:
        """Return ready when cwd and Hermes SQLite schema are usable."""
        if self.cwd and not Path(self.cwd).exists():
            return TargetStatus.UNREACHABLE
        if self._schema_error:
            return TargetStatus.DEGRADED
        return TargetStatus.READY

    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Send one formatted prompt to Hermes via -q flag."""
        prompt = format_brief(brief)
        command = self._command_for(urgency)
        if session and session.adapter_handle:
            command.extend(["--resume", session.adapter_handle])
        # Hermes uses -q for non-interactive query mode.
        command.extend(["-q", prompt])
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except TimeoutError:
            if "proc" in locals():
                proc.kill()
                await proc.wait()
            yield ConsultChunk(
                type="error",
                error_message="Hermes consult timed out",
            )
            return
        except OSError as exc:
            yield ConsultChunk(type="error", error_message=str(exc))
            return

        if proc.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            yield ConsultChunk(
                type="error",
                error_message=message or f"Hermes exited {proc.returncode}",
            )
            return

        response = stdout.decode("utf-8", errors="replace")
        response, truncated = bound_response(response, max_response_bytes)
        yield ConsultChunk(type="text", text=response)
        yield ConsultChunk(
            type="done",
            tokens_in=estimate_tokens(prompt),
            tokens_out=estimate_tokens(response),
            truncated=truncated,
        )

    async def open_session(self, purpose: str) -> Session:
        """Open a native Hermes session and capture its SQLite id."""
        self._ensure_schema_ready()
        command = self._command_for(Urgency.DEEP)
        command.extend(["-q", purpose, "--quiet"])
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            _, stderr = await proc.communicate()
        except OSError as exc:
            raise RuntimeError(f"failed to spawn Hermes: {exc}") from exc

        if proc.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or f"Hermes exited {proc.returncode}")

        handle = await asyncio.to_thread(self._newest_session_id)
        return Session(target=self.id, purpose=purpose, adapter_handle=handle)

    async def close_session(self, session: Session) -> None:
        """Mark the bridge session closed; Hermes owns SQLite lifecycle."""
        session.closed = True

    def _command_for(self, urgency: Urgency) -> list[str]:
        command = list(self.command)
        if urgency == Urgency.BLOCKER and self.strong_model:
            command.extend(["--model", self.strong_model])
        return command

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = HERMES_DB_PATH.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def _validate_schema(self) -> str | None:
        try:
            with self._connect_readonly() as conn:
                rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        except sqlite3.Error as exc:
            return f"Hermes schema unavailable: {exc}"

        columns = {row[1] for row in rows}
        missing = HERMES_REQUIRED_SESSION_COLUMNS - columns
        if missing:
            return (
                f"Hermes schema mismatch: "
                f"sessions missing {sorted(missing)}"
            )
        return None

    def _ensure_schema_ready(self) -> None:
        if self._schema_error:
            raise RuntimeError(self._schema_error)

    def _newest_session_id(self) -> str:
        self._ensure_schema_ready()
        try:
            with self._connect_readonly() as conn:
                row = conn.execute(
                    "SELECT id FROM sessions "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"failed to read Hermes sessions: {exc}") from exc
        if row is None:
            raise RuntimeError("Hermes did not create a session row")
        return str(row[0])
