"""Steer delivery to worker turns and the drop at settlement, through ``orchestrate``.

Agent mode is not wired to a driver yet, so each scenario stops the run at a
chosen worker turn (as a crash would), adds a steer to the durable state as
the orchestrator agent would, and resumes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import PLUGIN, DynamicState
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import AgentLoopState, SteerNote
from vibesys.orchestration.dynamic.steers import SteerAccepted, enqueue
from vs_runtime.api import AgentCapability, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole
    from vs_runtime.api.testing import FakeStateCommit

_NOTE = "Profile decode before tuning the batch size."
_PASS = {"passed": True, "analysis": "Candidate is correct."}


@dataclass
class _Turn:
    role: str
    message: str
    # The last durable commit when the turn started.
    last_commit: FakeStateCommit | None


@dataclass
class _Scenario:
    """Runs the plugin; a turn listed in ``crash_at`` (role, call number) ends the run."""

    hypothesis: str
    crash_at: set[tuple[str, int]]
    turns: list[_Turn] = field(default_factory=list)
    run: FakeRun | None = None
    _running: asyncio.Future[RunStatus] | None = None

    def open(self, tmp_path: Path) -> FakeRun:
        self.run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", benchmark_configured=True),
            responder=self.respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        return self.run

    def respond(
        self,
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            return portfolio(self.hypothesis)
        assert self.run is not None
        commits = self.run.state.commits
        self.turns.append(_Turn(role.id, message, commits[-1] if commits else None))
        if (role.id, sum(turn.role == role.id for turn in self.turns)) in self.crash_at:
            assert self._running is not None
            self._running.cancel()
        return implementation(self.hypothesis) if role.id == IMPLEMENTER.id else _PASS

    async def orchestrate(self, *, max_retries: int, crashes: bool) -> None:
        assert self.run is not None
        options = dynamic_options(max_in_flight=1, max_retries_per_round=max_retries)
        self._running = asyncio.ensure_future(PLUGIN.orchestrate(self.run, options))
        if crashes:
            with pytest.raises(asyncio.CancelledError):
                await self._running
        else:
            assert await self._running is RunStatus.SUCCEEDED

    async def steer(self, text: str, *, at_s: float) -> SteerNote:
        """Add a steer to the durable state, switching it to agent mode."""
        assert self.run is not None
        state = await self.run.state.load(DynamicState)
        assert state is not None
        state.agent = state.agent or AgentLoopState()
        accepted = enqueue(state, self.hypothesis, text, at_s=at_s, interrupt=False)
        assert isinstance(accepted, SteerAccepted)
        await self.run.state.commit(state)
        return accepted.note

    def messages(self, role: str) -> list[str]:
        return [turn.message for turn in self.turns if turn.role == role]


def _notes(commit: FakeStateCommit | None, hypothesis: str) -> list[SteerNote]:
    assert commit is not None
    assert isinstance(commit.value, DynamicState)
    assert commit.value.agent is not None
    return commit.value.agent.steers[hypothesis]


def test_note_reaches_the_next_implementer_turn_and_survives_a_crash(tmp_path: Path) -> None:
    """The turn-start commit delivers the note; a crash after it renders the note again."""
    scenario = _Scenario("cache", crash_at={(IMPLEMENTER.id, 1), (IMPLEMENTER.id, 2)})

    async def run() -> None:
        fake = scenario.open(tmp_path)
        fake.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        await scenario.orchestrate(max_retries=3, crashes=True)
        await scenario.steer(_NOTE, at_s=958.0)
        await scenario.orchestrate(max_retries=3, crashes=True)
        await scenario.orchestrate(max_retries=3, crashes=False)

    asyncio.run(run())

    first, delivered, redone = scenario.messages(IMPLEMENTER.id)
    assert _NOTE not in first
    assert "15:58 into the run" in delivered
    assert _NOTE in delivered
    assert _NOTE in redone
    # The commit that recorded each turn's start is the one that delivered the note.
    for turn in [item for item in scenario.turns if item.role == IMPLEMENTER.id][1:]:
        assert turn.last_commit is not None
        assert turn.last_commit.label == "dynamic: cache implementing"
        [note] = _notes(turn.last_commit, "cache")
        assert note.delivered_to == "cache/dynamic-implementer/attempt-1"
    # Once delivered, the note is not rendered to the judge.
    [review] = scenario.messages(JUDGE.id)
    assert _NOTE not in review


def test_note_reaches_the_judge_in_a_commit_right_before_its_turn(tmp_path: Path) -> None:
    scenario = _Scenario("cache", crash_at={(JUDGE.id, 1)})

    async def run() -> None:
        fake = scenario.open(tmp_path)
        fake.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        await scenario.orchestrate(max_retries=1, crashes=True)
        await scenario.steer(_NOTE, at_s=61.0)
        await scenario.orchestrate(max_retries=1, crashes=False)

    asyncio.run(run())

    [_implementer] = scenario.messages(IMPLEMENTER.id)
    first, resumed = scenario.messages(JUDGE.id)
    assert _NOTE not in first
    assert _NOTE in resumed
    assert "1:01 into the run" in resumed
    judge_turn = [turn for turn in scenario.turns if turn.role == JUDGE.id][1]
    assert judge_turn.last_commit is not None
    assert judge_turn.last_commit.label == "dynamic: cache review notes delivered"
    [note] = _notes(judge_turn.last_commit, "cache")
    assert note.delivered_to == "cache/dynamic-judge/attempt-1"


def test_note_pending_when_the_workstream_settles_is_dropped_and_journaled(
    tmp_path: Path,
) -> None:
    """Two crashes spend the attempt; the next run settles it with no worker turn."""
    scenario = _Scenario("crashing", crash_at={(IMPLEMENTER.id, 1), (IMPLEMENTER.id, 2)})

    async def run() -> None:
        scenario.open(tmp_path)
        await scenario.orchestrate(max_retries=1, crashes=True)
        await scenario.orchestrate(max_retries=1, crashes=True)
        await scenario.steer(_NOTE, at_s=30.0)
        await scenario.orchestrate(max_retries=1, crashes=False)

    asyncio.run(run())

    assert all(_NOTE not in message for message in scenario.messages(IMPLEMENTER.id))
    assert scenario.run is not None
    settled = next(
        commit
        for commit in scenario.run.state.commits
        if commit.label == "dynamic: record hypothesis crashing"
    )
    [note] = _notes(settled, "crashing")
    assert (note.delivered_to, note.dropped) == (None, "workstream_settled")
    assert isinstance(settled.value, DynamicState)
    assert settled.value.agent is not None
    [entry] = settled.value.agent.journal
    assert (entry.kind, entry.subject) == ("steer", "crashing")
    assert note.note_sha256[:12] in entry.text
