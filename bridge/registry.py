"""Target registry for Agent Bridge MCP (v3).

Owns:
  - Adapter registration and capability validation at startup.
  - Hybrid health check state machine (LAZY / CHECKING / POLLED).
  - Per-target concurrency semaphores.
  - Per-session locks (delegated coordination with bridge.sessions).

Does NOT own:
  - Session lifecycle / persistence (bridge.sessions)
  - Audit logging (bridge.audit)
  - The MCP server surface (server.py)

State machine summary:

       first list_targets
            |
            v
       ┌──────────┐  unhealthy check  ┌──────────────┐
   ┌──>│  LAZY    │──────────────────>│  POLLED      │
   │   │ (idle,   │                   │ (background  │
   │   │  cached) │<──────────────────│  poller, 15s)│
   │   └──────────┘  one healthy chk  └──────────────┘
   │        │
   │        │ list_targets after cache TTL
   │        v
   │   ┌──────────┐
   └───│ CHECKING │
       │(in-flight)│
       └──────────┘

Single-flight for CHECKING: concurrent list_targets calls during an
in-flight health check share the result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .protocol import (
    Adapter,
    AdapterKind,
    Capability,
    HealthStatus,
    Target,
    TargetStatus,
    validate_capabilities,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthConfig:
    """Health check tuning. Wired from [server.health] in targets.toml."""

    lazy_cache_seconds: float = 60.0
    polled_interval_seconds: float = 15.0
    check_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class TargetLimits:
    """Per-target concurrency and bounds. From [targets.<id>] in targets.toml."""

    max_concurrent: int = 4
    max_response_bytes: int = 32 * 1024
    session_ttl_seconds: int = 1800
    max_session_turns: int = 8


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


class _LifecycleState(str, Enum):
    """Where in the health state machine a target currently sits.

    Distinct from TargetStatus (which is what list_targets returns).
    A target can be in lifecycle=LAZY with status=READY (normal) or
    lifecycle=POLLED with status=DEGRADED (background recovery).
    """

    LAZY = "lazy"
    CHECKING = "checking"
    POLLED = "polled"


@dataclass
class _TargetEntry:
    """Everything the registry tracks per registered target."""

    adapter: Adapter
    limits: TargetLimits

    # Concurrency: bounds parallel consults to this target.
    semaphore: asyncio.Semaphore

    # Lifecycle state machine.
    lifecycle: _LifecycleState = _LifecycleState.LAZY

    # Last known health. None means "never checked since startup."
    last_health: Optional[HealthStatus] = None

    # Single-flight: when CHECKING, this future resolves to the new health.
    in_flight: Optional[asyncio.Future[HealthStatus]] = None

    # Set when lifecycle=POLLED. Cancelled on transition to LAZY.
    poller_task: Optional[asyncio.Task[None]] = None

    # asyncio lock guarding state transitions for this target. Held only
    # for the duration of state mutation, never across health() calls.
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Registry:
    """The single source of truth for what targets exist and how healthy
    they are. Wired by server.py at startup; queried on every consult and
    every list_targets.
    """

    def __init__(self, health_config: HealthConfig | None = None):
        self._entries: dict[str, _TargetEntry] = {}
        self._health_config = health_config or HealthConfig()
        self._closed = False

    # -- Registration ------------------------------------------------------

    def register(self, adapter: Adapter, limits: TargetLimits) -> None:
        """Register an adapter. Validates capabilities against kind.

        Raises ValueError if the capability set is inconsistent with kind
        (e.g. CLI_SUBPROCESS claiming EXACT_TOKENS). Better to crash at
        boot than serve lies via list_targets later.
        """
        if self._closed:
            raise RuntimeError("registry is closed")
        if adapter.id in self._entries:
            raise ValueError(f"target {adapter.id!r} already registered")

        # Will raise ValueError on mismatch. Caller (server bootstrap)
        # should let this propagate to fail startup.
        validate_capabilities(adapter.kind, adapter.capabilities)

        entry = _TargetEntry(
            adapter=adapter,
            limits=limits,
            semaphore=asyncio.Semaphore(limits.max_concurrent),
        )
        self._entries[adapter.id] = entry
        log.info(
            "registered target id=%s kind=%s caps=%s max_concurrent=%d",
            adapter.id,
            adapter.kind.value,
            sorted(c.value for c in adapter.capabilities),
            limits.max_concurrent,
        )

    # -- Lookup ------------------------------------------------------------

    def get_adapter(self, target_id: str) -> Adapter:
        """Return the adapter for `target_id`, or raise KeyError."""
        return self._entries[target_id].adapter

    def get_limits(self, target_id: str) -> TargetLimits:
        return self._entries[target_id].limits

    def get_semaphore(self, target_id: str) -> asyncio.Semaphore:
        """Per-target concurrency semaphore. Use in non-blocking mode:

            sem = registry.get_semaphore(target_id)
            if not sem.locked() and sem._value > 0:  # don't actually do this
                ...

        Better pattern: try acquire with timeout=0:

            try:
                await asyncio.wait_for(sem.acquire(), timeout=0)
            except asyncio.TimeoutError:
                return ConsultResult(outcome=Outcome.BUSY, ...)
            try:
                ...do consult...
            finally:
                sem.release()
        """
        return self._entries[target_id].semaphore

    def has(self, target_id: str) -> bool:
        return target_id in self._entries

    def all_ids(self) -> list[str]:
        return list(self._entries.keys())

    # -- list_targets surface ---------------------------------------------

    async def list_targets(self) -> list[Target]:
        """Return current state of all targets.

        Triggers health checks for any target whose cache has expired.
        Concurrent calls share in-flight checks (single-flight).
        Pollers running in the background do not block this call.
        """
        results = await asyncio.gather(
            *(self._target_snapshot(tid) for tid in self._entries),
            return_exceptions=False,
        )
        return list(results)

    async def _target_snapshot(self, target_id: str) -> Target:
        """Build a Target view of a single entry. Triggers a health check
        if the cached status is stale and we're in LAZY.
        """
        entry = self._entries[target_id]
        health = await self._get_or_check_health(entry)
        adapter = entry.adapter
        return Target(
            id=adapter.id,
            model=adapter.model,
            kind=adapter.kind,
            status=health.status,
            capabilities=sorted(adapter.capabilities, key=lambda c: c.value),
            note=health.error_message,
            last_checked_at=health.checked_at,
            latency_ms=health.latency_ms,
        )

    # -- Health state machine ---------------------------------------------

    async def _get_or_check_health(self, entry: _TargetEntry) -> HealthStatus:
        """Return cached health, or trigger a new check, depending on
        lifecycle state and cache age.

        Single-flight guarantee: if a check is in flight (lifecycle=CHECKING),
        all callers await the same future.
        """
        # POLLED: a background poller is keeping last_health fresh.
        # Just return the cached value, regardless of age.
        if entry.lifecycle == _LifecycleState.POLLED:
            assert entry.last_health is not None
            return entry.last_health

        # CHECKING: an in-flight check is happening. Join it.
        if entry.lifecycle == _LifecycleState.CHECKING:
            assert entry.in_flight is not None
            return await entry.in_flight

        # LAZY: check cache age. If fresh, return cached. If stale or
        # missing, trigger a check.
        if (
            entry.last_health is not None
            and self._cache_is_fresh(entry.last_health)
        ):
            return entry.last_health

        # Need to start a check. Acquire state lock to transition to
        # CHECKING atomically (so we don't double-fire on concurrent calls).
        async with entry.state_lock:
            # Re-check after acquiring lock — another caller may have
            # transitioned us to CHECKING already.
            if entry.lifecycle == _LifecycleState.CHECKING:
                assert entry.in_flight is not None
                future = entry.in_flight
            elif (
                entry.last_health is not None
                and self._cache_is_fresh(entry.last_health)
            ):
                return entry.last_health
            else:
                future = asyncio.get_running_loop().create_future()
                entry.in_flight = future
                entry.lifecycle = _LifecycleState.CHECKING
                # Spawn the actual check outside the lock.
                asyncio.create_task(
                    self._run_check_and_resolve(entry, future),
                    name=f"health-check-{entry.adapter.id}",
                )

        return await future

    async def _run_check_and_resolve(
        self,
        entry: _TargetEntry,
        future: asyncio.Future[HealthStatus],
    ) -> None:
        """Execute health() with timeout, update entry, resolve future,
        possibly start/stop poller. Always sets the future, even on error.
        """
        target_id = entry.adapter.id
        try:
            health = await self._do_health_check(entry)
        except Exception as exc:  # belt-and-suspenders: _do_health_check shouldn't raise
            log.exception("health check raised for %s", target_id)
            health = HealthStatus(
                status=TargetStatus.UNREACHABLE,
                checked_at=_now(),
                error_message=f"health check raised: {type(exc).__name__}: {exc}",
            )

        # Update entry state under the lock.
        async with entry.state_lock:
            previous = entry.last_health
            entry.last_health = self._merge_streaks(previous, health)
            entry.in_flight = None

            if health.status == TargetStatus.READY:
                # Healthy. If we were polled, stop the poller and go LAZY.
                if entry.poller_task is not None:
                    log.info("target %s recovered, stopping poller", target_id)
                    entry.poller_task.cancel()
                    entry.poller_task = None
                entry.lifecycle = _LifecycleState.LAZY
            else:
                # Unhealthy. Start polling if we aren't already.
                if entry.poller_task is None:
                    log.warning(
                        "target %s unhealthy (%s), starting poller",
                        target_id,
                        health.status.value,
                    )
                    entry.poller_task = asyncio.create_task(
                        self._poller_loop(entry),
                        name=f"health-poller-{target_id}",
                    )
                entry.lifecycle = _LifecycleState.POLLED

        if not future.done():
            future.set_result(entry.last_health)

    async def _poller_loop(self, entry: _TargetEntry) -> None:
        """Background poll for an unhealthy target. Cancelled on recovery
        or registry shutdown.
        """
        target_id = entry.adapter.id
        interval = self._health_config.polled_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                if self._closed:
                    return

                # Run a check. Uses the same machinery as a lazy check —
                # we transition to CHECKING, run health(), resolve.
                # The CHECKING transition is what handles single-flight
                # if a list_targets call lands during the poll.
                async with entry.state_lock:
                    # If we somehow recovered already, exit. (Shouldn't
                    # happen since this task gets cancelled on recovery,
                    # but defensive check.)
                    if entry.lifecycle == _LifecycleState.LAZY:
                        return
                    # If a check is already in flight (e.g. list_targets
                    # raced with us), join it instead of starting another.
                    if entry.lifecycle == _LifecycleState.CHECKING:
                        future = entry.in_flight
                        assert future is not None
                    else:
                        future = asyncio.get_running_loop().create_future()
                        entry.in_flight = future
                        entry.lifecycle = _LifecycleState.CHECKING
                        asyncio.create_task(
                            self._run_check_and_resolve(entry, future),
                            name=f"health-check-{target_id}-poll",
                        )

                # Wait for resolution outside the lock.
                try:
                    await future
                except Exception:
                    log.exception("poller check failed for %s", target_id)
                    # Loop continues; next iteration retries.
        except asyncio.CancelledError:
            log.debug("poller for %s cancelled", target_id)
            raise

    async def _do_health_check(self, entry: _TargetEntry) -> HealthStatus:
        """Run the adapter's health() with a timeout. Never raises —
        timeouts and errors become UNREACHABLE/DEGRADED HealthStatus.
        """
        timeout = self._health_config.check_timeout_seconds
        adapter = entry.adapter
        started = time.monotonic()
        try:
            health = await asyncio.wait_for(adapter.health(), timeout=timeout)
            # Adapter is responsible for setting checked_at and latency_ms,
            # but tolerate sloppy adapters by filling in defaults.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if health.latency_ms is None:
                health = health.model_copy(update={"latency_ms": elapsed_ms})
            return health
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return HealthStatus(
                status=TargetStatus.DEGRADED,
                checked_at=_now(),
                latency_ms=elapsed_ms,
                error_message=f"health check timed out after {timeout}s",
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return HealthStatus(
                status=TargetStatus.UNREACHABLE,
                checked_at=_now(),
                latency_ms=elapsed_ms,
                error_message=f"{type(exc).__name__}: {exc}",
            )

    def _cache_is_fresh(self, health: HealthStatus) -> bool:
        age = (_now() - health.checked_at).total_seconds()
        return age < self._health_config.lazy_cache_seconds

    @staticmethod
    def _merge_streaks(
        previous: Optional[HealthStatus],
        current: HealthStatus,
    ) -> HealthStatus:
        """Carry forward consecutive_* counters across checks.

        Not used for any logic at v3 (no debouncing), but populated for
        future use and visible via get_audit / debugging.
        """
        if previous is None:
            return current.model_copy(
                update={
                    "consecutive_healthy": 1 if current.status == TargetStatus.READY else 0,
                    "consecutive_unhealthy": 0 if current.status == TargetStatus.READY else 1,
                }
            )
        if current.status == TargetStatus.READY:
            healthy = previous.consecutive_healthy + 1
            unhealthy = 0
        else:
            healthy = 0
            unhealthy = previous.consecutive_unhealthy + 1
        return current.model_copy(
            update={
                "consecutive_healthy": healthy,
                "consecutive_unhealthy": unhealthy,
            }
        )

    # -- Shutdown ----------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel all pollers, stop accepting new registrations.

        Does not wait for in-flight consults; that's the caller's job
        (server.py shuts down sessions before closing the registry).
        """
        self._closed = True
        tasks: list[asyncio.Task[None]] = []
        for entry in self._entries.values():
            if entry.poller_task is not None:
                entry.poller_task.cancel()
                tasks.append(entry.poller_task)
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("registry closed, %d pollers cancelled", len(tasks))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "HealthConfig",
    "TargetLimits",
    "Registry",
]
