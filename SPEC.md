# Agent Bridge MCP — v2.1 Spec

A local MCP server that lets agent harnesses (OpenClaw, Hermes, and optionally the Claude API) consult each other mid-task. The active agent writes a structured brief, hands it off to the bridge, and gets a response back as a tool result.

## What changed from v2

All v2 deltas address feedback on adapter realism, session honesty, concurrency, response bounds, and timeouts. Public tool surface (`consult`, `list_targets`, `open_session`, `close_session`, `list_sessions`, `get_audit`) is unchanged — every change is underneath.

- **Adapter taxonomy** — adapters declare their `kind` so capability claims are honest. No pretending a CLI subprocess has free sessions.
- **Capability split** — `SESSIONS` becomes three: `SESSIONS_NATIVE`, `SESSIONS_REPLAY`, `SESSIONS_NONE`. Calling agent knows what to expect.
- **`TokenCount`** typed model — `value` + `method` (`exact` / `estimated` / `unknown`). Cost debugging gets traceable numbers.
- **Concurrency model** — per-session lock, per-target semaphore, no global lock.
- **`Outcome.BUSY`** — second caller on a busy session gets it immediately, not queued.
- **Bounded responses** — `max_response_bytes` per target. Truncation surfaces as `ConsultResult.truncated: bool`, not a hidden silence.
- **Per-urgency timeouts** — `quick` short, `blocker` long. Timeouts kill the call, record the elapsed time, leave deep sessions open for retry.
- **`ConsultResult.warnings`** — surfaces fallbacks (e.g. `deep` degraded to `quick` because target is `SESSIONS_NONE`).
- **Session turn cap** — `max_session_turns` to bound replay-adapter cost.

## Project layout

```
agent-bridge-mcp/
├── server.py                 # FastMCP server
├── bridge/
│   ├── __init__.py
│   ├── protocol.py           # Pydantic models — single source of types
│   ├── registry.py           # target registration, capability discovery, per-target semaphores
│   ├── sessions.py           # session lifecycle, TTL, per-session locks
│   └── audit.py              # JSONL audit log writer + body store
├── adapters/
│   ├── __init__.py
│   ├── base.py               # Adapter ABC, AdapterKind enum
│   ├── hermes.py             # CLI subprocess kind
│   ├── openclaw.py           # MCP-to-MCP kind
│   └── claude_api.py         # HTTP API kind
├── config/
│   └── targets.toml
├── state/
│   ├── sessions.json
│   ├── audit.jsonl
│   └── audit-bodies/         # full briefs + responses, keyed by brief_hash
├── tests/
├── systemd/
│   └── agent-bridge.service
├── pyproject.toml
└── README.md
```

## Adapter taxonomy

Each adapter declares its `kind` as a class attribute. The kind constrains which capabilities it can claim — the bridge enforces this at registration so a CLI adapter can't lie and say it has native sessions.

| Adapter kind | Mechanism | Sessions | Streaming | Token counts |
|---|---|---|---|---|
| `HTTP_API` | requests / SDK | native (conversation array) | native (SSE) | exact |
| `MCP_PROXY` | bridge calls target's MCP server | depends on harness | depends on harness | from harness if exposed |
| `CLI_SUBPROCESS` | spawn + stdin/stdout | usually replay only | line-buffered stdout if lucky | estimated via tokenizer |
| `PTY_ATTACHED` | pexpect against a REPL | yes-ish, fragile | yes | unknown |

Pick the row, live with its constraints. No optimism in the adapter declarations.

## Capabilities

```python
class Capability(str, Enum):
    SESSIONS_NATIVE = "sessions_native"   # underlying harness has real sessions
    SESSIONS_REPLAY = "sessions_replay"   # bridge replays history each turn
    SESSIONS_NONE   = "sessions_none"     # no session support; deep degrades to quick
    STREAMING       = "streaming"
    STRONG_MODEL    = "strong_model"      # urgency=blocker swaps in a stronger model
    EXACT_TOKENS    = "exact_tokens"      # token counts are exact, not estimated
```

