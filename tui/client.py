"""Read-only HTTP MCP client for the Agent Bridge TUI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import TypeAdapter

from bridge.protocol import AuditEntry, Session, Target

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BridgeSnapshot:
    """Latest bridge state fetched by the TUI."""

    targets: list[Target] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)


class BridgeClient:
    """Small read-only client for the bridge's streamable HTTP transport."""

    def __init__(
        self,
        bridge_url: str = "http://127.0.0.1:7878/mcp",
        *,
        timeout: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bridge_url = bridge_url
        self._owned_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        self._next_id = 0
        self.connected = False
        self.last_error: str | None = None
        self.snapshot = BridgeSnapshot()

    async def close(self) -> None:
        """Close the underlying HTTP client if owned by this instance."""
        if self._owned_client:
            await self._client.aclose()

    async def list_targets(self) -> list[Target] | None:
        """Fetch target health state. Returns cached data on disconnect."""
        payload = await self._call_tool("list_targets", {})
        if payload is None:
            return self.snapshot.targets
        targets = TypeAdapter(list[Target]).validate_python(payload)
        self.snapshot.targets = targets
        return targets

    async def list_sessions(self) -> list[Session] | None:
        """Fetch active sessions. Returns cached data on disconnect."""
        payload = await self._call_tool("list_sessions", {})
        if payload is None:
            return self.snapshot.sessions
        sessions = TypeAdapter(list[Session]).validate_python(payload)
        self.snapshot.sessions = sessions
        return sessions

    async def get_audit(
        self,
        *,
        limit: int = 200,
        target: str | None = None,
        since: str | None = None,
    ) -> list[AuditEntry] | None:
        """Fetch audit entries. Returns cached data on disconnect."""
        arguments: dict[str, Any] = {"limit": limit}
        if target is not None:
            arguments["target"] = target
        if since is not None:
            arguments["since"] = since
        payload = await self._call_tool("get_audit", arguments)
        if payload is None:
            return self.snapshot.audit
        entries = TypeAdapter(list[AuditEntry]).validate_python(payload)
        self.snapshot.audit = entries
        return entries

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any | None:
        """Call a read-only MCP tool and normalize common FastMCP response shapes."""
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            response = await self._client.post(
                self.bridge_url,
                json=request,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            self.connected = True
            self.last_error = None
            return self._extract_result(data.get("result"))
        except (httpx.HTTPError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            logger.warning("bridge read failed for %s: %s", name, exc)
            self.connected = False
            self.last_error = str(exc)
            return None

    def build_tool_request(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Build a JSON-RPC tool request for tests and diagnostics."""
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    @staticmethod
    def _extract_result(result: Any) -> Any:
        if isinstance(result, dict):
            if "structuredContent" in result:
                structured = result["structuredContent"]
                if isinstance(structured, dict) and "result" in structured:
                    return structured["result"]
                return structured
            if "content" in result and isinstance(result["content"], list):
                content = result["content"]
                if content and isinstance(content[0], dict) and "text" in content[0]:
                    text = content[0]["text"]
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
            if "result" in result:
                return result["result"]
        return result
