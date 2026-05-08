"""FastMCP server for Agent Bridge."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from adapters.base import Adapter
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
    ConsultResult,
    GetAuditRequest,
    OpenSessionResult,
    Outcome,
    Session,
    Target,
    Urgency,
)
from bridge.registry import HealthConfig, Registry, TargetLimits
from bridge.sessions import SessionManager
from bridge.streaming import stream_consult

try:
    from fastmcp import Context, FastMCP
    from fastmcp.exceptions import ToolError
except ModuleNotFoundError:  # pragma: no cover - local tests avoid FastMCP
    Context = Any  # type: ignore[misc, assignment]
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
        """Return configured targets with current health snapshots."""
        return await self.registry.list_targets()

    async def open_session(
        self,
        target: str,
        purpose: str,
        max_turns: int | None = None,
    ) -> OpenSessionResult:
        """Create a persistent session."""
        self._require_target(target)
        adapter = self.registry.get_adapter(target)
        limits = self.registry.get_limits(target)
        target_session = await adapter.open_session(purpose)
        session = await self.sessions.open_session(
            target=target,
            purpose=purpose,
            ttl_seconds=limits.session_ttl_seconds,
            max_turns=max_turns or limits.max_session_turns,
            adapter_handle=target_session.adapter_handle
            or target_session.session_id,
        )
        return OpenSessionResult(
            session_id=session.session_id,
            ttl_seconds=session.ttl_seconds,
            max_turns=session.max_turns,
        )

    async def close_session(self, session_id: str) -> CloseSessionResult:
        """Close a persistent session."""
        session = self.sessions.get(session_id)
        if session and self.registry.has(session.target):
            await self.registry.get_adapter(session.target).close_session(session)
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
        ctx: Any | None = None,
    ) -> ConsultResult:
        """Consult one target and return the final result."""
        self._require_target(target)
        adapter = self.registry.get_adapter(target)
        limits = self.registry.get_limits(target)
        parsed_brief = (
            brief if isinstance(brief, Brief) else Brief.model_validate(brief)
        )
        parsed_urgency = Urgency(urgency)
        warnings: list[str] = []
        session = self._session_for(target, session_id)
        session_locked = False
        target_locked = False

        if Capability.SESSIONS_NONE in adapter.capabilities:
            if parsed_urgency != Urgency.QUICK or session_id:
                warnings.append("deep degraded to quick: target has SESSIONS_NONE")
            session = None
            parsed_urgency = Urgency.QUICK

        if session and session.is_exhausted():
            result = self._synthetic_result(
                target,
                adapter.model,
                session,
                Outcome.REJECTED,
                "session turn cap reached",
                warnings,
            )
            await self._write_audit(result, parsed_brief, parsed_urgency, stream)
            return result

        if session:
            session_locked = await self.sessions.try_acquire(session.session_id)
            if not session_locked:
                result = self._synthetic_result(
                    target,
                    adapter.model,
                    session,
                    Outcome.BUSY,
                    "session is busy",
                    warnings,
                )
                await self._write_audit(result, parsed_brief, parsed_urgency, stream)
                return result

        target_locked = await self._try_acquire_target(target)
        if not target_locked:
            result = self._synthetic_result(
                target,
                adapter.model,
                session,
                Outcome.BUSY,
                "target is busy",
                warnings,
            )
            await self._write_audit(result, parsed_brief, parsed_urgency, stream)
            if session_locked and session:
                await self.sessions.release(session.session_id)
            return result

        started = time.monotonic()
        timeout_s = self.timeouts[parsed_urgency]
        try:
            result = await stream_consult(
                ctx,
                adapter.consult(
                    parsed_brief,
                    parsed_urgency,
                    session,
                    timeout_s,
                    limits.max_response_bytes,
                ),
                progress_token=("consult" if stream else None),
                target=target,
                model=(
                    getattr(adapter, "strong_model", adapter.model)
                    if parsed_urgency == Urgency.BLOCKER
                    else adapter.model
                ),
                session_id=session.session_id if session else None,
                timeout_s=timeout_s + 5,
                warnings=warnings,
            )
        finally:
            if target_locked:
                self.registry.get_semaphore(target).release()
            if session_locked and session:
                await self.sessions.release(session.session_id)

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        if session and result.outcome == Outcome.OK:
            await self.sessions.touch(session.session_id)
            if Capability.SESSIONS_REPLAY in adapter.capabilities:
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
        await self._write_audit(result, parsed_brief, parsed_urgency, stream)
        return result

    async def close(self) -> None:
        """Close resources owned by registered adapters and registry pollers."""
        for target_id in self.registry.all_ids():
            adapter = self.registry.get_adapter(target_id)
            close = getattr(adapter, "close", None)
            if close is not None:
                await close()
        await self.registry.aclose()

    def _require_target(self, target: str) -> None:
        if not self.registry.has(target):
            raise ToolError(f"unknown target: {target}")

    def _session_for(self, target: str, session_id: str | None) -> Session | None:
        if session_id is None:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            raise ToolError(f"unknown session: {session_id}")
        if session.target != target:
            raise ToolError("session target does not match consult target")
        if session.is_expired():
            raise ToolError(f"session expired or closed: {session_id}")
        return session

    async def _try_acquire_target(self, target: str) -> bool:
        semaphore = self.registry.get_semaphore(target)
        if semaphore.locked():
            return False
        await semaphore.acquire()
        return True

    def _synthetic_result(
        self,
        target: str,
        model: str,
        session: Session | None,
        outcome: Outcome,
        warning: str,
        warnings: list[str],
    ) -> ConsultResult:
        return ConsultResult(
            response="",
            target=target,
            model=model,
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
        streamed: bool,
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
            streamed=streamed,
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
    health = HealthConfig(
        lazy_cache_seconds=config.server.health.lazy_cache_seconds,
        polled_interval_seconds=config.server.health.polled_interval_seconds,
        check_timeout_seconds=config.server.health.check_timeout_seconds,
    )
    registry = Registry(health)
    for target_id, target_config in config.targets.items():
        adapter = _adapter_from_config(target_id, target_config)
        limits = TargetLimits(
            max_concurrent=int(target_config.get("max_concurrent", 4)),
            max_response_bytes=int(target_config.get("max_response_bytes", 32_768)),
            session_ttl_seconds=int(target_config.get("session_ttl_seconds", 1_800)),
            max_session_turns=int(target_config.get("max_session_turns", 8)),
        )
        registry.register(adapter=adapter, limits=limits)
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
        ctx: Context | None = None,
    ) -> ConsultResult:
        return await bridge.consult(
            target, brief, urgency, session_id, stream, ctx=ctx
        )

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
            strong_model=config.get("strong_model"),
            max_tokens=int(config.get("max_tokens", 4096)),
            timeout_s=int(config.get("timeout_s", 120)),
            max_response_bytes=int(config.get("max_response_bytes", 65_536)),
        )
    raise ValueError(f"unknown adapter: {adapter_name}")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _brief_summary(brief: Brief) -> str:
    return f"Goal: {brief.goal}\nAsk: {brief.ask}"


def _parse_since(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 audit lower bound."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError("invalid since value: expected ISO-8601 datetime") from exc


if __name__ == "__main__":
    main()