`list_targets` returns the capability list per target. The calling agent reads it and adjusts behavior — e.g. don't bother with `urgency=deep` on a `SESSIONS_NONE` target unless you want the warning.

## Tools (unchanged from v2)

- `list_targets() -> list[Target]`
- `consult(target, brief, urgency, session_id?, stream?) -> ConsultResult`
- `open_session(target, purpose) -> {session_id, ttl_seconds}`
- `close_session(session_id) -> {closed: bool}`
- `list_sessions() -> list[Session]`
- `get_audit(limit?, target?, since?) -> list[AuditEntry]`

`Brief` schema also unchanged — the goal/tried/failing/ask contract is doing real work.

## ConsultResult (v2.1)

```python
class ConsultResult(BaseModel):
    response: str
    target: str
    model: str
    session_id: Optional[str] = None
    elapsed_ms: int
    tokens_in: TokenCount
    tokens_out: TokenCount
    outcome: Outcome
    truncated: bool = False        # response hit max_response_bytes
    warnings: list[str] = []       # e.g. "deep degraded to quick: target has SESSIONS_NONE"
```

```python
class TokenCount(BaseModel):
    value: int
    method: Literal["exact", "estimated", "unknown"]
```

## Outcomes (v2.1)

```python
class Outcome(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    REJECTED = "rejected"   # brief failed validation, never reached adapter
    BUSY = "busy"           # session lock held / target semaphore full
```

`BUSY` returns immediately. The calling agent decides whether to wait, retry, or give up — the bridge does not queue.

## Concurrency model

- **No global lock.** Two consults to two different targets run in parallel.
- **Per-session lock.** A session can have one in-flight call at a time. Second caller gets `BUSY` immediately.
- **Per-target semaphore** with `max_concurrent_per_target` (default 4). Bounds runaway agents spawning N subprocesses.
- **Stateless calls bypass session locks** but still count against the target semaphore.

Lock acquisition is non-blocking — if you can't get the lock, you get `BUSY`. No queueing, no head-of-line blocking.

## Sessions (v2.1)

Sessions get a turn cap to bound replay-adapter cost:

```python
class Session(BaseModel):
    session_id: str
    target: str
    purpose: str
    created_at: datetime
    last_used_at: datetime
    ttl_seconds: int = 1800
    max_turns: int = 8                  # NEW
    turn_count: int = 0
    closed: bool = False
    adapter_handle: Optional[str] = None
    history: list[Turn] = []            # only used for SESSIONS_REPLAY adapters
```

When `turn_count >= max_turns`, the next call returns `Outcome.REJECTED` with a clear warning. Caller can `close_session` and `open_session` again to start fresh. This is intentional friction — replay sessions get expensive fast.

For `SESSIONS_NATIVE` adapters, `history` stays empty; the underlying harness handles it. For `SESSIONS_REPLAY`, the bridge stores `Turn(role, content, ts)` entries and prepends them on each consult.

## Timeouts

```toml
[server.timeouts]
quick = 30        # seconds
deep = 180
blocker = 600
```

On timeout:
- Adapter task is cancelled (subprocess killed if applicable).
- `Outcome.TIMEOUT` returned with `elapsed_ms` set to the actual elapsed time.
- For `deep`/`blocker`, the session stays open. Caller can retry or `close_session`.
- Audit entry written with outcome and partial token counts if available.

## Response bounds

```toml
[targets.hermes]
max_response_bytes = 32768   # ~8K tokens
```

When a response exceeds the cap:
- Truncate at the boundary, append `\n[truncated: N bytes omitted]`.
- `ConsultResult.truncated = true`.
- Streaming: emit a final `done` chunk with `truncated: true`, stop forwarding.

## Configuration (`config/targets.toml`)

