"""FastMCP server for Agent Bridge."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from adapters.base import Adapter, bound_response
from adapters.claude_api import ClaudeApiAdapter
from adapters.hermes import HermesAdapter
from adapters.openclaw import OpenClawAdapter
from bridge.audit import AuditLog, AuditQuery
from bridge.config import load_config
from bridge.protocol import (
    AuditEntry,
    Brief,
    Capability,
    CloseSessionResult,
    ConsultChunk,
    ConsultResult,
    GetAuditRequest,
    OpenSessionResult,
    Outcome,
    Session,
    Target,
    TokenCount,
    Urgency,
)
from bridge.registry import Registry, TargetEntry
from bridge.sessions import SessionManager

try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
except ModuleNotFoundError:  # pragma: no cover - local tests avoid FastMCP
    FastMCP = None  # type: ignore[assignment]

    class ToolError(RuntimeError):
        """Fallback ToolError used when FastMCP is not installed."""


class AgentBridge:
    """Bridge orchestration independent of the MCP transport."""

    def __init__(
        self,
        registry: Registry,
        sessions: SessionManager,
        audit: AuditLog,
        timeouts: dict[Urgency, int],
    ) -> None:
        self.registry = registry
        self.sessions = sessions
        self.audit = audit
        self.timeouts = timeouts

    async def list_targets(self) -> list[Target]:
        """Return configured targets."""
        return self.registry.list_targets()

    async def open_session(
        self,
        target: str,
        purpose: str,
        max_turns: int | None = None,
    ) -> OpenSessionResult:
        """Create a persistent session."""
        entry = self._entry(target)
        target_session = await entry.adapter.open_session(purpose)
        session = await self.sessions.open_session(
            target=target,
            purpose=purpose,
            ttl_seconds=entry.config.session_ttl_seconds,
            max_turns=max_turns or entry.config.max_session_turns,
            adapter_handle=target_session.adapter_handle,
        )
        return OpenSessionResult(
            session_id=session.session_id,
            ttl_seconds=session.ttl_seconds,
            max_turns=session.max_turns,
        )

    async def close_session(self, session_id: str) -> CloseSessionResult:
        """Close a persistent session."""
        session = self.sessions.get(session_id)
        if session:
            entry = self.registry.get(session.target)
            if entry:
                await entry.adapter.close_session(session)
        closed = await self.sessions.close_session(session_id)
        return CloseSessionResult(session_id=session_id, closed=closed)

    async def list_sessions(self) -> list[Session]:
        """Return active sessions."""
        return self.sessions.list_sessions()

    async def get_audit(
        self,
        limit: int = 50,
        target: str | None = None,
        since: str | None = None,
    ) -> list[AuditEntry]:
        """Query audit entries."""
        request = GetAuditRequest(
            limit=limit,
            target=target,
            since=_parse_since(since),
        )
        return await self.audit.query(
            AuditQuery(
                limit=request.limit,
                target=request.target,
                since=request.since,
            )
        )

    async def consult(
        self,
        target: str,
        brief: Brief | dict[str, Any],
        urgency: Urgency | str = Urgency.QUICK,
        session_id: str | None = None,
        stream: bool = False,
    ) -> ConsultResult:
        """Consult one target and return the final result."""
        del stream
        entry = self._entry(target)
        parsed_brief = (
            brief if isinstance(brief, Brief) else Brief.model_validate(brief)
        )
        parsed_urgency = Urgency(urgency)
        warnings: list[str] = []
        session = self._session_for(entry, session_id)
        session_locked = False
        target_locked = False

        if Capability.SESSIONS_NONE in entry.capabilities:
            if parsed_urgency != Urgency.QUICK or session_id:
                warnings.append(
                    "deep degraded to quick: target has SESSIONS_NONE"
                )
            session = None
            parsed_urgency = Urgency.QUICK

        if session and session.is_exhausted():
            result = self._synthetic_result(
                entry,
                parsed_urgency,
                session,
                Outcome.REJECTED,
                "session turn cap reached",
                warnings,
            )
            await self._write_audit(result, parsed_brief, parsed_urgency)
            return result

        if session:
            session_locked = await self.sessions.try_acquire(
                session.session_id
            )
            if not session_locked:
                result = self._synthetic_result(
                    entry,
                    parsed_urgency,
                    session,
                    Outcome.BUSY,
                    "session is busy",
                    warnings,
                )
                await self._write_audit(result, parsed_brief, parsed_urgency)
                return result

        target_locked = await entry.try_acquire()
        if not target_locked:
            result = self._synthetic_result(
                entry,
                parsed_urgency,
                session,
                Outcome.BUSY,
                "target is busy",
                warnings,
            )
            await self._write_audit(result, parsed_brief, parsed_urgency)
            if session_locked and session:
                await self.sessions.release(session.session_id)
            return result

        started = time.monotonic()
        timeout_s = self.timeouts[parsed_urgency]
        try:
            result = await asyncio.wait_for(
                self._consume_adapter(
                    entry,
                    parsed_brief,
                    parsed_urgency,
                    session,
                    timeout_s,
                    warnings,
                ),
                timeout=timeout_s + 5,
            )
        except TimeoutError:
            result = ConsultResult(
                response="",
                target=target,
                model=entry.config.model,
                session_id=session.session_id if session else None,
                elapsed_ms=_elapsed_ms(started),
                outcome=Outcome.TIMEOUT,
                warnings=warnings,
            )
        finally:
            await entry.release()
            if session_locked and session:
                await self.sessions.release(session.session_id)

        result.elapsed_ms = _elapsed_ms(started)
        if session and result.outcome == Outcome.OK:
            await self.sessions.touch(session.session_id)
            if Capability.SESSIONS_REPLAY in entry.capabilities:
                await self.sessions.add_turn(
                    session.session_id,
                    "caller",
                    _brief_summary(parsed_brief),
                )
                await self.sessions.add_turn(
                    session.session_id,
                    "target",
                    result.response,
                )
        await self._write_audit(result, parsed_brief, parsed_urgency)
        return result

    async def close(self) -> None:
        """Close resources owned by registered adapters."""
        for _, entry in self.registry.items():
            await entry.adapter.close()

    def _entry(self, target: str) -> TargetEntry:
        entry = self.registry.get(target)
        if entry is None:
            raise ToolError(f"unknown target: {target}")
        return entry

    def _session_for(
        self,
        entry: TargetEntry,
        session_id: str | None,
    ) -> Session | None:
        if session_id is None:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            raise ToolError(f"unknown session: {session_id}")
        if session.target != entry.target_id:
            raise ToolError("session target does not match consult target")
        if session.is_expired():
            raise ToolError(f"session expired or closed: {session_id}")
        return session

    async def _consume_adapter(
        self,
        entry: TargetEntry,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        warnings: list[str],
    ) -> ConsultResult:
        response_parts: list[str] = []
        done_chunk: ConsultChunk | None = None
        async for chunk in entry.adapter.consult(
            brief,
            urgency,
            session,
            timeout_s,
            entry.config.max_response_bytes,
        ):
            if chunk.type == "text" and chunk.text is not None:
                response_parts.append(chunk.text)
            elif chunk.type == "error":
                return ConsultResult(
                    response="",
                    target=entry.target_id,
                    model=entry.config.model,
                    session_id=session.session_id if session else None,
                    elapsed_ms=0,
                    outcome=Outcome.ERROR,
                    warnings=[*warnings, chunk.error_message or "error"],
                )
            elif chunk.type == "done":
                done_chunk = chunk

        response = "".join(response_parts)
        response, bridge_truncated = bound_response(
            response,
            entry.config.max_response_bytes,
        )
        return ConsultResult(
            response=response,
            target=entry.target_id,
            model=entry.config.model,
            session_id=session.session_id if session else None,
            elapsed_ms=0,
            tokens_in=(
                done_chunk.tokens_in
                if done_chunk and done_chunk.tokens_in
                else TokenCount.unknown()
            ),
            tokens_out=(
                done_chunk.tokens_out
                if done_chunk and done_chunk.tokens_out
                else TokenCount.unknown()
            ),
            outcome=Outcome.OK,
            truncated=bridge_truncated
            or bool(done_chunk and done_chunk.truncated),
            warnings=warnings,
        )

    def _synthetic_result(
        self,
        entry: TargetEntry,
        urgency: Urgency,
        session: Session | None,
        outcome: Outcome,
        warning: str,
        warnings: list[str],
    ) -> ConsultResult:
        del urgency
        return ConsultResult(
            response="",
            target=entry.target_id,
            model=entry.config.model,
            session_id=session.session_id if session else None,
            elapsed_ms=0,
            outcome=outcome,
            warnings=[*warnings, warning],
        )

    async def _write_audit(
        self,
        result: ConsultResult,
        brief: Brief,
        urgency: Urgency,
    ) -> None:
        entry = AuditEntry(
            target=result.target,
            urgency=urgency,
            session_id=result.session_id,
            brief_hash=brief.fingerprint(),
            elapsed_ms=result.elapsed_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            outcome=result.outcome,
            truncated=result.truncated,
            error_message=(
                "; ".join(result.warnings)
                if result.outcome != Outcome.OK and result.warnings
                else None
            ),
        )
        await self.audit.write(entry, brief, result.response, result.warnings)


async def build_bridge(config_path: str | Path) -> AgentBridge:
    """Build a configured AgentBridge instance."""
    config = load_config(config_path)
    registry = Registry()
    for target_id, target_config in config.targets.items():
        adapter = _adapter_from_config(target_id, target_config)
        registry.register(
            target_id=target_id,
            adapter=adapter,
            model=adapter.model,
            kind=adapter.kind,
            capabilities=adapter.capabilities,
            max_concurrent=int(target_config.get("max_concurrent", 4)),
            max_response_bytes=int(
                target_config.get("max_response_bytes", 32_768)
            ),
            session_ttl_seconds=int(
                target_config.get("session_ttl_seconds", 1_800)
            ),
            max_session_turns=int(
                target_config.get("max_session_turns", 8)
            ),
            strong_model=target_config.get("strong_model"),
        )
    sessions = SessionManager(config.server.sessions_path)
    await sessions.load()
    audit = AuditLog(config.server.audit_log, config.server.audit_bodies_dir)
    timeouts = {
        Urgency.QUICK: config.server.timeouts.quick,
        Urgency.DEEP: config.server.timeouts.deep,
        Urgency.BLOCKER: config.server.timeouts.blocker,
    }
    return AgentBridge(registry, sessions, audit, timeouts)


def create_mcp_server(bridge: AgentBridge) -> Any:
    """Create and register FastMCP tools."""
    if FastMCP is None:
        raise RuntimeError("fastmcp is not installed")
    mcp = FastMCP("harnesstalk-bridge")

    @mcp.tool()
    async def list_targets() -> list[Target]:
        return await bridge.list_targets()

    @mcp.tool()
    async def consult(
        target: str,
        brief: dict[str, Any],
        urgency: str = "quick",
        session_id: str | None = None,
        stream: bool = False,
    ) -> ConsultResult:
        return await bridge.consult(target, brief, urgency, session_id, stream)

    @mcp.tool()
    async def open_session(
        target: str,
        purpose: str,
        max_turns: int | None = None,
    ) -> OpenSessionResult:
        return await bridge.open_session(target, purpose, max_turns)

    @mcp.tool()
    async def close_session(session_id: str) -> CloseSessionResult:
        return await bridge.close_session(session_id)

    @mcp.tool()
    async def list_sessions() -> list[Session]:
        return await bridge.list_sessions()

    @mcp.tool()
    async def get_audit(
        limit: int = 50,
        target: str | None = None,
        since: str | None = None,
    ) -> list[AuditEntry]:
        return await bridge.get_audit(limit, target, since)

    return mcp


async def amain() -> None:
    """Async CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Agent Bridge MCP server")
    parser.add_argument("--config", default="config/targets.toml")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    config = load_config(args.config)
    bridge = await build_bridge(args.config)
    mcp = create_mcp_server(bridge)
    transport = args.transport or config.server.transport
    host = args.host or config.server.host
    port = args.port or config.server.port
    try:
        if transport == "stdio":
            await _maybe_await(mcp.run_async(transport="stdio"))
        else:
            await _maybe_await(
                mcp.run_async(
                    transport="streamable-http",
                    host=host,
                    port=port,
                )
            )
    finally:
        await bridge.close()


