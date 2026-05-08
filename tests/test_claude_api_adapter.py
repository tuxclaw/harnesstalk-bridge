"""Claude API adapter v3 tests."""

from __future__ import annotations

import json

import pytest

from adapters.claude_api import ClaudeApiAdapter
from bridge.protocol import (
    AdapterKind,
    Brief,
    Capability,
    Session,
    TargetStatus,
    TokenMethod,
    Urgency,
    validate_capabilities,
)


def sample_brief(ask: str = "What next?") -> Brief:
    return Brief(goal="Test Claude", tried=["Built a mock"], failing="Need proof", ask=ask)


def sse(payload: dict[str, object]) -> str:
    return "data: " + json.dumps(payload)


class FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b'{"error":{"message":"bad request"}}'


class FakeHealthResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return {"error": {"message": "ok"}}


class FakeClient:
    def __init__(self, lines: list[str] | None = None, health_status: int = 400) -> None:
        self.lines = lines or []
        self.health_status = health_status
        self.payloads: list[dict[str, object]] = []

    def stream(self, *args, **kwargs):
        del args
        self.payloads.append(kwargs["json"])
        return FakeStreamResponse(self.lines)

    async def post(self, *args, **kwargs) -> FakeHealthResponse:
        del args, kwargs
        return FakeHealthResponse(self.health_status)

    async def aclose(self) -> None:
        pass


def stream_lines(*texts: str, input_tokens: int = 3, output_tokens: int = 5) -> list[str]:
    lines = [
        sse({"type": "message_start", "message": {"usage": {"input_tokens": input_tokens}}})
    ]
    lines.extend(
        sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})
        for text in texts
    )
    lines.append(sse({"type": "message_delta", "usage": {"output_tokens": output_tokens}}))
    lines.append(sse({"type": "message_stop"}))
    return lines


def test_capability_validation() -> None:
    adapter = ClaudeApiAdapter("claude", "ANTHROPIC_API_KEY", "claude-test")

    assert adapter.kind == AdapterKind.HTTP_API
    assert adapter.capabilities == frozenset(
        {
            Capability.SESSIONS_NATIVE,
            Capability.STREAMING,
            Capability.EXACT_TOKENS,
            Capability.STRONG_MODEL,
        }
    )
    validate_capabilities(adapter.kind, adapter.capabilities)


@pytest.mark.asyncio
async def test_single_turn_exact_token_counts(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    adapter = ClaudeApiAdapter("claude", "ANTHROPIC_API_KEY", "claude-test")
    adapter._client = FakeClient(stream_lines("hello", input_tokens=11, output_tokens=22))

    chunks = [chunk async for chunk in adapter.consult(sample_brief(), Urgency.QUICK, None, 1, 1000)]

    assert chunks[0].text == "hello"
    assert chunks[-1].tokens_in.value == 11
    assert chunks[-1].tokens_in.method == TokenMethod.EXACT
    assert chunks[-1].tokens_out.value == 22


@pytest.mark.asyncio
async def test_multi_turn_deep_session_keeps_history(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient(stream_lines("turn one") + stream_lines("turn five"))
    adapter = ClaudeApiAdapter("claude", "ANTHROPIC_API_KEY", "claude-test")
    adapter._client = fake
    session = await adapter.open_session("remember turn one")

    for i in range(5):
        chunks = [
            chunk
            async for chunk in adapter.consult(
                sample_brief(f"turn {i + 1}"), Urgency.DEEP, session, 1, 1000
            )
        ]
        assert chunks[-1].type == "done"

    fifth_payload = fake.payloads[-1]
    messages = fifth_payload["messages"]
    serialized = json.dumps(messages)
    assert "turn 1" in serialized
    assert "turn one" in serialized


@pytest.mark.asyncio
async def test_truncation_behavior(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    adapter = ClaudeApiAdapter("claude", "ANTHROPIC_API_KEY", "claude-test")
    adapter._client = FakeClient(stream_lines("abcdef"))

    chunks = [chunk async for chunk in adapter.consult(sample_brief(), Urgency.QUICK, None, 1, 3)]

    assert chunks[0].text.startswith("abc")
    assert "[truncated:" in chunks[0].text
    assert chunks[-1].truncated is True


@pytest.mark.asyncio
async def test_streaming_yields_multiple_text_chunks(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    adapter = ClaudeApiAdapter("claude", "ANTHROPIC_API_KEY", "claude-test")
    adapter._client = FakeClient(stream_lines("one", "two", "three"))

    chunks = [chunk async for chunk in adapter.consult(sample_brief(), Urgency.QUICK, None, 1, 1000)]

    assert [chunk.text for chunk in chunks if chunk.type == "text"] == ["one", "two", "three"]
    assert chunks[-1].type == "done"


@pytest.mark.asyncio
async def test_health_ready_on_400_and_unreachable_on_auth(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    adapter = ClaudeApiAdapter("claude", "ANTHROPIC_API_KEY", "claude-test")
    adapter._client = FakeClient(health_status=400)
    assert (await adapter.health()).status == TargetStatus.READY

    adapter._client = FakeClient(health_status=401)
    assert (await adapter.health()).status == TargetStatus.UNREACHABLE


@pytest.mark.asyncio
async def test_blocker_swaps_to_strong_model(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient(stream_lines("strong"))
    adapter = ClaudeApiAdapter(
        "claude",
        "ANTHROPIC_API_KEY",
        "claude-normal",
        strong_model="claude-strong",
    )
    adapter._client = fake

    chunks = [chunk async for chunk in adapter.consult(sample_brief(), Urgency.BLOCKER, None, 1, 1000)]

    assert chunks[-1].type == "done"
    assert fake.payloads[0]["model"] == "claude-strong"
