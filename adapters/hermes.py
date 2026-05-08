"""Hermes CLI subprocess adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from adapters.base import Adapter, bound_response, estimate_tokens, format_brief
from bridge.protocol import (
    AdapterKind,
    Brief,
    Capability,
    ConsultChunk,
    Session,
    TargetStatus,
    Urgency,
)


class HermesAdapter(Adapter):
    """Run Hermes through its CLI."""

    kind = AdapterKind.CLI_SUBPROCESS

    def __init__(
        self,
        target_id: str,
        command: Sequence[str],
        cwd: str | None = None,
        model: str = "hermes",
        strong_model: str | None = None,
    ) -> None:
        self.id = target_id
        self.command = list(command)
        self.cwd = cwd
        self.model = model
        self.strong_model = strong_model
        caps = {Capability.SESSIONS_REPLAY}
        if strong_model:
            caps.add(Capability.STRONG_MODEL)
        self.capabilities = frozenset(caps)

    async def health(self) -> TargetStatus:
        """Return ready when the configured working directory is usable."""
        if self.cwd and not Path(self.cwd).exists():
            return TargetStatus.UNREACHABLE
        return TargetStatus.READY

    async def consult(
        self,
        brief: Brief,
        urgency: Urgency,
        session: Session | None,
        timeout_s: int,
        max_response_bytes: int,
    ) -> AsyncIterator[ConsultChunk]:
        """Send one formatted prompt to Hermes via -q flag."""
        prompt = self._prompt_with_replay(brief, session)
        command = self._command_for(urgency)
        # Hermes uses -q for non-interactive query mode
        command.extend(["-q", prompt])
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except TimeoutError:
            if "proc" in locals():
                proc.kill()
                await proc.wait()
            yield ConsultChunk(
                type="error",
                error_message="Hermes consult timed out",
            )
            return
        except OSError as exc:
            yield ConsultChunk(type="error", error_message=str(exc))
            return

        if proc.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            yield ConsultChunk(
                type="error",
                error_message=message or f"Hermes exited {proc.returncode}",
            )
            return

        response = stdout.decode("utf-8", errors="replace")
        response, truncated = bound_response(response, max_response_bytes)
        yield ConsultChunk(type="text", text=response)
        yield ConsultChunk(
            type="done",
            tokens_in=estimate_tokens(prompt),
            tokens_out=estimate_tokens(response),
            truncated=truncated,
        )

    async def open_session(self, purpose: str) -> Session:
        """Return a bridge replay session placeholder."""
        return Session(target=self.id, purpose=purpose)

    async def close_session(self, session: Session) -> None:
        """No target-side resource exists for replay sessions."""

    def _command_for(self, urgency: Urgency) -> list[str]:
        command = list(self.command)
        if urgency == Urgency.BLOCKER and self.strong_model:
            command.extend(["--model", self.strong_model])
        return command

    def _prompt_with_replay(
        self,
        brief: Brief,
        session: Session | None,
    ) -> str:
        current = format_brief(brief)
        if session is None or not session.history:
            return current
        lines = ["Prior session turns:"]
        for turn in session.history:
            lines.append(f"{turn.role}: {turn.content}")
        lines.extend(["", "Current brief:", current])
        return "\n".join(lines)
