# Task: Build Agent Bridge MCP Server

## What You're Building
A local MCP server (`agent-bridge`) that lets AI agent harnesses (OpenClaw, Hermes, optionally Claude API) consult each other mid-task. The calling agent writes a structured brief, hands it to the bridge, and gets a response back as a tool result.

## Source of Truth
- **Spec:** `SPEC.md` in this directory — READ IT FIRST, it's the complete v2.1 specification
- **Protocol models:** `bridge/protocol.py` — ALREADY WRITTEN by Opus. DO NOT rewrite. Import from it.
- **Context:** `.context/decisions.md`, `.context/notes.md`

## Stack
- **Language:** Python 3.11+
- **Framework:** FastMCP (for MCP server)
- **Models:** Pydantic v2 (protocol.py already uses it)
- **Config:** TOML (`config/targets.toml`)
- **Concurrency:** asyncio
- **Style:** PEP 8, Google Python style guide

## What To Build (in order)

### 1. Project Setup
- `pyproject.toml` with dependencies: `fastmcp`, `pydantic>=2.0`, `tomli` (Python <3.11) or `tomllib` (3.11+), `uvicorn` (for HTTP transport)
- `README.md` with setup instructions

### 2. Bridge Core (`bridge/`)
- `bridge/__init__.py`
- `bridge/registry.py` — Target registration, capability discovery, per-target semaphores, `validate_capabilities()` call at registration
- `bridge/sessions.py` — Session lifecycle, TTL expiry, per-session locks, turn cap enforcement, `Turn` history for replay adapters
- `bridge/audit.py` — JSONL audit log writer + body store (writes to `state/audit.jsonl` and `state/audit-bodies/`)

### 3. Adapter Base (`adapters/`)
- `adapters/__init__.py`
- `adapters/base.py` — `Adapter` ABC from spec, `AdapterKind` enum, `ConsultChunk` streaming protocol

### 4. Hermes Adapter (`adapters/hermes.py`)
- **Kind:** CLI_SUBPROCESS
- **Capabilities:** SESSIONS_REPLAY, STRONG_MODEL (if `strong_model` configured)
- **How it works:** Spawns `hermes chat` as a subprocess, sends the brief as input, reads response from stdout
- **Key challenge:** Hermes CLI is interactive. Investigate: does `hermes` support piped input? A `--message` flag? If not, use PTY_ATTACHED kind with pexpect.
- **Config:** `command`, `cwd`, `strong_model` from targets.toml
- **Session replay:** Store `Turn` entries in bridge session history, prepend on each consult
- **Token estimation:** Use `tiktoken` or rough char/4 estimate

### 5. OpenClaw Adapter (`adapters/openclaw.py`)
- **Kind:** MCP_PROXY
- **Capabilities:** SESSIONS_NATIVE (OpenClaw has real sessions), STRONG_MODEL
- **How it works:** Calls OpenClaw's MCP server via HTTP to spawn sessions and send messages
- **Config:** `mcp_url` from targets.toml
- **Investigate:** How does OpenClaw expose "send a prompt, get a response" via MCP? Check if `sessions_spawn` / `sessions_send` are available as MCP tools.

### 6. Claude API Adapter (`adapters/claude_api.py`)
- **Kind:** HTTP_API
- **Capabilities:** SESSIONS_NATIVE, EXACT_TOKENS, STREAMING, STRONG_MODEL
- **How it works:** Direct Anthropic API calls via `httpx` or `anthropic` SDK
- **Config:** `api_key_env`, `model` from targets.toml
- **Sessions:** Use Anthropic's conversation array (native sessions)

### 7. MCP Server (`server.py`)
- FastMCP server with these tools:
  - `list_targets()` — returns all registered targets with capabilities
  - `consult(target, brief, urgency, session_id?, stream?)` — the main tool
  - `open_session(target, purpose, max_turns?)` — create persistent session
  - `close_session(session_id)` — tear down session
  - `list_sessions()` — show active sessions
  - `get_audit(limit?, target?, since?)` — query audit log
- Dual transport: stdio (default) + streamable-HTTP on 127.0.0.1:7878
- Load config from `config/targets.toml`

### 8. Config (`config/targets.toml`)
- Example config with hermes, openclaw, claude-api targets
- All settings from spec (timeouts, max_response_bytes, max_concurrent, etc.)

### 9. Systemd Unit (`systemd/agent-bridge.service`)
- User unit for Bazzite (everything under ~/.local, ~/.config)
- From spec

### 10. Tests (`tests/`)
- Unit tests for registry, sessions, audit
- Integration test with a mock adapter

## Rules
- **DO NOT rewrite bridge/protocol.py** — it's done. Import from it.
- **PEP 8 style** — 79 char line limit, descriptive names, type hints everywhere
- **No unnecessary deps** — justify every package
- **All imports explicit** — no wildcard imports
- **Error handling** — use MCP ToolError for tool-facing errors, log internally
- **Docstrings** — Google style (Args, Returns, Raises)
- **async/await** — all adapter methods are async
- **Security** — loopback-only, no auth (local-only), context logged at DEBUG only

## Acceptance Criteria
1. `server.py` starts without errors (stdio mode)
2. `list_targets` returns configured targets with correct capabilities
3. `consult` with a mock adapter returns a valid `ConsultResult`
4. Session lifecycle works: open → consult → consult → close
5. Turn cap enforcement: REJECTED after max_turns
6. Audit log writes JSONL entries correctly
7. Per-target semaphore returns BUSY when saturated
8. Config loads from targets.toml correctly
