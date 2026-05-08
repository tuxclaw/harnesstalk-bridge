# Decisions

## [2026-05-08] Initial Architecture
**By:** Jack (orchestrator), with Opus (spec author)
**Context:** Tux wants a local MCP server that lets OpenClaw and Hermes consult each other mid-task.
**Decision:** Follow the v2.1 spec exactly. Python + FastMCP + Pydantic v2. protocol.py is already written (provided by Opus) — use it as-is.
**Key decisions from spec:**
- Adapter taxonomy: CLI_SUBPROCESS for Hermes, MCP_PROXY for OpenClaw, HTTP_API for Claude API optional
- Non-blocking concurrency: per-session lock + per-target semaphore, BUSY on contention
- Session turn cap (default 8) to bound replay cost
- Per-urgency timeouts: quick=30s, deep=180s, blocker=600s
- Bounded responses with truncation warning
- Audit log: JSONL index + body store for forensics
- Bazzite-friendly: everything under ~/.local and ~/.config

## [2026-05-08] Hermes Adapter Strategy
**By:** Jack + Opus (spec update)
**Context:** Hermes CLI is interactive (`hermes chat`), but persists sessions in `~/.hermes/` SQLite and supports `--resume <session_id>`.
**Decision:** CLI_SUBPROCESS kind with SESSIONS_NATIVE capability. Commands:
- Open session: `hermes chat -q "<seed prompt>" --quiet` → parse session id from `~/.hermes/sessions.db`
- Consult: `hermes chat --resume <session_id> -q "<brief>" --quiet`
- Stateless quick: `hermes chat -q "<brief>" --quiet --ignore-user-config`
- Blocker: add `--model <strong_model>`
- Prefer reading sessions.db over scraping stderr for session id capture
**Status:** Active

## [2026-05-08] OpenClaw Adapter Strategy
**By:** Jack
**Context:** OpenClaw has built-in MCP support and session spawning.
**Decision:** Use MCP_PROXY kind. The adapter calls OpenClaw's MCP server to spawn sessions and send messages. Declare SESSIONS_NATIVE capability if OpenClaw sessions are resumable, otherwise SESSIONS_REPLAY.
**Status:** Active — needs investigation during build

## [2026-05-08] Review Hardening Decisions
**By:** Helen
**Context:** Frozone identified concurrency, timeout, I/O, and maintainability risks in the first Agent Bridge MCP implementation.
**Decisions:**
- Use guarded `_in_use` accounting only for target concurrency instead of dual semaphore + counter state.
- Let adapters own exact request timeouts while the bridge uses a 5-second cleanup grace period.
- Centralize response byte bounding in `adapters/base.py`.
- Keep pooled `httpx.AsyncClient` instances on HTTP adapters and expose adapter-level `close()` for cleanup.
**Status:** Active

## [2026-05-08] v2.2 Patch — SESSIONS_NATIVE Upgrade
**By:** Jack (orchestrator)
**Context:** v2.2 spec calls for flipping Hermes adapter from SESSIONS_REPLAY to SESSIONS_NATIVE. Discovery checklist run first.
**Key findings from discovery:**
- **DB path correction:** Spec says `~/.hermes/sessions.db` but actual session store is `~/.hermes/state.db`. The `sessions.db` file is 0 bytes.
- **Schema:** `sessions` table with `id` TEXT PK (format: `YYYYMMDD_HHMMSS_hex6`), `source`, `started_at` REAL, `ended_at` REAL, `end_reason`, `message_count`, token count columns, `title`.
- **`--resume SESSION_ID`** confirmed on `hermes chat --help`.
- **`--quiet` / `-Q`** suppresses banner/spinner, outputs final response + session info.
- **Session JSONL files** also in `~/.hermes/sessions/` but the relational store is `state.db`.
**Decision:** Use Path A (read SQLite). DB path = `~/.hermes/state.db`. Query `sessions` table ordered by `started_at DESC LIMIT 1` after spawn to capture session id. Set `HERMES_DB_PATH` constant with schema-version check at init.
**Tests required:** 3 tests per spec (capability validation, single-turn parity, multi-turn token regression).
**Status:** Active
