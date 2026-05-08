"""CLI entry point for the Agent Bridge TUI."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from bridge.config import load_config
from tui.app import AgentBridgeInspector, TuiSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge tui",
        description="Read-only inspector for a running Agent Bridge instance.",
    )
    parser.add_argument("--bridge-url", metavar="URL", help="Override [tui].bridge_url")
    parser.add_argument("--config", default="config/targets.toml", help="Path to targets.toml")
    parser.add_argument("--no-mouse", action="store_true", help="Disable mouse support")
    return parser


def settings_from_args(args: argparse.Namespace) -> TuiSettings:
    config = load_config(Path(args.config))
    tui = config.tui
    return TuiSettings(
        bridge_url=args.bridge_url or tui.bridge_url,
        poll_targets_seconds=tui.poll_targets_seconds,
        poll_sessions_seconds=tui.poll_sessions_seconds,
        poll_audit_seconds=tui.poll_audit_seconds,
        audit_initial_limit=tui.audit_initial_limit,
        audit_bodies_dir=config.server.audit_bodies_dir,
        mouse=False if args.no_mouse else tui.mouse,
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = AgentBridgeInspector(settings_from_args(args))
    app.run()


async def amain(argv: list[str] | None = None) -> None:
    await asyncio.to_thread(main, argv)


if __name__ == "__main__":
    main()
