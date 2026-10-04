"""Steer delivery to worker turns and the drop at settlement, through ``orchestrate``.

Scenarios queue steers at durable, undispatched or acknowledged checkpoints.
Ambiguous provider dispatch is tested separately and never blindly replayed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import PLUGIN, DynamicState, WorkstreamPlan
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.lifecycle import IntentKind, IntentStage
from vibesys.orchestration.dynamic.models import AgentLoopState, DynamicWorkstream, SteerNote

# test-isolation: inject the failed commit through the dynamic worker port directly,
# before the planner/input gate can add unrelated commits.
from vibesys.orchestration.dynamic.orchestration import DurableStateCommitError, _DynamicRun
from vibesys.orchestration.dynamic.steers import SteerAccepted, enqueue
from vs_runtime.api import AgentCapability, RunFacts, RunStatus, RuntimeContractError
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
    implementation_result: dict[str, object] | None = None
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
                AgentCapability.DURABLE_TURN_CONTINUATION,
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
        if role.id == IMPLEMENTER.id:
            return (
                implementation(self.hypothesis)
                if self.implementation_result is None
                else self.implementation_result
            )
        return _PASS

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


def test_ambiguous_dispatch_keeps_notes_reserved_and_blocks_unsafe_replay(tmp_path: Path) -> None:
    scenario = _Scenario("cache", crash_at={(IMPLEMENTER.id, 1)})

    async def run() -> None:
        fake = scenario.open(tmp_path)
        fake.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        fake.state.script_commit_at("dynamic: cache implementing", OSError("before dispatch"))
        with pytest.raises(DurableStateCommitError):
            await scenario.orchestrate(max_retries=3, crashes=False)
        await scenario.steer(_NOTE, at_s=958.0)
        await scenario.orchestrate(max_retries=3, crashes=True)
        with pytest.raises(RuntimeContractError, match="requires reconciliation"):
            await scenario.orchestrate(max_retries=3, crashes=False)
        state = await fake.state.load(DynamicState)
        assert state is not None
        assert state.agent is not None
        [note] = state.agent.steers["cache"]
        assert note.delivered_to is None
        assert note.dropped is None
        assert note.reserved_to is not None
        assert state.lifecycle.intents[note.reserved_to].stage is IntentStage.BLOCKED
        assert (
            len(
                [
                    intent
                    for intent in state.lifecycle.intents.values()
                    if intent.kind is IntentKind.TURN
                ]
            )
            == 1
        )

    asyncio.run(run())
    [dispatched] = scenario.messages(IMPLEMENTER.id)
    assert _NOTE in dispatched
    assert scenario.messages(JUDGE.id) == []


def test_note_reaches_the_judge_in_a_commit_right_before_its_turn(tmp_path: Path) -> None:
    scenario = _Scenario("cache", crash_at=set())

    async def run() -> None:
        fake = scenario.open(tmp_path)
        fake.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        fake.state.script_commit_at("dynamic: cache reviewed", OSError("after acknowledged review"))
        with pytest.raises(DurableStateCommitError):
            await scenario.orchestrate(max_retries=1, crashes=False)
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
    assert judge_turn.last_commit.label == "dynamic: cache dynamic-judge dispatch authorized"
    [note] = _notes(judge_turn.last_commit, "cache")
    assert note.delivered_to is None
    assert note.reserved_to is not None
    assert isinstance(judge_turn.last_commit.value, DynamicState)
    assert (
        judge_turn.last_commit.value.lifecycle.intents[note.reserved_to].stage
        is IntentStage.DISPATCHED
    )


def test_note_pending_when_the_workstream_settles_is_dropped_and_journaled(
    tmp_path: Path,
) -> None:
    """A known terminal disposition settles after restart without another worker turn."""
    scenario = _Scenario(
        "crashing",
        crash_at=set(),
        implementation_result={"summary": "No viable mechanism.", "outcome": "disproven"},
    )

    async def run() -> None:
        fake = scenario.open(tmp_path)
        fake.state.script_commit_at(
            "dynamic: record hypothesis crashing", OSError("round checkpoint")
        )
        with pytest.raises(DurableStateCommitError):
            await scenario.orchestrate(max_retries=1, crashes=False)
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


def test_session_setup_failure_leaves_note_pending_for_dispatch(tmp_path: Path) -> None:
    scenario = _Scenario("cache", crash_at=set())

    async def run() -> None:
        fake = scenario.open(tmp_path)
        fake.evaluation.script_benchmark(INPUT_BASELINE, throughput(10.0))
        fake.state.script_commit_at("dynamic: cache implementing", OSError("before dispatch"))
        with pytest.raises(DurableStateCommitError):
            await scenario.orchestrate(max_retries=3, crashes=False)
        await scenario.steer(_NOTE, at_s=1.0)
        fake.agents.script_creation(RuntimeError("session unavailable"))
        await scenario.orchestrate(max_retries=3, crashes=False)
        failed = next(
            commit
            for commit in fake.state.commits
            if commit.label == "dynamic: cache attempt failed"
        )
        [note] = _notes(failed, "cache")
        assert note.delivered_to is None
        assert note.dropped is None
        assert note.reserved_to is not None
        assert isinstance(failed.value, DynamicState)
        assert failed.value.lifecycle.intents[note.reserved_to].stage is IntentStage.PREPARED
        state = await fake.state.load(DynamicState)
        assert state is not None
        assert state.agent is not None
        [delivered] = state.agent.steers["cache"]
        assert delivered.delivered_to == note.reserved_to
        assert delivered.dropped is None

    asyncio.run(run())
    assert _NOTE in scenario.messages(IMPLEMENTER.id)[-1]


def test_failed_dispatch_commit_never_invokes_the_provider(tmp_path: Path) -> None:
    """A failed authorization write reloads Prepared, preserving its notes."""
    plans = portfolio("cache")["workstreams"]
    assert isinstance(plans, list)
    plan = WorkstreamPlan.model_validate(plans[0])
    scenario = _Scenario("cache", crash_at=set())

    async def run() -> None:
        fake = scenario.open(tmp_path)
        initial = DynamicState(
            agent=AgentLoopState(),
            workstreams=[
                DynamicWorkstream(
                    hypothesis_id="cache",
                    sequence=1,
                    planning_call=1,
                    plan=plan,
                    parent_revision="fake-revision",
                )
            ],
        )
        enqueue(initial, "cache", _NOTE, at_s=1.0, interrupt=False)
        await fake.state.commit(initial)
        dynamic = await _DynamicRun.open(fake, dynamic_options(max_in_flight=1))
        fake.state.script_commit(None, OSError("dispatch durability failure"))
        try:
            with pytest.raises(DurableStateCommitError):
                await dynamic.workstreams.execute(plan)
        finally:
            await dynamic.input_gate.stop()
        assert scenario.turns == []
        durable = await fake.state.load(DynamicState)
        assert durable == dynamic.state
        assert durable is not None
        assert durable.agent is not None
        [note] = durable.agent.steers["cache"]
        assert note.delivered_to is None
        assert note.reserved_to is not None
        assert durable.lifecycle.intents[note.reserved_to].stage is IntentStage.PREPARED

    asyncio.run(run())


def test_unknown_provider_outcome_blocks_in_process_retry(tmp_path: Path) -> None:
    """A lost dispatch acknowledgment cannot be retried as ordinary work."""
    plans = portfolio("cache")["workstreams"]
    assert isinstance(plans, list)
    plan = WorkstreamPlan.model_validate(plans[0])
    script = Script({IMPLEMENTER.id: [RuntimeError("provider transport lost")]})

    async def run() -> None:
        fake = baseline_run(tmp_path, script)
        initial = DynamicState(
            agent=AgentLoopState(),
            workstreams=[
                DynamicWorkstream(
                    hypothesis_id="cache",
                    sequence=1,
                    planning_call=1,
                    plan=plan,
                    parent_revision="fake-revision",
                )
            ],
        )
        enqueue(initial, "cache", _NOTE, at_s=1.0, interrupt=False)
        await fake.state.commit(initial)
        dynamic = await _DynamicRun.open(
            fake, dynamic_options(max_in_flight=1, max_retries_per_round=3)
        )
        try:
            with pytest.raises(RuntimeContractError, match="requires reconciliation"):
                await dynamic.workstreams.execute(plan)
        finally:
            await dynamic.input_gate.stop()
        durable = await fake.state.load(DynamicState)
        assert durable is not None
        assert durable.agent is not None
        [note] = durable.agent.steers["cache"]
        assert note.delivered_to is None
        assert note.reserved_to is not None
        assert durable.lifecycle.intents[note.reserved_to].stage is IntentStage.BLOCKED
        assert len(script.calls) == 1
        assert durable.workstreams[0].budget.spent == 1
        assert durable.workstreams[0].candidate_revision is not None
        assert durable.search.rounds == []

    asyncio.run(run())
