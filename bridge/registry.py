"""Target registry with capability validation and concurrency limits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from bridge.protocol import (
    AdapterKind,
    Capability,
    Target,
    TargetStatus,
    validate_capabilities,
)


@dataclass(slots=True)
class TargetConfig:
    """Static configuration associated with one target."""

    target_id: str
    model: str
    kind: AdapterKind
    max_concurrent: int = 4
    max_response_bytes: int = 32_768
    session_ttl_seconds: int = 1_800
    max_session_turns: int = 8
    strong_model: str | None = None
    note: str | None = None


class TargetEntry:
    """Internal registry entry for a configured adapter."""

    def __init__(
        self,
        adapter: Any,
        config: TargetConfig,
        capabilities: frozenset[Capability],
    ) -> None:
        validate_capabilities(config.kind, capabilities)
        self.adapter = adapter
        self.config = config
        self.capabilities = capabilities
        self.status = TargetStatus.READY
        self._guard = asyncio.Lock()
        self._in_use = 0

    @property
    def target_id(self) -> str:
        return self.config.target_id

    async def try_acquire(self) -> bool:
        """Acquire a target slot without waiting."""
        async with self._guard:
            if self._in_use >= self.config.max_concurrent:
                return False
            self._in_use += 1
            return True

    async def release(self) -> None:
        """Release a previously acquired target slot."""
        async with self._guard:
            if self._in_use == 0:
                return
            self._in_use -= 1

    def to_target(self) -> Target:
        """Return the public target description."""
        capabilities = sorted(self.capabilities, key=lambda item: item.value)
        return Target(
            id=self.target_id,
            model=self.config.model,
            kind=self.config.kind,
            status=self.status,
            capabilities=capabilities,
            note=self.config.note,
        )


class Registry:
    """Manage registered consultation targets."""

    def __init__(self) -> None:
        self._targets: dict[str, TargetEntry] = {}

    def register(
        self,
        *,
        target_id: str,
        adapter: Any,
        model: str,
        kind: AdapterKind,
        capabilities: frozenset[Capability],
        max_concurrent: int = 4,
        max_response_bytes: int = 32_768,
        session_ttl_seconds: int = 1_800,
        max_session_turns: int = 8,
        strong_model: str | None = None,
        note: str | None = None,
    ) -> TargetEntry:
        """Register a target and validate its capability claim."""
        if target_id in self._targets:
            raise ValueError(f"target already registered: {target_id}")

        config = TargetConfig(
            target_id=target_id,
            model=model,
            kind=kind,
            max_concurrent=max_concurrent,
            max_response_bytes=max_response_bytes,
            session_ttl_seconds=session_ttl_seconds,
            max_session_turns=max_session_turns,
            strong_model=strong_model,
            note=note,
        )
        entry = TargetEntry(
            adapter=adapter,
            config=config,
            capabilities=capabilities,
        )
        self._targets[target_id] = entry
        return entry

    def get(self, target_id: str) -> TargetEntry | None:
        """Return a target entry by identifier."""
        return self._targets.get(target_id)

    def list_targets(self) -> list[Target]:
        """Return all registered public target descriptions."""
        return [
            self._targets[key].to_target()
            for key in sorted(self._targets)
        ]

    def items(self) -> list[tuple[str, TargetEntry]]:
        """Return all registry items."""
        return list(self._targets.items())
