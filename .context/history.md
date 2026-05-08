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