def main() -> None:
    """CLI entrypoint."""
    asyncio.run(amain())


def _adapter_from_config(target_id: str, config: dict[str, Any]) -> Adapter:
    adapter_name = str(config["adapter"])
    if adapter_name == "hermes":
        command = config.get("command", ["hermes", "chat", "--quiet"])
        if not isinstance(command, list):
            raise ValueError("Hermes command must be a TOML array")
        return HermesAdapter(
            target_id=target_id,
            command=[str(item) for item in command],
            cwd=config.get("cwd"),
            model=str(config.get("model", "hermes")),
            strong_model=config.get("strong_model"),
        )
    if adapter_name == "openclaw":
        return OpenClawAdapter(
            target_id=target_id,
            mcp_url=str(config["mcp_url"]),
            model=str(config.get("model", "openclaw")),
        )
    if adapter_name == "claude_api":
        return ClaudeApiAdapter(
            target_id=target_id,
            api_key_env=str(config.get("api_key_env", "ANTHROPIC_API_KEY")),
            model=str(config["model"]),
            max_tokens=int(config.get("max_tokens", 4096)),
        )
    raise ValueError(f"unknown adapter: {adapter_name}")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _brief_summary(brief: Brief) -> str:
    return f"Goal: {brief.goal}\nAsk: {brief.ask}"


def _parse_since(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 audit lower bound."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            "invalid since value: expected ISO-8601 datetime"
        ) from exc


if __name__ == "__main__":
    main()
