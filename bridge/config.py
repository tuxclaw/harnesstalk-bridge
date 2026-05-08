"""TOML configuration loading for Agent Bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(slots=True)
class TimeoutConfig:
    """Per-urgency timeout settings in seconds."""

    quick: int = 30
    deep: int = 180
    blocker: int = 600


@dataclass(slots=True)
class HealthConfigData:
    """Server health-check settings in seconds."""

    lazy_cache_seconds: float = 60.0
    polled_interval_seconds: float = 15.0
    check_timeout_seconds: float = 10.0


@dataclass(slots=True)
class ServerConfig:
    """Server-level configuration."""

    host: str = "127.0.0.1"
    port: int = 7878
    transport: str = "stdio"
    audit_log: str = "state/audit.jsonl"
    audit_bodies_dir: str = "state/audit-bodies"
    sessions_path: str = "state/sessions.json"
    max_context_bytes: int = 8_192
    max_attachments: int = 4
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    health: HealthConfigData = field(default_factory=HealthConfigData)


@dataclass(slots=True)
class AppConfig:
    """Top-level bridge configuration."""

    server: ServerConfig
    targets: dict[str, dict[str, Any]]


def load_config(path: str | Path = "config/targets.toml") -> AppConfig:
    """Load bridge configuration from TOML.

    Args:
        path: TOML file path.

    Returns:
        Parsed application configuration.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If top-level sections have invalid types.
    """
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    server_raw = raw.get("server", {})
    targets_raw = raw.get("targets", {})
    if not isinstance(server_raw, dict):
        raise ValueError("[server] must be a TOML table")
    if not isinstance(targets_raw, dict):
        raise ValueError("[targets] must be a TOML table")

    timeouts_raw = server_raw.get("timeouts", {})
    if not isinstance(timeouts_raw, dict):
        raise ValueError("[server.timeouts] must be a TOML table")
    health_raw = server_raw.get("health", {})
    if not isinstance(health_raw, dict):
        raise ValueError("[server.health] must be a TOML table")

    timeouts = TimeoutConfig(
        quick=int(timeouts_raw.get("quick", 30)),
        deep=int(timeouts_raw.get("deep", 180)),
        blocker=int(timeouts_raw.get("blocker", 600)),
    )
    health = HealthConfigData(
        lazy_cache_seconds=float(health_raw.get("lazy_cache_seconds", 60.0)),
        polled_interval_seconds=float(
            health_raw.get("polled_interval_seconds", 15.0)
        ),
        check_timeout_seconds=float(health_raw.get("check_timeout_seconds", 10.0)),
    )
    server = ServerConfig(
        host=str(server_raw.get("host", "127.0.0.1")),
        port=int(server_raw.get("port", 7878)),
        transport=str(server_raw.get("transport", "stdio")),
        audit_log=str(server_raw.get("audit_log", "state/audit.jsonl")),
        audit_bodies_dir=str(
            server_raw.get("audit_bodies_dir", "state/audit-bodies")
        ),
        sessions_path=str(
            server_raw.get("sessions_path", "state/sessions.json")
        ),
        max_context_bytes=int(server_raw.get("max_context_bytes", 8_192)),
        max_attachments=int(server_raw.get("max_attachments", 4)),
        timeouts=timeouts,
        health=health,
    )

    targets: dict[str, dict[str, Any]] = {}
    for target_id, target_raw in targets_raw.items():
        if not isinstance(target_raw, dict):
            raise ValueError(f"[targets.{target_id}] must be a TOML table")
        targets[str(target_id)] = dict(target_raw)

    return AppConfig(server=server, targets=targets)
