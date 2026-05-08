"""Hermes native-session adapter tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from adapters import hermes
from adapters.hermes import HermesAdapter
from bridge.protocol import (
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    Session,
    Turn,
    Urgency,
    validate_capabilities,
)


def sample_brief() -> Brief:
    """Return a valid test brief."""
    return Brief(
        goal="Keep Hermes context native",
        tried=["Opened a bridge session"],
        failing="Replay history must not be prepended",
        ask="Return a concise answer",
    )


def create_hermes_db(path: Path) -> None:
    """Create the minimal Hermes state.db schema used by the adapter."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, "
            "source TEXT, "
            "started_at REAL, "
            "ended_at REAL, "
            "message_count INTEGER, "
            "input_tokens INTEGER, "
            "output_tokens INTEGER, "
            "title TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO sessions "
            "(id, source, started_at, ended_at, message_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260508_041100_abcdef", "cli", 100.0, None, 1),
        )


class FakeProc:
    """Small asyncio subprocess stand-in."""

    def __init__(self, stdout: bytes = b"Hermes says hi", returncode: int = 0):
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def test_hermes_native_capability_accepted() -> None:
    """CLI_SUBPROCESS + SESSIONS_NATIVE is valid per protocol v2.1."""
    validate_capabilities(
        AdapterKind.CLI_SUBPROCESS,
        frozenset({Capability.SESSIONS_NATIVE, Capability.STREAMING}),
    )


@pytest.mark.asyncio
async def test_hermes_consult_uses_native_session_without_replay(
    tmp_path,
    monkeypatch,
) -> None:
    """A consult resumes Hermes and never embeds bridge replay history."""
    db_path = tmp_path / "state.db"
    create_hermes_db(db_path)
    monkeypatch.setattr(hermes, "HERMES_DB_PATH", db_path)
    calls: list[tuple[str, ...]] = []

    async def fake_subprocess(*args, **kwargs):
        del kwargs
        calls.append(tuple(args))
        return FakeProc()

    monkeypatch.setattr(hermes.asyncio, "create_subprocess_exec", fake_subprocess)
    adapter = HermesAdapter("hermes", ["hermes", "chat", "--quiet"])
    session = Session(
        target="hermes",
        purpose="test",
        adapter_handle="20260508_041100_abcdef",
        history=[Turn(role="caller", content="old caller text")],
    )

    chunks = [
        chunk
        async for chunk in adapter.consult(
            sample_brief(),
            Urgency.DEEP,
            session,
            timeout_s=1,
            max_response_bytes=1024,
        )
    ]

    assert isinstance(chunks[0], ConsultChunk)
    assert chunks[0].type == "text"
    assert chunks[0].text == "Hermes says hi"
    assert chunks[1].type == "done"
    command = calls[0]
    prompt = command[command.index("-q") + 1]
    assert "--resume" in command
    assert "20260508_041100_abcdef" in command
    assert "Prior session turns" not in prompt
    assert "old caller text" not in prompt
    assert "Current brief" not in prompt


@pytest.mark.asyncio
async def test_hermes_native_multi_turn_prompt_does_not_grow(
    tmp_path,
    monkeypatch,
) -> None:
    """Native sessions keep bridge history empty across repeated turns."""
    db_path = tmp_path / "state.db"
    create_hermes_db(db_path)
    monkeypatch.setattr(hermes, "HERMES_DB_PATH", db_path)
    calls: list[tuple[str, ...]] = []

    async def fake_subprocess(*args, **kwargs):
        del kwargs
        calls.append(tuple(args))
        return FakeProc(stdout=b"turn response")

    monkeypatch.setattr(hermes.asyncio, "create_subprocess_exec", fake_subprocess)
    adapter = HermesAdapter("hermes", ["hermes", "chat", "--quiet"])
    session = await adapter.open_session("purpose seed")

    prompts: list[str] = []
    for _ in range(8):
        chunks = [
            chunk
            async for chunk in adapter.consult(
                sample_brief(),
                Urgency.DEEP,
                session,
                timeout_s=1,
                max_response_bytes=1024,
            )
        ]
        assert chunks[-1].type == "done"
        assert session.history == []
        command = calls[-1]
        prompts.append(command[command.index("-q") + 1])

    assert session.adapter_handle == "20260508_041100_abcdef"
    assert len(set(prompts)) == 1
    assert all("Prior session turns" not in prompt for prompt in prompts)
    assert all(len(prompt) == len(prompts[0]) for prompt in prompts)
