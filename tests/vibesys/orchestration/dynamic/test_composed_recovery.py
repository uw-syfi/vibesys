"""Generated lifecycle traces compose live sessions, durable scopes and profile jobs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._fault_boundaries import (
    Boundary,
    FaultBoundary,
    FaultSessions,
    FaultState,
    Side,
)
from tests.vibesys.orchestration.dynamic._profile_release_support import (
    ProfileReleaseEffects,
    profile_release_effects,
)
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
)
from tests.vibesys.orchestration.dynamic._turn_support import HeldTurns

from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.control import SearchEnd, Withdrawal
from vibesys.orchestration.dynamic.lifecycle import IntentStage
from vibesys.orchestration.dynamic.models import AgentLoopState, DynamicState, WorkstreamPhase

# test-isolation: compose the existing Workers port with external evaluator effects;
# the production entrypoint cannot inject a mid-turn action source yet.
from vibesys.orchestration.dynamic.orchestration import _DynamicRun
from vibesys.orchestration.dynamic.workstream import InterruptResult
from vs_evaluation.api import EvaluationAgentRole, EvidenceKind, SubmitCall
from vs_runtime.api import AgentRole, RuntimeContractError

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.agent_loop import AgentLoop
    from vibesys.orchestration.dynamic.models import PlannedWorkstream


class Action(StrEnum):
    STEER = "steer"
    INTERRUPT = "interrupt"
    PARK = "park"
    CANCEL = "cancel"
    STOP = "stop"
    CRASH = "crash"


class TraceStoppedError(RuntimeError):
    """An injected stop input, propagated after started work drains."""


@dataclass
class LiveTrace:
    """Own a live search and its effect barriers throughout generated actions."""

    dynamic: _DynamicRun
    loop: AgentLoop[PlannedWorkstream]
    held: HeldTurns
    effects: ProfileReleaseEffects
    task: asyncio.Task[SearchEnd]
    accepted_notes: list[str] = field(default_factory=list)

    async def steer(self, number: int) -> None:
        text = f"Note {number}"
        result = steers.enqueue(self.dynamic.state, "a", text, at_s=float(number), interrupt=False)
        if isinstance(result, steers.SteerAccepted):
            self.accepted_notes.append(text)
        else:
            assert result.code is steers.SteerRefusal.RATE_LIMITED
        await self.effects.base.state.commit(self.dynamic.state)

    async def apply(self, actions: list[Action]) -> None:
        """Dispatch actions only at explicit turn and resource boundaries."""
        for number, action in enumerate(actions):
            match action:
                case Action.STEER:
                    await self.steer(number)
                case Action.INTERRUPT:
                    assert (
                        await self.dynamic.workstreams.interrupt("a") is InterruptResult.INTERRUPTED
                    )
                    await _opened_turn_or_ended_search(self.held, self.task)
                case Action.PARK | Action.CANCEL:
                    await self.loop.withdraw("a", Withdrawal(action.value))
                    await self.task
                    return
                case Action.STOP:
                    await self.effects.service.cancel_outstanding()
                    await self.loop.stop(TraceStoppedError("operator stop"))
                    self.held.release.set()
                    with pytest.raises(TraceStoppedError, match="operator stop"):
                        await self.task
                    return
                case Action.CRASH:
                    self.task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await self.task
                    recovered = await _DynamicRun.open(self.effects.restart(), self.dynamic.options)
                    try:
                        with pytest.raises(RuntimeContractError, match="requires reconciliation"):
                            await recovered.search_loop().run(recovered.recoverable())
                    finally:
                        await recovered.input_gate.stop()
                    return
        self.held.release.set()
        await self.task


def assert_state(state: DynamicState, script: Script) -> None:
    assert state.agent is not None
    assert state.workstreams[0].budget.refunded <= 12
    assert len(state.search.rounds) <= 1
    for note in state.agent.steers.get("a", []):
        assert note.delivered_to is None or note.dropped is None
        rendered = [
            message
            for role, _, message in script.calls
            if role in {IMPLEMENTER.id, JUDGE.id} and note.text in message
        ]
        assert len(rendered) <= 1
        if note.delivered_to is not None:
            assert note.delivered_to == note.reserved_to
            assert len(rendered) == 1
    active = [
        intent
        for intent in state.lifecycle.intents.values()
        if intent.stage is not IntentStage.COMPLETED
    ]
    assert all(intent.stage is IntentStage.BLOCKED for intent in active), active
    if state.workstreams[0].phase is WorkstreamPhase.CANCELLED:
        [record] = state.search.rounds
        assert record.candidate_disposition == "discard"
        assert record.candidate_retained is False


def assert_notes_preserved(state: DynamicState, expected: list[str]) -> None:
    """Every accepted input remains delivered, dropped, or reserved for recovery."""
    assert state.agent is not None
    assert [note.text for note in state.agent.steers.get("a", [])] == expected


async def run_trace(
    actions: list[Action], barrier: Boundary | None = None, side: Side = Side.BEFORE
) -> None:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a")],
            IMPLEMENTER.id: [implementation("a") for _ in range(len(actions) + 3)],
            JUDGE.id: [{"passed": True, "analysis": "Correct."}],
        }
    )
    held = HeldTurns(script)
    # These paths are resource identities only. Every participating port is in-memory.
    base = baseline_run(Path("/memory/composed-trace"), script, responder=held.respond)
    base.workspaces.set_default_patch("diff --git a/engine.py b/engine.py")
    fault = FaultBoundary(barrier, side) if barrier is not None else None
    effects = profile_release_effects(Path("/memory/composed-trace"), base, fault=fault)
    if fault is not None:
        effects.run = replace(
            effects.run,
            state=FaultState(base.state, fault),
            agents=FaultSessions(base.agents, fault),
        )
    dynamic = await _DynamicRun.open(
        effects.run, dynamic_options(max_in_flight=1, max_retries_per_round=12)
    )
    dynamic.state.agent = AgentLoopState()
    loop = dynamic.search_loop()
    task = asyncio.create_task(loop.run(dynamic.recoverable()))
    await _opened_turn_or_ended_search(held, task)
    candidate = base.workspaces.candidates[-1]
    scope_id = candidate.id
    assert scope_id is not None
    try:
        await effects.own_resources(candidate, "a")
        live = LiveTrace(dynamic, loop, held, effects, task)
        if fault is None:
            await live.apply(actions)
        else:
            await run_fault_trace(live, actions, fault)
        await effects.evaluation.release_jobs("a")
        await effects.assert_released(scope_id)
        if fault is None:
            assert len(effects.executor.cancellations) == len(set(effects.executor.cancellations))
        state = await base.state.load(DynamicState)
        assert state is not None
        assert_state(state, script)
        assert_notes_preserved(state, live.accepted_notes)
        completed: set[str] = set()
        prior_completed: set[str] = set()
        for commit in base.state.commits:
            assert isinstance(commit.value, DynamicState)
            current_completed = {
                intent.operation_id
                for intent in commit.value.lifecycle.intents.values()
                if intent.stage is IntentStage.COMPLETED
            }
            new_completions = current_completed - prior_completed
            assert not completed.intersection(new_completions)
            completed.update(new_completions)
            prior_completed = current_completed
        if state.workstreams[0].phase is WorkstreamPhase.CANCELLED:
            assert dynamic.rounds.winner() is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dynamic.input_gate.stop()
        await effects.close()


async def wait_boundary(
    fault: FaultBoundary, terminal: asyncio.Task[object], opened: asyncio.Task[int] | None = None
) -> None:
    """A missing boundary fails when its operation ends or a provider turn dispatches."""
    reached = asyncio.create_task(fault.reached.wait())
    alternatives: set[asyncio.Task[object]] = {terminal, reached}
    if opened is not None:
        alternatives.add(opened)
    try:
        await asyncio.wait(alternatives, return_when=asyncio.FIRST_COMPLETED)
        assert fault.fired, f"operation passed unreachable boundary: {fault.boundary}:{fault.side}"
    finally:
        reached.cancel()
        await asyncio.gather(reached, return_exceptions=True)


async def run_fault_trace(live: LiveTrace, actions: list[Action], fault: FaultBoundary) -> None:
    """Crash on either side of an effect, retain storage and external jobs, then replay."""
    # Generated nonterminal inputs happen before the selected boundary. Terminal
    # actions are covered by the same property's ordinary trace alternative.
    for number, action in enumerate(actions):
        if action is Action.STEER:
            await live.steer(number)
        elif action is Action.INTERRUPT:
            await live.dynamic.workstreams.interrupt("a")
            await _opened_turn_or_ended_search(live.held, live.task)
    fault.armed = True
    if fault.boundary in {Boundary.PREPARE, Boundary.DISPATCH, Boundary.SESSION}:
        await live.dynamic.workstreams.interrupt("a")
    elif fault.boundary is Boundary.SUBMIT:
        grant = live.effects.service.grant(
            principal_id="implementer:a",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=live.effects.base.workspaces.candidates[-1].id,
        )
        operation = asyncio.create_task(
            live.effects.service.dispatch(
                SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.BENCHMARK,))
            )
        )
        await wait_boundary(fault, operation)
        await asyncio.gather(operation, return_exceptions=True)
    else:
        await live.loop.withdraw("a", Withdrawal.CANCEL)
    if fault.boundary in {Boundary.PREPARE, Boundary.DISPATCH, Boundary.SESSION}:
        opened = asyncio.create_task(live.held.opened.get())
        try:
            await wait_boundary(fault, live.task, opened)
        finally:
            opened.cancel()
            await asyncio.gather(opened, return_exceptions=True)
    else:
        await wait_boundary(fault, live.task)
    fault.armed = False
    await restart_faulted_host(live)


async def restart_faulted_host(live: LiveTrace) -> None:
    """Retain durable state and external jobs while replacing the crashed host."""
    live.task.cancel()
    await asyncio.gather(live.task, return_exceptions=True)
    await live.dynamic.input_gate.stop()
    # A new host owns new sessions and containers. Model process teardown
    # through the workspace port while keeping durable revisions and jobs.
    for workspace in live.effects.base.workspaces.candidates:
        await workspace.discard()
    try:
        recovered = await _DynamicRun.open(live.effects.restart(), live.dynamic.options)
    except RuntimeContractError as error:
        if "requires reconciliation" not in str(error):
            raise
        return
    loop = recovered.search_loop()
    task = asyncio.create_task(loop.run(recovered.recoverable()))
    opened = asyncio.create_task(live.held.opened.get())
    try:
        done, _ = await asyncio.wait({task, opened}, return_when=asyncio.FIRST_COMPLETED)
        if opened in done and not task.done():
            await loop.withdraw("a", Withdrawal.CANCEL)
        try:
            await task
        except RuntimeContractError as error:
            if "requires reconciliation" not in str(error):
                raise
    finally:
        opened.cancel()
        await asyncio.gather(opened, return_exceptions=True)
        await recovered.input_gate.stop()


@settings(max_examples=60, deadline=None)
@example(actions=[Action.STEER, Action.INTERRUPT, Action.PARK], barrier=None, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT, Action.CANCEL], barrier=None, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT, Action.STOP], barrier=None, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT, Action.CRASH], barrier=None, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.PREPARE, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.PREPARE, side=Side.AFTER)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.DISPATCH, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.DISPATCH, side=Side.AFTER)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.SETTLE, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.SETTLE, side=Side.AFTER)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.RELEASE, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.RELEASE, side=Side.AFTER)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.SUBMIT, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.SUBMIT, side=Side.AFTER)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.CANCEL, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.CANCEL, side=Side.AFTER)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.SESSION, side=Side.BEFORE)
@example(actions=[Action.STEER, Action.INTERRUPT], barrier=Boundary.SESSION, side=Side.AFTER)
@given(
    actions=st.lists(st.sampled_from(tuple(Action)), min_size=1, max_size=10),
    barrier=st.one_of(st.none(), st.sampled_from(tuple(Boundary))),
    side=st.sampled_from(tuple(Side)),
)
def test_composed_recovery_preserves_resources_candidates_and_steers(
    actions: list[Action], barrier: Boundary | None, side: Side
) -> None:
    """One trace covers intents, capture release and notes across commit/effect crashes."""
    asyncio.run(run_trace(actions, barrier, side))


async def _opened_turn_or_ended_search(held: HeldTurns, task: asyncio.Task[SearchEnd]) -> int:
    """Surface an ended worker instead of waiting for a provider it never reached."""
    opened = asyncio.create_task(held.opened.get())
    try:
        done, _ = await asyncio.wait({opened, task}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            pytest.fail("search ended before the expected provider turn")
        return await opened
    finally:
        opened.cancel()
        await asyncio.gather(opened, return_exceptions=True)


@pytest.mark.parametrize("held_role", [IMPLEMENTER, JUDGE], ids=["implementer", "judge"])
def test_park_continuation_reopens_scope_and_preserves_invocation_identity(
    held_role: AgentRole,
) -> None:
    """A deliberate continuation can submit fresh jobs after its old release."""

    async def run() -> None:
        script = Script(
            {
                ORCHESTRATOR.id: [portfolio("a"), portfolio("a", continue_hypothesis=True)],
                IMPLEMENTER.id: [implementation("a"), implementation("a")],
                JUDGE.id: [{"passed": True, "analysis": "Correct."}] * 2,
            }
        )
        held = HeldTurns(script, role=held_role)
        base = baseline_run(Path("/memory/park-resume"), script, responder=held.respond)
        base.workspaces.set_default_patch("diff --git a/engine.py b/engine.py")
        effects = profile_release_effects(Path("/memory/park-resume"), base)
        first = await _DynamicRun.open(effects.run, dynamic_options(max_in_flight=1))
        first.state.agent = AgentLoopState()
        loop = first.search_loop()
        task = asyncio.create_task(loop.run(first.recoverable()))
        await _opened_turn_or_ended_search(held, task)
        try:
            await effects.own_resources(base.workspaces.candidates[-1], "a")
            await loop.withdraw("a", Withdrawal.PARK)
            await task
            assert all(session.closed for session in base.agents.sessions)
            scope_id = base.workspaces.candidates[-1].id
            assert scope_id is not None
            await effects.assert_released(scope_id)
            old_counter = first.state.workstreams[0].invocation_sequence
            turns_per_attempt = 1 if held_role is IMPLEMENTER else 2
            assert old_counter == turns_per_attempt
            await first.input_gate.stop()
            resumed = await _DynamicRun.open(
                effects.restart(), dynamic_options(max_in_flight=1, max_rounds=2)
            )
            resumed_loop = resumed.search_loop()
            resumed_task = asyncio.create_task(resumed_loop.run(resumed.recoverable()))
            await _opened_turn_or_ended_search(held, resumed_task)
            assert not await effects.evaluation.jobs_released("a")
            assert (
                resumed.state.workstreams[0].invocation_sequence == old_counter + turns_per_attempt
            )
            assert all(
                intent.stage is IntentStage.COMPLETED
                for intent in resumed.state.lifecycle.intents.values()
                if intent.kind.value == "reopen"
            )
            await effects.own_resources(base.workspaces.candidates[-1], "a")
            await resumed_loop.withdraw("a", Withdrawal.CANCEL)
            await resumed_task
            assert all(session.closed for session in base.agents.sessions)
            scope_id = base.workspaces.candidates[-1].id
            assert scope_id is not None
            await effects.assert_released(scope_id)
            await resumed.input_gate.stop()
        finally:
            await first.input_gate.stop()
            await effects.close()

    asyncio.run(run())
