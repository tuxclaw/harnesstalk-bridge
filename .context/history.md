# History

## [2026-05-08] Project Created
**Agent:** Jack (orchestrator)
**Branch:** main (initial setup)
**Changes:** Scaffolded project directory, copied protocol.py (provided by Opus), seeded .context/
**Files:** bridge/protocol.py, SPEC.md, .context/

## [2026-05-08] Review Findings Fixed
**Agent:** Helen
**Branch:** master
**Changes:** Fixed Frozone review findings across registry concurrency, Claude history retention, timeout ownership, response bounding, async persistence, pytest-asyncio, audit input validation, keyword-only registry registration, and HTTP client pooling.
**Verification:** `python -m pytest tests/ -v` — 11 passed.

## [2026-05-08] Hermes Native Sessions v2.2 Patch
**Agent:** Helen
**Branch:** andy/hermes-sessions-native
**Changes:** Flipped Hermes adapter from `SESSIONS_REPLAY` to `SESSIONS_NATIVE`; added read-only SQLite schema validation for `~/.hermes/state.db`; `open_session` now spawns `hermes chat -q <purpose> --quiet` and captures the newest native session id; `consult` resumes via `--resume <session_id>` without replay history; close marks bridge session closed only.
**Tests:** Added Hermes native-session coverage for CLI_SUBPROCESS capability validation, no-replay consult parity, and multi-turn prompt-size regression.

## [2026-05-08] v3 Upgrade
**Agent:** Helen
**Branch:** andy/v3-upgrade
**Changes:** Updated consumers for v3 protocol/registry APIs; fixed bridge exports; updated Hermes/OpenClaw health to return `HealthStatus`; rewrote Claude API adapter for direct Anthropic streaming, exact token accounting, in-memory sessions, health probing, truncation, and blocker strong-model routing; added `bridge/streaming.py`; rewired server consult/list_targets/build paths to v3 registry and streaming wrapper; added `[server.health]` config parsing and config examples.
**Tests:** Added v3 health state-machine, Claude API adapter, and streaming wrapper coverage. `python -m pytest tests/ -q` — 33 passed.

## [2026-05-08] v3.1 TUI Inspector
**Agent:** Dash
**Branch:** andy/v3.1-tui
**Changes:** Added read-only Textual TUI package with targets, sessions, audit panels, Dracula styling, read-only HTTP MCP client, audit filter parser, `AuditEntry.id`, `[tui]` config parsing, CLI/script wiring, and README TUI section.
**Verification:** `.venv/bin/python -m pytest tests/ -v` — 40 passed.
