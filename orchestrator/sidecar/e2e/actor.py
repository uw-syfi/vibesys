"""An agent's scripted behavior, between the provider's frames and the machine's effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from e2e.effects import UnscriptedError, perform
from e2e.scripts import ROLES, is_job_finished, lead

if TYPE_CHECKING:
    from pathlib import Path

    from e2e.effects import Machine, Performed, Script
    from e2e.scripts import Me


@dataclass
class Actor:
    """Answers each message the agent is sent by running its role's script, and keeps a record.

    ``record`` receives ``prompts/N.md`` (message N, as sent) and, if a turn fails,
    ``peer-failure.txt`` naming why, so the test that started the run can read both. The failure is
    also raised, which fails the turn in the sidecar.
    """

    me: Me
    role: str
    machine: Machine
    record: Path
    messages: list[str] = field(default_factory=list)
    received: int = 0

    def _script(self) -> Script:
        script = ROLES.get(self.role)
        if script is None:
            msg = f"no script for role {self.role!r}"
            raise UnscriptedError(msg)
        return script(self.me, tuple(self.messages))

    def act(self, message: str) -> Performed:
        """Run the script for *message*, the next the agent was sent."""
        self.received += 1
        prompts = self.record / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / f"{self.received}.md").write_text(message)
        if self.role == "lead" and is_job_finished(message):
            return perform(lead(self.me, tuple(self.messages), notification=True), self.machine)
        self.messages.append(message)
        try:
            return perform(self._script(), self.machine)
        except Exception as error:
            (self.record / "peer-failure.txt").write_text(
                f"message {self.received}: {type(error).__name__}: {error}\n"
            )
            raise
