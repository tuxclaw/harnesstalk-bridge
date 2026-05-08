"""Session lifecycle, persistence, expiry, and per-session locking."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from bridge.protocol import Session, Turn


class SessionManager:
    """Manage persistent bridge sessions."""

    def __init__(
        self,
        storage_path: str | Path = "state/sessions.json",
    ) -> None:
        self._storage_path = Path(storage_path)
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._busy: set[str] = set()
        self._guard = asyncio.Lock()

    async def load(self) -> None:
        """Load sessions from disk if a state file exists."""
        if not self._storage_path.exists():
            return
        raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
        self._sessions = {}
        self._locks = {}
        for item in raw:
            session = Session.model_validate(item)
            self._sessions[session.session_id] = session
            self._locks[session.session_id] = asyncio.Lock()

    async def save(self) -> None:
        """Persist current sessions to disk."""
        payload = [
            session.model_dump(mode="json")
            for session in sorted(
                self._sessions.values(),
                key=lambda item: item.created_at,
            )
        ]
        await asyncio.to_thread(self._write_sessions_sync, payload)

    def _write_sessions_sync(self, payload: list[dict[str, object]]) -> None:
        """Synchronously persist sessions from a worker thread."""
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._storage_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    async def open_session(
        self,
        target: str,
        purpose: str,
        ttl_seconds: int = 1_800,
        max_turns: int = 8,
        adapter_handle: str | None = None,
    ) -> Session:
        """Create and persist a new session."""
        session = Session(
            target=target,
            purpose=purpose,
            ttl_seconds=ttl_seconds,
            max_turns=max_turns,
            adapter_handle=adapter_handle,
        )
        self._sessions[session.session_id] = session
        self._locks[session.session_id] = asyncio.Lock()
        await self.save()
        return session

    def get(self, session_id: str) -> Session | None:
        """Return a session if it exists."""
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> bool:
        """Mark a session closed and persist the change."""
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return False
        session.closed = True
        await self.save()
        return True

    def list_sessions(self, include_closed: bool = False) -> list[Session]:
        """Return active sessions, optionally including closed ones."""
        self.gc_expired()
        sessions = sorted(
            self._sessions.values(),
            key=lambda item: item.created_at,
        )
        if include_closed:
            return sessions
        return [session for session in sessions if not session.closed]

    def gc_expired(self) -> int:
        """Close sessions whose TTL has expired."""
        now = datetime.now(timezone.utc)
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.is_expired(now)
        ]
        for session_id in expired:
            session = self._sessions[session_id]
            session.closed = True
        return len(expired)

    async def touch(self, session_id: str) -> Session | None:
        """Advance last-used metadata and turn count."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.touch()
        await self.save()
        return session

    async def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> Session | None:
        """Append one replay history turn and persist."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.history.append(Turn(role=role, content=content))
        await self.save()
        return session

    async def try_acquire(self, session_id: str) -> bool:
        """Acquire a session lock without waiting."""
        lock = self._locks.get(session_id)
        if lock is None:
            return False
        async with self._guard:
            if session_id in self._busy:
                return False
            self._busy.add(session_id)
        await lock.acquire()
        return True

    async def release(self, session_id: str) -> None:
        """Release a held session lock."""
        lock = self._locks.get(session_id)
        if lock is None:
            return
        async with self._guard:
            self._busy.discard(session_id)
            if lock.locked():
                lock.release()
