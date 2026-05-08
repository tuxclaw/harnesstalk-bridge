"""Protocol models for Agent Bridge MCP (v3).

All cross-component types live here. Adapters, the server, the audit log,
the registry, and the session store all import from this module — nothing
in here imports from the rest of the bridge, so it's safe to import from
anywhere.

Pydantic v2.

Changes from v2.1:
  - HealthStatus model (replaces bare TargetStatus return type from health())
  - Target gains last_checked_at and latency_ms
  - Adapter ABC defined here (was implied / scattered before)
  - AuditEntry.streamed boolean

Changes from v2:
  - TokenCount typed model (value + method)
  - Capability split: SESSIONS_NATIVE / SESSIONS_REPLAY / SESSIONS_NONE,
    plus EXACT_TOKENS
  - AdapterKind enum + capability/kind validation rules
  - Outcome.BUSY
  - Session.max_turns + Turn model for replay adapters
  - ConsultResult.truncated, ConsultResult.warnings
  - ConsultChunk.truncated on done
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncIterator, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Limits — kept as module constants so config loader can override at startup
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES = 8 * 1024
MAX_ATTACHMENTS = 4
MAX_GOAL_CHARS = 1_000
MAX_FAILING_CHARS = 4_000
MAX_ASK_CHARS = 1_000
MAX_TRIED_ITEMS = 10
MAX_TRIED_ITEM_CHARS = 500
DEFAULT_MAX_SESSION_TURNS = 8


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Urgency(str, Enum):
    """How the calling agent wants the bridge to handle the consult.

    - QUICK: stateless one-shot. Fresh session, no history retained.
    - DEEP: persistent session, follow-ups allowed via session_id.
    - BLOCKER: same as DEEP but adapter may swap to a stronger model
      (configured per-target via `strong_model` in targets.toml).
    """

    QUICK = "quick"
    DEEP = "deep"
    BLOCKER = "blocker"


class TargetStatus(str, Enum):
    READY = "ready"
    UNREACHABLE = "unreachable"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class Capability(str, Enum):
    """What a target can actually do. Honest, not aspirational.

    Exactly one of SESSIONS_* must be declared per adapter. The bridge
    validates this at registration so list_targets never lies.
    """

    SESSIONS_NATIVE = "sessions_native"
    SESSIONS_REPLAY = "sessions_replay"
    SESSIONS_NONE = "sessions_none"
    STREAMING = "streaming"
    STRONG_MODEL = "strong_model"
    EXACT_TOKENS = "exact_tokens"


SESSION_CAPABILITIES = frozenset(
    {Capability.SESSIONS_NATIVE, Capability.SESSIONS_REPLAY, Capability.SESSIONS_NONE}
)


class AdapterKind(str, Enum):
    """How an adapter talks to its target. Constrains valid capabilities."""

    HTTP_API = "http_api"
    MCP_PROXY = "mcp_proxy"
    CLI_SUBPROCESS = "cli_subprocess"
    PTY_ATTACHED = "pty_attached"


class AttachmentKind(str, Enum):
    CODE = "code"
    ERROR = "error"
    FILE_EXCERPT = "file_excerpt"
    DIFF = "diff"
    LOG = "log"


class Outcome(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    REJECTED = "rejected"   # validation failure or session turn cap hit
    BUSY = "busy"           # session lock held or target semaphore full


class TokenMethod(str, Enum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Capability/kind validation rules
# ---------------------------------------------------------------------------

# Capabilities each kind is forbidden from claiming. The bridge calls
# validate_capabilities(kind, caps) at registration; mismatch -> startup
# failure. Better to crash at boot than serve lies via list_targets.
#
# Design note: this table narrows obvious lies, not every possible mismatch.
# A CLI_SUBPROCESS adapter CAN claim SESSIONS_NATIVE if the underlying
# harness persists sessions externally and accepts a resume flag (Hermes
# does this via its SQLite store + --resume <session_id>). The honest
# constraint is "an adapter must declare its actual capabilities" — kind
# is a hint for what's typical, not a hard predictor.
#
# EXACT_TOKENS stays forbidden for subprocess/PTY kinds because a wrapper
# process genuinely cannot see the provider's API response payload. If a
# harness writes exact counts to stderr or a log file, the adapter can
# parse them and claim... still not EXACT_TOKENS, because that's
# transport-level exactness from the model API. Estimated counts from
# subprocess output are ESTIMATED.
_KIND_FORBIDDEN_CAPS: dict[AdapterKind, frozenset[Capability]] = {
    AdapterKind.CLI_SUBPROCESS: frozenset({Capability.EXACT_TOKENS}),
    AdapterKind.PTY_ATTACHED: frozenset({Capability.EXACT_TOKENS}),
    AdapterKind.MCP_PROXY: frozenset(),    # depends on harness; trust the declaration
    AdapterKind.HTTP_API: frozenset(),     # model-dependent; trust the declaration
}


def validate_capabilities(
    kind: AdapterKind, caps: frozenset[Capability]
) -> None:
    """Raise ValueError if caps are inconsistent with kind.

    Rules:
      - Exactly one SESSIONS_* must be present.
      - No capability in _KIND_FORBIDDEN_CAPS[kind] may be present.
    """
    session_caps = caps & SESSION_CAPABILITIES
    if len(session_caps) != 1:
        raise ValueError(
            f"adapter must declare exactly one of "
            f"{sorted(c.value for c in SESSION_CAPABILITIES)}, "
            f"got {sorted(c.value for c in session_caps)}"
        )

    forbidden = caps & _KIND_FORBIDDEN_CAPS[kind]
    if forbidden:
        raise ValueError(
            f"kind={kind.value} cannot claim "
            f"{sorted(c.value for c in forbidden)}"
        )


# ---------------------------------------------------------------------------
# Token counts
# ---------------------------------------------------------------------------


class TokenCount(BaseModel):
    """A token count with provenance.

    HTTP APIs return exact counts. CLI adapters estimate via tokenizer.
    PTY adapters usually can't count at all. Recording method means cost
    debugging later has traceable numbers.
    """

    value: int = Field(..., ge=0)
    method: TokenMethod

    @classmethod
    def unknown(cls) -> "TokenCount":
        return cls(value=0, method=TokenMethod.UNKNOWN)

    @classmethod
    def exact(cls, n: int) -> "TokenCount":
        return cls(value=n, method=TokenMethod.EXACT)

    @classmethod
    def estimated(cls, n: int) -> "TokenCount":
        return cls(value=n, method=TokenMethod.ESTIMATED)


# ---------------------------------------------------------------------------
# Brief — the structured handoff the calling agent must fill in
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    kind: AttachmentKind
    label: str = Field(..., max_length=200)
    content: str = Field(...)
    language: Optional[str] = Field(default=None, max_length=40)

    @field_validator("content")
    @classmethod
    def _content_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes; "
                "trim it before sending"
            )
        return v


class Brief(BaseModel):
    """Structured question for a target agent. Four required fields are the
    contract — if the caller can't fill them, they don't have a real
    question yet.
    """

    goal: str = Field(..., max_length=MAX_GOAL_CHARS)
    tried: list[str] = Field(..., min_length=1, max_length=MAX_TRIED_ITEMS)
    failing: str = Field(..., max_length=MAX_FAILING_CHARS)
    ask: str = Field(..., max_length=MAX_ASK_CHARS)
    attachments: list[Attachment] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS
    )

    @field_validator("tried")
    @classmethod
    def _tried_items(cls, v: list[str]) -> list[str]:
        for i, item in enumerate(v):
            if not item.strip():
                raise ValueError(f"tried[{i}] is empty")
            if len(item) > MAX_TRIED_ITEM_CHARS:
                raise ValueError(
                    f"tried[{i}] exceeds {MAX_TRIED_ITEM_CHARS} chars"
                )
        return v

    def fingerprint(self) -> str:
        """Stable SHA-256 of the brief content. Used as audit body key."""
        import json

        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Health and targets (returned by list_targets)
# ---------------------------------------------------------------------------


class HealthStatus(BaseModel):
    """Result of a single Adapter.health() check.

    Returned by adapters; consumed by the registry's state machine. The
    registry caches the most recent HealthStatus per target and derives
    the user-facing Target view from it.

    Adapters fill in status, error_message (on failure), and optionally
    latency_ms. The registry fills in checked_at if the adapter doesn't
    set it, and maintains the consecutive_* streak counters across calls.
    """

    status: TargetStatus
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description="How long the health check itself took, in ms.",
    )
    error_message: Optional[str] = Field(
        default=None,
        max_length=2_000,
        description="Populated for DEGRADED/UNREACHABLE; surfaced as Target.note.",
    )
    consecutive_healthy: int = Field(default=0, ge=0)
    consecutive_unhealthy: int = Field(default=0, ge=0)


class Target(BaseModel):
    """User-facing view of a registered target. Returned by list_targets.

    Built by the registry from the latest HealthStatus + the adapter's
    declared identity. last_checked_at and latency_ms are populated from
    HealthStatus; status reflects the most recent check (or the cached
    value if within the lazy-cache TTL).
    """

    id: str
    model: str
    kind: AdapterKind
    status: TargetStatus
    capabilities: list[Capability] = Field(default_factory=list)
    note: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
    return f"sess_{uuid4().hex[:16]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Turn(BaseModel):
    """One exchange in a SESSIONS_REPLAY history. Bridge stores these and
    prepends on each consult. SESSIONS_NATIVE adapters leave history empty.
    """

    role: Literal["caller", "target"]
    content: str
    ts: datetime = Field(default_factory=_now)


class Session(BaseModel):
    """A persistent thread with a target. Lives in state/sessions.json."""

    session_id: str = Field(default_factory=_new_session_id)
    target: str
    purpose: str = Field(..., max_length=500)
    created_at: datetime = Field(default_factory=_now)
    last_used_at: datetime = Field(default_factory=_now)
    ttl_seconds: int = Field(default=1800, ge=60, le=86_400)
    max_turns: int = Field(default=DEFAULT_MAX_SESSION_TURNS, ge=1, le=100)
    turn_count: int = 0
    closed: bool = False
    adapter_handle: Optional[str] = None
    history: list[Turn] = Field(default_factory=list)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.closed:
            return True
        now = now or _now()
        age = (now - self.last_used_at).total_seconds()
        return age > self.ttl_seconds

    def is_exhausted(self) -> bool:
        return self.turn_count >= self.max_turns

    def touch(self) -> None:
        self.last_used_at = _now()
        self.turn_count += 1


# ---------------------------------------------------------------------------
# Consult result and streaming chunks
# ---------------------------------------------------------------------------


class ConsultChunk(BaseModel):
    """A single chunk yielded by an adapter's async iterator.

    Adapters yield zero or more TEXT chunks followed by exactly one DONE.
    A single ERROR chunk may replace the DONE if something blew up.
    """

    type: Literal["text", "done", "error"]
    text: Optional[str] = None
    # Populated on DONE
    tokens_in: Optional[TokenCount] = None
    tokens_out: Optional[TokenCount] = None
    truncated: bool = False
    # Populated on ERROR
    error_message: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "ConsultChunk":
        if self.type == "text" and self.text is None:
            raise ValueError("text chunk requires text")
        if self.type == "error" and not self.error_message:
            raise ValueError("error chunk requires error_message")
        return self


class ConsultResult(BaseModel):
    """Final result returned to the calling agent (non-streaming path)."""

    response: str
    target: str
    model: str
    session_id: Optional[str] = None
    elapsed_ms: int = Field(..., ge=0)
    tokens_in: TokenCount = Field(default_factory=TokenCount.unknown)
    tokens_out: TokenCount = Field(default_factory=TokenCount.unknown)
    outcome: Outcome = Outcome.OK
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    """One line of state/audit.jsonl. Full bodies live in audit-bodies/."""

    id: str = Field(default_factory=lambda: f"aud_{uuid4().hex[:16]}")
    ts: datetime = Field(default_factory=_now)
    target: str
    urgency: Urgency
    session_id: Optional[str] = None
    brief_hash: str
    elapsed_ms: int = Field(..., ge=0)
    tokens_in: TokenCount = Field(default_factory=TokenCount.unknown)
    tokens_out: TokenCount = Field(default_factory=TokenCount.unknown)
    outcome: Outcome
    truncated: bool = False
    streamed: bool = False
    error_message: Optional[str] = None

    def to_jsonl(self) -> str:
        return self.model_dump_json()


# ---------------------------------------------------------------------------
# Tool input/output envelopes
# ---------------------------------------------------------------------------


class ConsultRequest(BaseModel):
    target: str
    brief: Brief
    urgency: Urgency = Urgency.QUICK
    session_id: Optional[str] = None
    stream: bool = False


class OpenSessionRequest(BaseModel):
    target: str
    purpose: str = Field(..., max_length=500)
    max_turns: int = Field(default=DEFAULT_MAX_SESSION_TURNS, ge=1, le=100)


class OpenSessionResult(BaseModel):
    session_id: str
    ttl_seconds: int
    max_turns: int


class CloseSessionResult(BaseModel):
    session_id: str
    closed: bool


class GetAuditRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000)
    target: Optional[str] = None
    since: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Adapter ABC
# ---------------------------------------------------------------------------


class Adapter(ABC):
    """Abstract base class every concrete adapter inherits from.

    Subclasses set the four class-level identity attributes (id, model,
    kind, capabilities) and implement the four async methods. The
    registry validates capability/kind consistency at registration via
    validate_capabilities(); inconsistent declarations crash the bridge
    at startup rather than serving lies via list_targets.

    Streaming contract: consult() is an async generator. It yields zero
    or more ConsultChunk(type='text') chunks, followed by exactly one
    ConsultChunk(type='done') OR ConsultChunk(type='error'). Adapters
    without streaming support yield a single text chunk + done — the
    server's streaming wrapper handles this case transparently, so
    non-streaming adapters don't need special-casing.

    Session contract: depends on the SESSIONS_* capability declared.
      - SESSIONS_NATIVE: open_session/close_session manage state in the
        underlying harness; consult() passes session.adapter_handle to
        the harness; Session.history is left empty (the registry knows
        not to populate it).
      - SESSIONS_REPLAY: open_session creates an empty Session; the
        bridge appends to Session.history after each turn; consult()
        prepends history to the brief at call time.
      - SESSIONS_NONE: open_session must raise NotImplementedError;
        consult() with session=not None must also raise. The server
        degrades urgency=deep to quick with a warning before reaching
        the adapter.

    Lifecycle: instances are created at server startup, registered with
    the registry, and live until shutdown. Adapters should be safe to
    have multiple consult() calls in flight concurrently up to the
    target's max_concurrent semaphore (enforced by the registry).
    """

    id: str
    model: str
    kind: AdapterKind
    capabilities: frozenset[Capability]

    @abstractmethod
    async def health(self) -> HealthStatus:
        """Cheap liveness probe. Must complete within the configured
        check_timeout_seconds. Should not consume real consult quota
        (no LLM calls). Errors should be caught and returned as
        HealthStatus(status=UNREACHABLE, error_message=...) rather than
        raising — the registry catches exceptions defensively, but
        adapters should produce structured results when possible.
        """

    @abstractmethod
    def consult(
        self,
        brief: "Brief",
        urgency: Urgency,
        session: Optional["Session"],
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Run a consultation, yielding chunks as they arrive.

        Adapter is responsible for enforcing timeout_s and
        max_response_bytes at the protocol level it owns (subprocess
        kill, HTTP timeout, etc.). The server wraps the call in
        asyncio.wait_for as a backstop, but adapters that respect
        these themselves produce cleaner errors and partial results.

        On truncation: stop yielding text, emit a final text chunk
        containing '\\n[truncated: N bytes omitted]', then done with
        truncated=True.
        """

    @abstractmethod
    async def open_session(self, purpose: str) -> "Session":
        """Create a new persistent session. Implementation depends on
        SESSIONS_* capability — see class docstring."""

    @abstractmethod
    async def close_session(self, session: "Session") -> None:
        """Release any harness-side resources for the session. Idempotent.
        Bridge may call this on TTL expiry, explicit close, or shutdown."""


__all__ = [
    # Limits
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENTS",
    "DEFAULT_MAX_SESSION_TURNS",
    # Enums
    "Urgency",
    "TargetStatus",
    "Capability",
    "SESSION_CAPABILITIES",
    "AdapterKind",
    "AttachmentKind",
    "Outcome",
    "TokenMethod",
    # Validation
    "validate_capabilities",
    # Core models
    "TokenCount",
    "Attachment",
    "Brief",
    "HealthStatus",
    "Target",
    "Turn",
    "Session",
    "ConsultChunk",
    "ConsultResult",
    "AuditEntry",
    # Tool envelopes
    "ConsultRequest",
    "OpenSessionRequest",
    "OpenSessionResult",
    "CloseSessionResult",
    "GetAuditRequest",
    # ABC
    "Adapter",
]
