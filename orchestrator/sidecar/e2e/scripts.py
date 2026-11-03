"""What each role does on the toy task, in the calls a real agent following the ``optimize`` skill makes.

The lead measures the base, starts a Claude worker and a Codex worker, then a judge for the better
commit, promotes it and reports. A worker commits a score, checks and measures it with
``vibesys-cli`` and hands off. A judge compares the commit with the base. These are scripts of the
observable calls, not of the skill's reasoning: this proves the wiring, not the quality of a plan.

Each script is a function of who the agent is and the messages it was sent so far (the last is the
one it answers). A message count a script does not know raises ``UnscriptedError``, so a handoff
that wakes the lead twice, or not at all, fails the run instead of being papered over.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from e2e.effects import Mcp, Outcome, Shell, UnscriptedError

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from e2e.effects import Effect, Script

TASK = "toy"
CLI = "vibesys-cli"
SCORES = {"claude_code": 14, "codex": 17}
_COMMIT = r"[0-9a-f]{40}"


@dataclass(frozen=True)
class Me:
    """Who the agent is: the identity the orchestrator gave its session."""

    run_id: str
    agent_id: str
    harness: str
    """The harness name the orchestrator's tools use (``claude_code`` or ``codex``)."""


def cli(me: Me, command: Sequence[str], *flags: str) -> Shell:
    """A ``vibesys-cli`` call as the agent makes it: explicit flags for the task, run, agent and harness."""
    return Shell(
        (
            CLI,
            *command,
            *flags,
            "--task",
            TASK,
            "--run",
            me.run_id,
            "--agent",
            me.agent_id,
            "--harness",
            me.harness.replace("_", "-"),
        )
    )


def git(*args: str) -> Shell:
    """A git call that commits as a fixed author, with no signing."""
    return Shell(
        (
            "git",
            "-c",
            "user.name=agent",
            "-c",
            "user.email=agent@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        )
    )


def _head() -> Generator[Effect, Outcome, str]:
    return (yield Shell(("git", "rev-parse", "HEAD"))).text.strip()


@dataclass(frozen=True)
class HandedOff:
    """A worker's handoff as the lead reads it in the message the orchestrator sent."""

    commit: str
    score: int


def read_handoff(message: str) -> HandedOff:
    """The commit and score a handoff message names."""
    commit = re.search(rf"commit ({_COMMIT}) on branch", message)
    score = re.search(r"Claim: score (\d+)", message)
    if commit is None or score is None:
        msg = f"not a worker handoff: {message!r}"
        raise UnscriptedError(msg)
    return HandedOff(commit.group(1), int(score.group(1)))


def _best(messages: Sequence[str]) -> HandedOff:
    """The better of the two worker handoffs, which are the lead's second and third messages."""
    return max((read_handoff(m) for m in messages[1:3]), key=lambda handoff: handoff.score)


def _start(me: Me, _messages: Sequence[str]) -> Script:
    base = yield from _head()
    yield cli(me, ["submit", "check", base], "--wait")
    yield cli(me, ["submit", "measure", base], "--samples", "3", "--wait")
    for plan, (harness, score) in enumerate(SCORES.items(), start=1):
        brief = (
            f"Plan {plan}: set score.txt to {score}, check it and measure it. Base commit {base}."
        )
        yield Mcp(
            "spawn_agent",
            {"role": "worker", "harness": harness, "plan_number": plan, "brief": brief},
        )
    yield Mcp("list_agents", {})
    return "Started a Claude worker and a Codex worker."


def _first_handoff(_me: Me, _messages: Sequence[str]) -> Script:
    return "Noted the first handoff."
    yield  # a script is a generator even when it does nothing


def _second_handoff(_me: Me, messages: Sequence[str]) -> Script:
    best = _best(messages)
    base = yield from _head()
    yield Mcp(
        "spawn_agent",
        {
            "role": "judge",
            "harness": "claude_code",
            "brief": f"Review commit {best.commit} against {base}.",
        },
    )
    return "Started a judge."


def _judged(me: Me, messages: Sequence[str]) -> Script:
    best = _best(messages)
    yield cli(me, ["promote", best.commit])
    yield cli(me, ["submit", "check", best.commit], "--no-cache", "--wait")
    yield cli(me, ["submit", "measure", best.commit], "--samples", "3", "--no-cache", "--wait")
    result = yield cli(me, ["result"], "--json")
    value = json.loads(result.text)["current"]["result"]["measure"]["value"]
    yield Mcp(
        "post_report",
        {"headline": f"best score {value} at {best.commit}", "changed": f"promoted {best.commit}"},
    )
    return "Reported."


#: What the lead does for its n-th message: the brief, the two worker handoffs, the judge's.
LEAD_STEPS = {1: _start, 2: _first_handoff, 3: _second_handoff, 4: _judged}


def lead(me: Me, messages: Sequence[str]) -> Script:
    """The lead: base, two workers, a judge, promote, report."""
    step = LEAD_STEPS.get(len(messages))
    if step is None:
        msg = f"the lead was sent message {len(messages)}, which its script does not expect"
        raise UnscriptedError(msg)
    return (yield from step(me, messages))


def _only_one(messages: Sequence[str], role: str) -> None:
    """A worker and a judge are sent their brief and nothing else."""
    if len(messages) != 1:
        msg = f"a {role} was sent message {len(messages)}, which its script does not expect"
        raise UnscriptedError(msg)


def worker(me: Me, messages: Sequence[str]) -> Script:
    """A worker: commit a score, check, measure, hand off."""
    _only_one(messages, "worker")
    found = re.search(rf"Plan (\d+): set score.txt to (\d+).* Base commit ({_COMMIT})", messages[0])
    if found is None:
        msg = f"not a worker brief: {messages[0]!r}"
        raise UnscriptedError(msg)
    plan, score, base = int(found.group(1)), found.group(2), found.group(3)
    yield Shell(("sh", "-c", f"echo {score} > score.txt"))
    yield git("commit", "-q", "-a", "-m", f"set score {score}")
    commit = yield from _head()
    yield cli(me, ["submit", "check", commit], "--wait")
    yield cli(me, ["submit", "measure", commit], "--samples", "3", "--wait")
    shown = yield cli(me, ["show", commit], "--json")
    yield Mcp(
        "post_handoff",
        {
            "plan_number": plan,
            "has_candidate": True,
            "claim": f"score {score}",
            "commit": commit,
            "parent_commit": base,
            "branch": "HEAD",
            "checks": "passed",
            "measurement": shown.text.strip(),
        },
    )
    return "Handed off."


def judge(me: Me, messages: Sequence[str]) -> Script:
    """A judge: compare the commit with the base and say so."""
    _only_one(messages, "judge")
    found = re.search(rf"commit ({_COMMIT}) against ({_COMMIT})", messages[0])
    if found is None:
        msg = f"not a judge brief: {messages[0]!r}"
        raise UnscriptedError(msg)
    verdict = yield cli(me, ["compare", found.group(1)], "--against", found.group(2))
    passed = verdict.text.startswith("better")
    yield Mcp(
        "post_handoff",
        {
            "has_candidate": False,
            "claim": "pass" if passed else "fail",
            "learned": verdict.text.strip(),
        },
    )
    return "Judged."


ROLES = {"lead": lead, "worker": worker, "judge": judge}