```toml
[server]
host = "127.0.0.1"
port = 7878
transport = "stdio"
audit_log = "state/audit.jsonl"
audit_bodies_dir = "state/audit-bodies"
max_context_bytes = 8192        # per attachment cap
max_attachments = 4

[server.timeouts]
quick = 30
deep = 180
blocker = 600

[targets.hermes]
adapter = "hermes"
kind = "CLI_SUBPROCESS"
command = ["hermes-cli", "--model", "mimo-7b"]
cwd = "/home/dan/hermes"
session_ttl_seconds = 1800
max_session_turns = 8
max_concurrent = 4
max_response_bytes = 32768
strong_model = "mimo-7b-instruct"

[targets.openclaw]
adapter = "openclaw"
kind = "MCP_PROXY"
mcp_url = "http://127.0.0.1:9001/mcp"
session_ttl_seconds = 1800
max_session_turns = 8
max_concurrent = 4
max_response_bytes = 32768

[targets.claude-api]
adapter = "claude_api"
kind = "HTTP_API"
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-opus-4-7"
max_concurrent = 8
max_response_bytes = 65536
```

Adding a target = TOML block + adapter file declaring its `kind`. No server changes.

## Adapter contract (v2.1)

```python
class AdapterKind(str, Enum):
    HTTP_API = "http_api"
    MCP_PROXY = "mcp_proxy"
    CLI_SUBPROCESS = "cli_subprocess"
    PTY_ATTACHED = "pty_attached"


class Adapter(ABC):
    id: str
    model: str
    kind: AdapterKind                  # class attribute, declared per adapter
    capabilities: frozenset[Capability]  # validated against kind at registration

    @abstractmethod
    async def health(self) -> Status: ...

    @abstractmethod
    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]: ...

    @abstractmethod
    async def open_session(self, purpose: str) -> Session: ...

    @abstractmethod
    async def close_session(self, session: Session) -> None: ...
```

Bridge passes `timeout_s` and `max_response_bytes` to the adapter — adapter is responsible for enforcing them at the protocol level it owns (subprocess kill, HTTP timeout, etc.). The bridge wraps the whole call in `asyncio.wait_for` as a backstop.

Capability validation at registration:
- `kind=CLI_SUBPROCESS` cannot claim `SESSIONS_NATIVE` or `EXACT_TOKENS`.
- `kind=HTTP_API` can claim anything (model-dependent).
- `kind=PTY_ATTACHED` cannot claim `EXACT_TOKENS`.
- All adapters must claim exactly one of `SESSIONS_NATIVE` / `SESSIONS_REPLAY` / `SESSIONS_NONE`.

Bridge fails to start if any adapter declares an inconsistent set. Better to crash at boot than serve lies via `list_targets`.

## Audit log format (v2.1)

```json
{"ts":"2026-05-08T14:22:01Z","target":"hermes","urgency":"blocker","session_id":"sess_abc","brief_hash":"sha256:...","elapsed_ms":4321,"tokens_in":{"value":412,"method":"estimated"},"tokens_out":{"value":883,"method":"estimated"},"outcome":"ok","truncated":false}
```

Body store: `state/audit-bodies/<brief_hash>.json` holds `{brief, response, warnings}`. Keeps the index grep-friendly while preserving full payloads for forensics.

## Transport (unchanged)

- Default: `stdio`
- Optional: `streamable-http` on `127.0.0.1:7878`
- Streaming via MCP progress notifications when `stream=true`

## Bazzite deployment (unchanged)

- Own Distrobox (`agent-bridge`)
- User systemd unit
- `journalctl --user -u agent-bridge` for logs
- Both harnesses register it in their MCP config

## What v2.1 deliberately still doesn't do

- No queueing — `BUSY` is the answer
- No multi-tenant auth — localhost only
- No cross-bridge federation
- No web UI
- No automatic model fallback (e.g. retry on a different target on `ERROR`) — caller's job

## Migration from v2

If you wrote any v2 code: only the protocol module changes. `Brief`, `Target`, the tool envelopes, and `Session` core fields are stable. Additions: `TokenCount`, `Outcome.BUSY`, `Capability` split, `Session.max_turns`, `ConsultResult.truncated`, `ConsultResult.warnings`, `AdapterKind`. Nothing renamed, nothing removed.
