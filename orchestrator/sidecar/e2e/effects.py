"""What a scripted agent does, as data, and the one function that does it.

A script is a generator: it yields an effect (run this command, call this orchestrator tool), is sent
the outcome, and returns the text the agent ends its turn with. ``perform`` is the only code that
runs a script, so a script is a plain function of the messages the agent was sent and of what its
effects returned, and a test runs it against a fake ``Machine`` with no process.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Shell:
    """Run a command in the agent's worktree, with the agent's environment."""

    argv: tuple[str, ...]


@dataclass(frozen=True)
class Mcp:
    """Call a tool of the orchestrator's MCP server, as the agent does through its CLI."""

    tool: str
    arguments: Mapping[str, Any]


Effect = Shell | Mcp


@dataclass(frozen=True)
class Outcome:
    """How an effect ended: whether it succeeded, and what it printed or answered."""

    ok: bool
    text: str


@dataclass(frozen=True)
class Step:
    """An effect and its outcome."""

    effect: Effect
    outcome: Outcome


@dataclass(frozen=True)
class Performed:
    """One turn's worth of work: every step in order, and the text the turn ended with."""

    steps: tuple[Step, ...]
    text: str


Script = Generator[Effect, Outcome, str]


class Machine(Protocol):
    """Where effects happen: a host in production, a recording fake in unit tests."""

    def run(self, effect: Effect) -> Outcome:
        """Carry out *effect* and say how it ended. Never raises for a failing effect."""
        ...


class EffectFailedError(Exception):
    """An effect failed, so the scripted turn cannot be what the test says it is."""


class UnscriptedError(Exception):
    """The agent was sent a message its script has no answer for: a lost or repeated wake-up."""


def perform(script: Script, machine: Machine) -> Performed:
    """Run *script* on *machine*; a failing effect raises ``EffectFailedError`` naming it."""
    steps: list[Step] = []
    try:
        effect = next(script)
        while True:
            outcome = machine.run(effect)
            if not outcome.ok:
                msg = f"{effect} failed: {outcome.text}"
                raise EffectFailedError(msg)
            steps.append(Step(effect, outcome))
            effect = script.send(outcome)
    except StopIteration as done:
        return Performed(tuple(steps), done.value or "")
