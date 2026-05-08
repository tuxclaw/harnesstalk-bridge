# HarnessTalk-Bridge

A local [MCP](https://modelcontextprotocol.io) server that lets AI agent harnesses consult each other mid-task. The active agent writes a structured brief, hands it to the bridge, and gets a response back as a tool result.

```
┌──────────────────┐         ┌──────────────────┐
│ OpenClaw         │         │ Hermes           │
│                  │         │                  │
│ tool: consult ───┼────┐ ┌──┼── tool: consult  │
└──────────────────┘    │ │  └──────────────────┘
                        ▼ ▼
               ┌─────────────────────┐
               │ HarnessTalk-Bridge  │
               │ (MCP server)        │
               │                     │
               │ ├─ adapter: hermes  │
               │ ├─ adapter: openclaw│
               │ └─ adapter: claude  │
               └─────────────────────┘
```

## Features

- **Bidirectional & symmetric** — any target can call any other target
- **Structured briefs** — goal/tried/failing/ask format forces clear questions
- **Urgency tiers** — `quick` (stateless), `deep` (persistent session), `blocker` (stronger model)
- **Honest capabilities** — adapters declare what they can actually do; the bridge validates at boot
- **Non-blocking concurrency** — per-session locks + per-target semaphores, `BUSY` on contention
- **Bounded responses** — `max_response_bytes` per target, truncation surfaced as a flag
- **Audit log** — JSONL index + body store for forensics
- **Bazzite-friendly** — everything under `~/.local` and `~/.config`, no layered packages

## Requirements

- Python 3.11+
- A configured target file at `config/targets.toml`

## Quick Start

```bash
# Clone
git clone git@github.com:tuxclaw/harnesstalk-bridge.git
cd harnesstalk-bridge

# Setup
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Configure
cp config/targets.toml config/targets.toml.local
# Edit targets.toml with your actual targets

# Run (stdio — for harnesses that spawn the server)
harnesstalk-bridge

# Run (HTTP — for shared server instance)
harnesstalk-bridge --transport streamable-http
```

## Configuration

All config lives in `config/targets.toml`:

```toml
[server]
host = "127.0.0.1"
port = 7878
transport = "stdio"
audit_log = "state/audit.jsonl"
audit_bodies_dir = "state/audit-bodies"
sessions_path = "state/sessions.json"
max_context_bytes = 8192
max_attachments = 4

[server.timeouts]
quick = 30        # seconds
deep = 180
blocker = 600

[server.health]
lazy_cache_seconds = 60
polled_interval_seconds = 15
check_timeout_seconds = 10

[targets.hermes]
adapter = "hermes"
kind = "CLI_SUBPROCESS"
command = ["hermes", "chat", "--quiet"]
cwd = "/home/tux"
model = "mimo-7b"
session_ttl_seconds = 1800
max_session_turns = 8
max_concurrent = 4
max_response_bytes = 32768
strong_model = "mimo-7b-instruct"

[targets.claude-api]
adapter = "claude_api"
kind = "HTTP_API"
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-opus-4-7"
strong_model = "claude-opus-4-7"
max_tokens = 4096
session_ttl_seconds = 3600
max_session_turns = 16
max_concurrent = 8
max_response_bytes = 65536
```

### Adding a Target

Add a TOML block + create an adapter file in `adapters/`. No server changes needed.

### Environment Variables

| Var | Default | Purpose |
|---|---|---|
| `AGENT_BRIDGE_HTTP_PORT` | `7878` | Streamable-HTTP listen port |
| `AGENT_BRIDGE_TIMEOUT_S` | `120` | Default adapter timeout |

Adapter-specific config (e.g. `ANTHROPIC_API_KEY` for Claude) lives in the adapter module.

## Tools

The bridge exposes six MCP tools:

### `list_targets`

Returns all configured targets with their capabilities and status.

### `consult(target, brief, urgency, session_id?, stream?)`

Hand a brief to a target agent, block on response.

**Brief schema:**
```json
{
  "goal": "What you're trying to accomplish",
  "tried": ["What you've already attempted"],
  "failing": "The specific thing that's not working",
  "ask": "The precise question for the other agent"
}
```

**Urgency:**
- `quick` — stateless one-shot, no history retained
- `deep` — persistent session, follow-ups via `session_id`
- `blocker` — like `deep` but may swap to a stronger model

### `open_session(target, purpose, max_turns?)`

Create a persistent session for follow-up consultations.

### `close_session(session_id)`

Tear down a persistent session.

### `list_sessions()`

Show active sessions with metadata.

### `get_audit(limit?, target?, since?)`

Query the audit log for past consultations.

## Response Shape

All `consult` calls return:

```json
{
  "response": "string",
  "target": "hermes",
  "model": "mimo-7b",
  "session_id": "sess_abc123",
  "elapsed_ms": 4321,
  "tokens_in": { "value": 412, "method": "estimated" },
  "tokens_out": { "value": 883, "method": "estimated" },
  "outcome": "ok",
  "truncated": false,
  "warnings": []
}
```

### Outcomes

| Outcome | Meaning |
|---|---|
| `ok` | Success |
| `error` | Adapter error (see warnings) |
| `timeout` | Adapter didn't respond in time |
| `rejected` | Brief validation failed or session turn cap hit |
| `busy` | Session lock held or target semaphore full |

## Adapter Taxonomy

Each adapter declares its `kind`, which constrains valid capabilities:

| Kind | Mechanism | Sessions | Tokens |
|---|---|---|---|
| `HTTP_API` | requests/SDK | native | exact |
| `MCP_PROXY` | calls target's MCP server | depends on harness | from harness |
| `CLI_SUBPROCESS` | spawn + stdin/stdout | replay or native | estimated |
| `PTY_ATTACHED` | pexpect against REPL | fragile | unknown |

### Writing an Adapter

```python
from adapters.base import Adapter
from bridge.protocol import AdapterKind, Capability, Brief, Session, ConsultChunk

class MyAdapter(Adapter):
    id = "my-target"
    kind = AdapterKind.HTTP_API
    capabilities = frozenset({Capability.SESSIONS_NATIVE, Capability.EXACT_TOKENS})
    model = "my-model"

    async def health(self) -> TargetStatus: ...
    async def consult(self, brief, urgency, session, timeout_s, max_response_bytes) -> AsyncIterator[ConsultChunk]: ...
    async def open_session(self, purpose) -> Session: ...
    async def close_session(self, session) -> None: ...
```

## Systemd Service

For persistent deployment:

```bash
cp systemd/agent-bridge.service ~/.config/systemd/user/
systemctl --user enable --now agent-bridge
journalctl --user -u agent-bridge -f
```

## Harness Registration

### OpenClaw

Register the bridge as an MCP server in your OpenClaw config. For HTTP transport:

```json
{
  "mcpServers": {
    "agent-bridge": {
      "url": "http://127.0.0.1:7878"
    }
  }
}
```

### Hermes

Register `http://127.0.0.1:7878` as a streamable-HTTP MCP server in your Hermes config.

The Hermes adapter uses **native sessions** (`SESSIONS_NATIVE`): it captures session ids from
`~/.hermes/state.db` after each spawn and passes `--resume <id>` on follow-up consults.
Bridge-side replay history is unused — Hermes manages its own context.

## Project Structure

```
harnesstalk-bridge/
├── server.py                 # FastMCP server + AgentBridge orchestrator
├── bridge/
│   ├── protocol.py           # Pydantic models (single source of types)
│   ├── registry.py           # Target registration, capability validation
│   ├── sessions.py           # Session lifecycle, TTL, per-session locks
│   ├── audit.py              # JSONL audit log + body store
│   └── config.py             # TOML config loader
├── adapters/
│   ├── base.py               # Adapter ABC + shared utilities
│   ├── hermes.py             # CLI_SUBPROCESS adapter
│   ├── openclaw.py           # MCP_PROXY adapter
│   └── claude_api.py         # HTTP_API adapter
├── config/targets.toml       # Target configuration
├── tests/                    # pytest + pytest-asyncio
├── systemd/                  # User systemd unit
└── pyproject.toml
```

## Development

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Test
pytest

# Lint (if configured)
ruff check .
```

## Security

- **Loopback-only** — HTTP transport binds to `127.0.0.1` by default
- **No auth** — local-only, trusted environment
- **Context not logged** — brief `context` field logged at DEBUG only
- **Subprocess isolation** — adapters run as the calling user, no privilege escalation

## License

MIT
