"""Consult streaming helpers for MCP progress notifications."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from bridge.protocol import ConsultChunk, ConsultResult, Outcome, TokenCount

log = logging.getLogger(__name__)


async def stream_consult(
    ctx: Any,
    chunks: AsyncIterator[ConsultChunk],
    progress_token: str | int | None,
    *,
    target: str = "",
    model: str = "",
    session_id: str | None = None,
    timeout_s: int | None = None,
    warnings: list[str] | None = None,
) -> ConsultResult:
    """Drain adapter chunks, optionally emit MCP progress, return a result."""
    started = time.monotonic()
    notes = list(warnings or [])
    parts: list[str] = []
    done: ConsultChunk | None = None
    outcome = Outcome.OK

    try:
        if timeout_s is None:
            async for chunk in chunks:
                stop = await _handle_chunk(ctx, chunk, progress_token, parts)
                if stop is not None:
                    done = stop
                    break
        else:
            async with asyncio.timeout(timeout_s):
                async for chunk in chunks:
                    stop = await _handle_chunk(ctx, chunk, progress_token, parts)
                    if stop is not None:
                        done = stop
                        break
    except asyncio.TimeoutError:
        outcome = Outcome.TIMEOUT
        notes.append(f"consult timed out after {timeout_s}s")
        await _aclose(chunks)
    except asyncio.CancelledError:
        await _aclose(chunks)
        raise
    except Exception as exc:
        log.exception("adapter stream raised")
        outcome = Outcome.ERROR
        notes.append(f"{type(exc).__name__}: {exc}")
        await _aclose(chunks)

    if done and done.type == "error":
        outcome = Outcome.ERROR
        notes.append(done.error_message or "adapter error")

    if progress_token is not None and outcome != Outcome.OK:
        await _report_progress(
            ctx,
            progress=len(parts),
            message=f"[{outcome.value}] {notes[-1] if notes else ''}".strip(),
        )

    return ConsultResult(
        response="".join(parts),
        target=target,
        model=model,
        session_id=session_id,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        tokens_in=(
            done.tokens_in
            if done and done.tokens_in is not None
            else TokenCount.unknown()
        ),
        tokens_out=(
            done.tokens_out
            if done and done.tokens_out is not None
            else TokenCount.unknown()
        ),
        outcome=outcome,
        truncated=bool(done and done.truncated),
        warnings=notes,
    )


async def _handle_chunk(
    ctx: Any,
    chunk: ConsultChunk,
    progress_token: str | int | None,
    parts: list[str],
) -> ConsultChunk | None:
    if chunk.type == "text" and chunk.text is not None:
        parts.append(chunk.text)
        if progress_token is not None:
            await _report_progress(ctx, progress=len(parts), message=chunk.text)
        return None
    if chunk.type in {"done", "error"}:
        return chunk
    return None


async def _report_progress(ctx: Any, progress: int, message: str) -> None:
    if ctx is None or not hasattr(ctx, "report_progress"):
        return
    await ctx.report_progress(progress=progress, total=None, message=message)


async def _aclose(chunks: AsyncIterator[ConsultChunk]) -> None:
    aclose = getattr(chunks, "aclose", None)
    if aclose is not None:
        await aclose()
