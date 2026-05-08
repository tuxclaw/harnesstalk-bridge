"""Adapter base classes and shared helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

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


class Adapter(ABC):
    """Abstract interface implemented by all consultation targets."""

    id: str
    model: str
    kind: AdapterKind
    capabilities: frozenset[Capability]

    @abstractmethod
    async def health(self) -> TargetStatus:
        """Return current target availability."""

    @abstractmethod
    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Send a brief to the target.

        Args:
            brief: Structured consultation request.
            urgency: Requested urgency level.
            session: Existing bridge session, if any.
            timeout_s: Adapter-level timeout in seconds.
            max_response_bytes: Maximum response bytes to return.

        Yields:
            Text chunks followed by a done chunk, or one error chunk.
        """

    @abstractmethod
    async def open_session(self, purpose: str) -> Session:
        """Open a target-backed session where supported."""

    @abstractmethod
    async def close_session(self, session: Session) -> None:
        """Close target-backed session resources where supported."""

    async def close(self) -> None:
        """Close adapter-level resources, such as pooled HTTP clients."""


def estimate_tokens(text: str) -> TokenCount:
    """Estimate token count using the spec's rough char/4 rule."""
    return TokenCount.estimated(max(1, len(text) // 4) if text else 0)


def format_brief(brief: Brief) -> str:
    """Format a structured brief as plain text for target harnesses."""
    lines = [
        "Goal:",
        brief.goal,
        "",
        "Tried:",
    ]
    lines.extend(f"- {item}" for item in brief.tried)
    lines.extend(["", "Failing:", brief.failing, "", "Ask:", brief.ask])
    if brief.attachments:
        lines.append("")
        lines.append("Attachments:")
        for attachment in brief.attachments:
            language = (
                f" ({attachment.language})" if attachment.language else ""
            )
            lines.append(
                f"--- {attachment.kind.value}: "
                f"{attachment.label}{language}"
            )
            lines.append(attachment.content)
    return "\n".join(lines)


def bound_response(
    response: str,
    max_response_bytes: int,
) -> tuple[str, bool]:
    """Trim a response to the configured byte limit."""
    encoded = response.encode("utf-8")
    if len(encoded) <= max_response_bytes:
        return response, False
    omitted = len(encoded) - max_response_bytes
    trimmed = encoded[:max_response_bytes].decode("utf-8", errors="ignore")
    return f"{trimmed}\n[truncated: {omitted} bytes omitted]", True
