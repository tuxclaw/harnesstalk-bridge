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
**By:** Jack
**Context:** Hermes CLI is interactive (`hermes chat`). Need non-interactive prompt→response.
**Decision:** Use CLI_SUBPROCESS kind. Investigate if `hermes chat` supports piped input or a `--message` flag. If not, use PTY_ATTACHED as fallback. Adapter should declare SESSIONS_REPLAY capability (bridge replays history each turn).
**Status:** Active — needs investigation during build

## [2026-05-08] OpenClaw Adapter Strategy
**By:** Jack
**Context:** OpenClaw has built-in MCP support and session spawning.
**Decision:** Use MCP_PROXY kind. The adapter calls OpenClaw's MCP server to spawn sessions and send messages. Declare SESSIONS_NATIVE capability if OpenClaw sessions are resumable, otherwise SESSIONS_REPLAY.
**Status:** Active — needs investigation during build
