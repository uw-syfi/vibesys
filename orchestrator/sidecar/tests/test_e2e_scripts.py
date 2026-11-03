"""The scripted agents of the end-to-end test do what their role says, and fail loudly off script.

The scripts run against a fake ``Machine`` that answers like the real commands, so no process
starts. The acting peers are driven through the real ``AgentshimDriver`` on both harnesses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agentshim import Agent, ApprovalPolicy, NativePermissions, TransportKind
from agentshim.testing import FakeClock, FakeExecutor, SequentialIds
from e2e.actor import Actor
from e2e.effects import EffectFailedError, Mcp, Outcome, Shell, UnscriptedError, perform
from e2e.peers import ActingClaudePeers, ActingCodex
from e2e.scripts import ROLES, Me
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentshim import AgentEvent
    from e2e.effects import Effect

BASE = "b" * 40
ME = Me(run_id="run-1", agent_id="agent-1", harness="claude_code")
COMMITS = st.text("0123456789abcdef", min_size=40, max_size=40)


@dataclass
class FakeMachine:
    """Answers the commands the scripts run as the real ones do, and remembers what it ran."""

    head: str = BASE
    effects: list[Effect] = field(default_factory=list)
    failing: str | None = None

    def run(self, effect: Effect) -> Outcome:
        self.effects.append(effect)
        if isinstance(effect, Shell):
            return self._shell(effect.argv)
        return Outcome(ok=effect.tool != self.failing, text=f"{effect.tool} ok")

    def _shell(self, argv: Sequence[str]) -> Outcome:
        if argv[0] == "git" and "rev-parse" in argv:
            return Outcome(ok=True, text=self.head + "\n")
        if argv[0] == "vibesys-cli" and argv[1] == "result":
            return Outcome(
                ok=True,
                text=json.dumps(
                    {"current": {"head": "x", "result": {"measure": {"value": 17.01}}}}
                ),
            )
        if argv[0] == "vibesys-cli" and argv[1] == "compare":
            return Outcome(ok=True, text="better\n")
        return Outcome(ok=True, text="ok")


def handoff(commit: str, score: int) -> str:
    return (
        f"Agent w handed off plan 1.\nCandidate: commit {commit} on branch HEAD (parent {BASE}).\n"
        f"Claim: score {score}\nChecks: passed\nMeasurement: x\nLearned: \nNext: "
    )


def tools(machine: FakeMachine) -> list[str]:
    return [e.tool for e in machine.effects if isinstance(e, Mcp)]


def lead_messages(a: str, score_a: int, b: str, score_b: int) -> list[str]:
    return ["brief", handoff(a, score_a), handoff(b, score_b), "judge handoff"]


@given(a=COMMITS, b=COMMITS, score_a=st.integers(1, 99), score_b=st.integers(1, 99))
def test_the_lead_sends_the_judge_to_the_better_commit_whatever_the_order_of_the_handoffs(
    a: str, b: str, score_a: int, score_b: int
) -> None:
    machine = FakeMachine()
    messages = lead_messages(a, score_a, b, score_b)[:3]
    perform(ROLES["lead"](ME, messages), machine)
    brief = next(e.arguments["brief"] for e in machine.effects if isinstance(e, Mcp))
    assert brief == f"Review commit {a if score_a >= score_b else b} against {BASE}."


@given(a=COMMITS, b=COMMITS, score_a=st.integers(1, 99), score_b=st.integers(1, 99))
def test_the_lead_promotes_the_better_commit_and_reports_the_measured_number(
    a: str, b: str, score_a: int, score_b: int
) -> None:
    machine = FakeMachine()
    perform(ROLES["lead"](ME, lead_messages(a, score_a, b, score_b)), machine)
    best = a if score_a >= score_b else b
    promote = next(e for e in machine.effects if isinstance(e, Shell) and "promote" in e.argv)
    assert best in promote.argv
    report = [e for e in machine.effects if isinstance(e, Mcp)][-1]
    assert report.tool == "post_report"
    assert report.arguments["headline"] == f"best score 17.01 at {best}"


def test_the_lead_starts_a_claude_worker_and_a_codex_worker_after_measuring_the_base() -> None:
    machine = FakeMachine()
    perform(ROLES["lead"](ME, ["brief"]), machine)
    spawns = [
        e.arguments for e in machine.effects if isinstance(e, Mcp) and e.tool == "spawn_agent"
    ]
    assert [(s["role"], s["harness"]) for s in spawns] == [
        ("worker", "claude_code"),
        ("worker", "codex"),
    ]
    measure = next(
        i for i, e in enumerate(machine.effects) if isinstance(e, Shell) and "measure" in e.argv
    )
    first_spawn = next(i for i, e in enumerate(machine.effects) if isinstance(e, Mcp))
    assert measure < first_spawn
    assert tools(machine)[-1] == "list_agents"


@given(count=st.integers(5, 40))
def test_a_lead_sent_more_messages_than_its_script_has_fails_instead_of_acting(count: int) -> None:
    machine = FakeMachine()
    messages = lead_messages("a" * 40, 14, "c" * 40, 17) + ["one more"] * (count - 4)
    with pytest.raises(UnscriptedError, match="message"):
        perform(ROLES["lead"](ME, messages), machine)
    assert machine.effects == []


@pytest.mark.parametrize("role", ["worker", "judge"])
@given(count=st.integers(2, 20))
def test_a_worker_or_judge_sent_a_second_message_fails_instead_of_acting(
    role: str, count: int
) -> None:
    machine = FakeMachine()
    with pytest.raises(UnscriptedError):
        perform(ROLES[role](ME, ["brief"] * count), machine)
    assert machine.effects == []


@given(score=st.integers(1, 99), plan=st.integers(1, 9))
def test_a_worker_commits_checks_measures_then_hands_off_its_own_commit(
    score: int, plan: int
) -> None:
    machine = FakeMachine(head="c" * 40)
    brief = f"Plan {plan}: set score.txt to {score}, check it and measure it. Base commit {BASE}."
    perform(ROLES["worker"](ME, [brief]), machine)
    verbs = [
        e.argv[1] for e in machine.effects if isinstance(e, Shell) and e.argv[0] == "vibesys-cli"
    ]
    assert verbs == ["submit", "submit", "show"]
    last = machine.effects[-1]
    assert isinstance(last, Mcp)
    assert last.tool == "post_handoff"
    assert last.arguments["commit"] == "c" * 40
    assert last.arguments["claim"] == f"score {score}"
    assert last.arguments["plan_number"] == plan


def test_a_failing_effect_fails_the_turn_naming_the_effect() -> None:
    machine = FakeMachine(failing="spawn_agent")
    with pytest.raises(EffectFailedError, match="spawn_agent"):
        perform(ROLES["lead"](ME, ["brief"]), machine)


# -- the acting peers, behind the real driver


def _driver(provider: str, actor: Actor, turns: list[None]) -> AgentshimDriver:
    if provider == "claude":
        peers = ActingClaudePeers(actor, lambda: turns.append(None)).build
        harness = orch.HARNESS_CLAUDE_CODE
    else:
        peers = ActingCodex(actor, lambda: turns.append(None)).peer
        harness = orch.HARNESS_CODEX
    agent = Agent(
        provider,
        transport=TransportKind.STREAM,
        model="fake-model",
        executor=FakeExecutor([], peers=peers),
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/tester"},
        permissions=NativePermissions.bypass(),
        approvals=ApprovalPolicy.DENY,
        clock=FakeClock(),
        ids=SequentialIds(),
    )
    launch = h.launch(harness)
    return AgentshimDriver(
        lambda conversation_id: build_session(
            launch, agent=lambda _name, _identity: agent, new_conversation_id=conversation_id
        ),
        preferred_id="11111111-1111-4111-8111-111111111111",
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_each_message_runs_the_next_step_and_the_turn_shows_the_calls_the_agent_made(
    provider: str, tmp_path: Path
) -> None:
    machine = FakeMachine()
    actor = Actor(ME, "lead", machine, tmp_path)
    turns: list[None] = []
    driver = _driver(provider, actor, turns)
    driver.open()
    seen: list[AgentEvent] = []
    driver.begin_turn("t1", "Run the vibesys:optimize skill")
    result = driver.run_turn(seen.append)
    assert result.status == orch.TURN_STATUS_COMPLETED
    assert tools(machine) == ["spawn_agent", "spawn_agent", "list_agents"]
    assert (tmp_path / "prompts" / "1.md").read_text() == "Run the vibesys:optimize skill"
    shown = " ".join(repr(event) for event in seen)
    assert "spawn_agent" in shown
    assert "list_agents" in shown


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_message_the_script_does_not_expect_fails_the_turn_and_leaves_a_record(
    provider: str, tmp_path: Path
) -> None:
    machine = FakeMachine()
    actor = Actor(ME, "worker", machine, tmp_path)
    driver = _driver(provider, actor, [])
    driver.open()
    driver.begin_turn("t1", "not a brief")
    with pytest.raises(UnscriptedError):
        driver.run_turn(lambda _event: None)
    assert "UnscriptedError" in (tmp_path / "peer-failure.txt").read_text()
    assert machine.effects == []
