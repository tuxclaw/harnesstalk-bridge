"""Anthropic Claude HTTP API adapter."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx

from adapters.base import Adapter, bound_response, format_brief
from bridge.protocol import (
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    Session,
    TargetStatus,
    TokenCount,
    Urgency,
)


class ClaudeApiAdapter(Adapter):
    """Call the Anthropic Messages API directly."""

    kind = AdapterKind.HTTP_API

    def __init__(
        self,
        target_id: str,
        api_key_env: str,
        model: str,
        max_tokens: int = 4096,
    ) -> None:
        self.id = target_id
        self.api_key_env = api_key_env
        self.model = model
        self.max_tokens = max_tokens
        self.capabilities = frozenset(
            {
                Capability.SESSIONS_NATIVE,
                Capability.EXACT_TOKENS,
                Capability.STREAMING,
                Capability.STRONG_MODEL,
            }
        )
        self._messages: dict[str, list[dict[str, str]]] = {}
        self._client = httpx.AsyncClient()

    async def health(self) -> TargetStatus:
        """Return disabled when the API key is not configured."""
        if not os.environ.get(self.api_key_env):
            return TargetStatus.DISABLED
        return TargetStatus.READY

    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Send one Messages API request."""
        del urgency
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            yield ConsultChunk(
                type="error",
                error_message=f"{self.api_key_env} is not set",
            )
            return

        prompt = format_brief(brief)
        messages = self._messages_for(session)
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        headers = {
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
            "content-type": "application/json",
        }

        try:
            response = await self._client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            yield ConsultChunk(type="error", error_message=str(exc))
            return

        data = response.json()
        full_text = _extract_text(data)
        returned_text, truncated = bound_response(
            full_text,
            max_response_bytes,
        )
        if session:
            self._messages.setdefault(session.session_id, messages)
            self._messages[session.session_id].append(
                {"role": "assistant", "content": full_text}
            )
        usage = data.get("usage", {})
        yield ConsultChunk(type="text", text=returned_text)
        yield ConsultChunk(
            type="done",
            tokens_in=TokenCount.exact(int(usage.get("input_tokens", 0))),
            tokens_out=TokenCount.exact(int(usage.get("output_tokens", 0))),
            truncated=truncated,
        )

    async def open_session(self, purpose: str) -> Session:
        """Create an in-memory conversation array."""
        session = Session(target=self.id, purpose=purpose)
        self._messages[session.session_id] = [
            {"role": "user", "content": f"Session purpose: {purpose}"}
        ]
        return session

    async def close_session(self, session: Session) -> None:
        """Drop in-memory conversation state."""
        self._messages.pop(session.session_id, None)

    async def close(self) -> None:
        """Close the pooled Anthropic HTTP client."""
        await self._client.aclose()

    def _messages_for(self, session: Session | None) -> list[dict[str, str]]:
        if session is None:
            return []
        return list(self._messages.get(session.session_id, []))


def _extract_text(data: dict[str, object]) -> str:
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)
