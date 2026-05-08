"""Adapter compatibility imports and shared helpers."""

from __future__ import annotations

from bridge.protocol import Adapter, Brief, TokenCount


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
            language = f" ({attachment.language})" if attachment.language else ""
            lines.append(
                f"--- {attachment.kind.value}: {attachment.label}{language}"
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
