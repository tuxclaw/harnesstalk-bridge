"""OpenClaw MCP proxy adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

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


class OpenClawAdapter(Adapter):
    """Call OpenClaw's MCP HTTP endpoint."""

    kind = AdapterKind.MCP_PROXY

    def __init__(
        self,
        target_id: str,
        mcp_url: str,
        model: str = "openclaw",
    ) -> None:
        self.id = target_id
        self.mcp_url = mcp_url
        self.model = model
        self.capabilities = frozenset(
            {Capability.SESSIONS_NATIVE, Capability.STRONG_MODEL}
        )
        self._client = httpx.AsyncClient()

    async def health(self) -> TargetStatus:
        """Probe the MCP endpoint."""
        try:
            await self._client.post(
                self.mcp_url,
                json=self._rpc("tools/list", {}),
                timeout=2.0,
            )
        except httpx.HTTPError:
            return TargetStatus.UNREACHABLE
        return TargetStatus.READY

    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Send the brief through OpenClaw MCP tools."""
        prompt = format_brief(brief)
        tool_name = "sessions_send" if session else "consult"
        arguments: dict[str, Any] = {
            "prompt": prompt,
            "urgency": urgency.value,
        }
        if session and session.adapter_handle:
            arguments["session_id"] = session.adapter_handle

        try:
            result = await self._call_tool(tool_name, arguments, timeout_s)
        except httpx.HTTPError as exc:
            yield ConsultChunk(type="error", error_message=str(exc))
            return

        response = _extract_text(result)
        response, truncated = bound_response(response, max_response_bytes)
        yield ConsultChunk(type="text", text=response)
        yield ConsultChunk(
            type="done",
            tokens_in=estimate_tokens(prompt),
            tokens_out=estimate_tokens(response),
            truncated=truncated,
        )

    async def open_session(self, purpose: str) -> Session:
        """Open an OpenClaw session when the remote tool exists."""
        result = await self._call_tool(
            "sessions_spawn",
            {"purpose": purpose},
            timeout_s=30,
        )
        handle = _extract_session_id(result)
        return Session(target=self.id, purpose=purpose, adapter_handle=handle)

    async def close_session(self, session: Session) -> None:
        """Ask OpenClaw to close the target session."""
        if session.adapter_handle:
            await self._call_tool(
                "sessions_close",
                {"session_id": session.adapter_handle},
                timeout_s=30,
            )

    async def close(self) -> None:
        """Close the pooled OpenClaw HTTP client."""
        await self._client.aclose()

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout_s: int,
    ) -> dict[str, Any]:
        payload = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        response = await self._client.post(
            self.mcp_url,
            json=payload,
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise httpx.HTTPStatusError(
                str(data["error"]),
                request=response.request,
                response=response,
            )
        return data.get("result", data)

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": f"agent-bridge-{self.id}",
            "method": method,
            "params": params,
        }


def _extract_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        ]
        return "\n".join(part for part in parts if part)
    for key in ("response", "text", "message"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return str(result)


def _extract_session_id(result: dict[str, Any]) -> str | None:
    for key in ("session_id", "id", "handle"):
        value = result.get(key)
        if isinstance(value, str):
            return value
    return None
