"""Streaming wrapper tests."""

from __future__ import annotations

import asyncio

import pytest

from bridge.protocol import ConsultChunk, Outcome, TokenCount
from bridge.streaming import stream_consult


class FakeContext:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def report_progress(self, progress, total, message) -> None:
        del progress, total
        self.messages.append(message)


async def chunks(items: list[ConsultChunk]):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_non_streaming_adapter_no_progress_when_stream_false() -> None:
    ctx = FakeContext()
    result = await stream_consult(
        ctx,
        chunks([
            ConsultChunk(type="text", text="hello"),
            ConsultChunk(type="done", tokens_in=TokenCount.estimated(1)),
        ]),
        progress_token=None,
        target="mock",
        model="mock",
    )

    assert result.response == "hello"
    assert result.outcome == Outcome.OK
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_streaming_true_progress_in_order() -> None:
    ctx = FakeContext()
    result = await stream_consult(
        ctx,
        chunks([
            ConsultChunk(type="text", text="one"),
            ConsultChunk(type="text", text="two"),
            ConsultChunk(type="done"),
        ]),
        progress_token="tok",
    )

    assert result.response == "onetwo"
    assert ctx.messages == ["one", "two"]


@pytest.mark.asyncio
async def test_streaming_false_accumulates_no_notifications() -> None:
    ctx = FakeContext()
    result = await stream_consult(
        ctx,
        chunks([
            ConsultChunk(type="text", text="one"),
            ConsultChunk(type="text", text="two"),
            ConsultChunk(type="done"),
        ]),
        progress_token=None,
    )

    assert result.response == "onetwo"
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_adapter_error_mid_stream_returns_error() -> None:
    ctx = FakeContext()
    result = await stream_consult(
        ctx,
        chunks([
            ConsultChunk(type="text", text="partial"),
            ConsultChunk(type="error", error_message="boom"),
        ]),
        progress_token="tok",
    )

    assert result.response == "partial"
    assert result.outcome == Outcome.ERROR
    assert "boom" in result.warnings[-1]


@pytest.mark.asyncio
async def test_client_cancellation_closes_adapter_generator() -> None:
    closed = False

    async def generator():
        nonlocal closed
        try:
            yield ConsultChunk(type="text", text="first")
            await asyncio.sleep(10)
        finally:
            closed = True

    task = asyncio.create_task(stream_consult(None, generator(), "tok"))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed is True


@pytest.mark.asyncio
async def test_timeout_mid_stream_returns_partial_timeout() -> None:
    async def generator():
        yield ConsultChunk(type="text", text="partial")
        await asyncio.sleep(1)
        yield ConsultChunk(type="done")

    result = await stream_consult(None, generator(), None, timeout_s=0.01)

    assert result.response == "partial"
    assert result.outcome == Outcome.TIMEOUT
    assert "timed out" in result.warnings[-1]
