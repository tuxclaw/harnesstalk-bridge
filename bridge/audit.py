"""Audit index and body store helpers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bridge.protocol import AuditEntry, Brief


@dataclass(slots=True)
class AuditQuery:
    """Filtering parameters for audit reads."""

    limit: int = 50
    target: str | None = None
    since: datetime | None = None


class AuditLog:
    """Write and query the bridge audit trail."""

    def __init__(
        self,
        log_path: str | Path = "state/audit.jsonl",
        bodies_dir: str | Path = "state/audit-bodies",
    ) -> None:
        self._log_path = Path(log_path)
        self._bodies_dir = Path(bodies_dir)

    async def write(
        self,
        entry: AuditEntry,
        brief: Brief,
        response: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Append one audit record and write the full body payload."""
        await asyncio.to_thread(
            self._write_sync,
            entry,
            brief,
            response,
            warnings or [],
        )

    def _write_sync(
        self,
        entry: AuditEntry,
        brief: Brief,
        response: str,
        warnings: list[str],
    ) -> None:
        """Synchronously write audit data from a worker thread."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._bodies_dir.mkdir(parents=True, exist_ok=True)

        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.to_jsonl())
            handle.write("\n")

        body_path = self._body_path(entry.brief_hash)
        if body_path.exists():
            return
        payload = {
            "brief": brief.model_dump(mode="json"),
            "response": response,
            "warnings": warnings,
        }
        body_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    async def query(self, query: AuditQuery) -> list[AuditEntry]:
        """Read audit entries in reverse chronological order."""
        if not self._log_path.exists():
            return []

        entries: list[AuditEntry] = []
        with self._log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = AuditEntry.model_validate_json(line)
                if query.target and entry.target != query.target:
                    continue
                if query.since and entry.ts < query.since:
                    continue
                entries.append(entry)

        entries.sort(key=lambda item: item.ts, reverse=True)
        return entries[: query.limit]

    def read_body(self, brief_hash: str) -> dict[str, object] | None:
        """Read the stored full body payload for a brief hash."""
        path = self._body_path(brief_hash)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _body_path(self, brief_hash: str) -> Path:
        safe_name = brief_hash.replace(":", "_")
        return self._bodies_dir / f"{safe_name}.json"
