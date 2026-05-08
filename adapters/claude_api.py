"""Anthropic Claude HTTP API adapter."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from adapters.base import bound_response, format_brief
from bridge.protocol import (
    Adapter,
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    HealthStatus,
    Session,
    TargetStatus,
    TokenCount,
    Urgency,
)

log = logging.getLogger(__name__)

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


class ClaudeApiAdapter(Adapter):
    """Call the Anthropic Messages API directly with native streaming."""

    kind = AdapterKind.HTTP_API
    capabilities = frozenset(
        {
            Capability.SESSIONS_NATIVE,
            Capability.EXACT_TOKENS,
            Capability.STREAMING,
            Capability.STRONG_MODEL,
        }
    )

    def __init__(
        self,
        target_id: str,
        api_key_env: str,
        model: str,
        strong_model: str | None = None,
        max_tokens: int = 4096,
        timeout_s: int | None = None,
        max_response_bytes: int | None = None,
    ) -> None:
        self.id = target_id
        self.api_key_env = api_key_env
        self.model = model
        self.strong_model = strong_model or model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes
        self._messages: dict[str, list[dict[str, str]]] = {}
        self._client = httpx.AsyncClient()

    async def health(self) -> HealthStatus:
        """Probe Anthropic with a malformed request that should return JSON 400."""
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            return HealthStatus(
                status=TargetStatus.UNREACHABLE,
                error_message=f"{self.api_key_env} is not set",
            )
        try:
            response = await self._client.post(
                _MESSAGES_URL,
                headers=self._headers(api_key),
                json={"malformed": True},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            return HealthStatus(
                status=TargetStatus.UNREACHABLE,
                error_message=str(exc),
            )

        if response.status_code in {400, 200}:
            try:
                response.json()
            except ValueError:
                return HealthStatus(
                    status=TargetStatus.DEGRADED,
                    error_message="Anthropic returned non-JSON health response",
                )
            return HealthStatus(status=TargetStatus.READY)
        if response.status_code in {401, 403}:
            return HealthStatus(
                status=TargetStatus.UNREACHABLE,
                error_message=f"Anthropic auth failed: HTTP {response.status_code}",
            )
        if response.status_code >= 500:
            return HealthStatus(
                status=TargetStatus.DEGRADED,
                error_message=f"Anthropic service error: HTTP {response.status_code}",
            )
        return HealthStatus(
            status=TargetStatus.DEGRADED,
            error_message=f"unexpected Anthropic health status: HTTP {response.status_code}",
        )

    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Send one streaming Messages API request and yield text deltas."""
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
        model = self.strong_model if urgency == Urgency.BLOCKER else self.model
        limit = self.max_response_bytes or max_response_bytes
        payload = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": messages,
            "stream": True,
        }

        full_text_parts: list[str] = []
        yielded = 0
        truncated = False
        tokens_in = TokenCount.unknown()
        tokens_out = TokenCount.unknown()

        try:
            async with self._client.stream(
                "POST",
                _MESSAGES_URL,
                headers=self._headers(api_key),
                json=payload,
                timeout=timeout_s,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    yield ConsultChunk(
                        type="error",
                        error_message=_http_error_message(response, body),
                    )
                    return
                async for event in _iter_sse_events(response):
                    event_type = event.get("type")
                    if event_type == "message_start":
                        usage = event.get("message", {}).get("usage", {})
                        tokens_in = _exact_from_usage(usage, "input_tokens")
                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text") if isinstance(delta, dict) else None
                        if not isinstance(text, str):
                            continue
                        full_text_parts.append(text)
                        if truncated:
                            continue
                        encoded = text.encode("utf-8")
                        if yielded + len(encoded) <= limit:
                            yielded += len(encoded)
                            yield ConsultChunk(type="text", text=text)
                        else:
                            remaining = max(0, limit - yielded)
                            partial = encoded[:remaining].decode(
                                "utf-8", errors="ignore"
                            )
                            omitted = len(encoded) - remaining
                            suffix = f"\n[truncated: {omitted} bytes omitted]"
                            if partial:
                                yield ConsultChunk(type="text", text=partial + suffix)
                            else:
                                yield ConsultChunk(type="text", text=suffix)
                            truncated = True
                    elif event_type == "message_delta":
                        usage = event.get("usage", {})
                        tokens_out = _exact_from_usage(usage, "output_tokens")
                    elif event_type == "message_stop":
                        break
        except httpx.HTTPError as exc:
            yield ConsultChunk(type="error", error_message=str(exc))
            return

        full_text = "".join(full_text_parts)
        if session:
            key = self._session_key(session)
            self._messages[key] = messages
            self._messages[key].append(
                {"role": "assistant", "content": full_text}
            )
        yield ConsultChunk(
            type="done",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
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
        self._messages.pop(self._session_key(session), None)
        session.closed = True

    async def close(self) -> None:
        """Close the pooled Anthropic HTTP client."""
        await self._client.aclose()

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "anthropic-version": _ANTHROPIC_VERSION,
            "x-api-key": api_key,
            "content-type": "application/json",
        }

    def _messages_for(self, session: Session | None) -> list[dict[str, str]]:
        if session is None:
            return []
        return list(self._messages.get(self._session_key(session), []))

    def _session_key(self, session: Session) -> str:
        return session.adapter_handle or session.session_id


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.debug("ignoring malformed Anthropic SSE payload")
            continue
        if isinstance(data, dict):
            yield data


def _exact_from_usage(usage: Any, key: str) -> TokenCount:
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int):
            return TokenCount.exact(value)
    return TokenCount.unknown()


def _http_error_message(response: httpx.Response, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"Anthropic HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return f"Anthropic HTTP {response.status_code}: {error['message']}"
    return f"Anthropic HTTP {response.status_code}"
