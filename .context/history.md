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
