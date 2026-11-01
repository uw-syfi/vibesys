"""An interrupt-mode steer is offered to a running turn only after core records the offer.

``MidTurnSteering`` runs at each control poll against the real core. It records an offer core
picks (``InputOfferedMidTurn``), asks the provider at the next poll, once the offer is
committed, records that the provider took it (``InputTakenMidTurn``), and returns to core
every offer the provider declined or that no turn core still holds could take
(``InputMidTurnReleased``). Whatever the provider answers and wherever the host restarts, the
provider is asked only for committed offers, never twice for one offer by one process, and a
declined input goes back to waiting for the next turn instead of being offered to the same
turn at every poll. A message the provider takes and then refuses comes back with the turn's
result, which releases it ahead of the turn's end.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from tests.support.session_world import (
    SCOPE,
    SESSION,
    dispatch_request,
    ensure_request,
    inspect_request,
    open_host,
)

from vs_agent.api import SteerOutcome
from vs_agent.api.testing import FakeSteering
from vs_core.api import (
    Access,
    ArtifactId,
    ArtifactRef,
    CoreState,
    InputId,
    InputMidTurnReleased,
    InputMode,
    InputOfferedMidTurn,
    InputRecord,
    InputTakenMidTurn,
    Invocation,
    InvocationId,
    InvocationRef,
    MidTurnOffer,
    ResourceId,
    RoleId,
    SchemaRef,
    Scope,
    ScopeInputTarget,
    SessionId,
    SessionInput,
    SessionInputReceived,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    TurnObserved,
    TurnSpec,
    Until,
    step,
)
from vs_core.testing.builders import initial_state
from vs_project.api import Project
from vs_runtime.api.core import (
    ExecutionResult,
    KeyedDocuments,
    MidTurnSteering,
    RuntimeSessionRequests,
)
from vs_runtime.api.executor_contexts import context_for
from vs_sim.api.testing import SimThreads, wait_until_started

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent

_INPUT = InputId(root="steer-op-s1")
_Answer = Literal["delivered", "unsupported", "no_running_turn"]
_Restart = Literal["never", "after-offer", "after-ask"]


def _running() -> tuple[CoreState, InvocationRef]:
    """A run with one session turn executing, run-scoped, and nothing offered to it."""
    base = initial_state()
    run = Scope(owner=base.run.run_id, generation=0)
    session = SessionSpec(
        session_id=SessionId(root="session"),
        role_id=RoleId(root="planner"),
        policy="fresh",
        lifetime="ephemeral",
        access=Access.READ_ONLY,
    )
    spec = TurnSpec(
        session=session,
        invocation_id=InvocationId(root="turn-1"),
        workspace=run,
        prompts=(),
        output_schema=SchemaRef(name="plan", version=1),
        charge_class="free",
        max_turns=1,
    )
    ref = InvocationRef(
        session_id=session.session_id, invocation_id=spec.invocation_id, generation=0
    )
    sessions = SessionsState(
        sessions=(
            SessionView(
                spec=session,
                scope=run,
                generation=0,
                phase=SessionPhase.EXECUTING,
                invocation=spec.invocation_id,
                resource_id=ResourceId(root="conversation"),
            ),
        ),
        invocations=(
            Invocation(
                until=Until(at=100.0),
                invocation=ref,
                scope=run,
                turn=spec,
                phase=SessionPhase.EXECUTING,
            ),
        ),
    )
    return base.model_copy(update={"sessions": sessions}), ref


def _interrupt(core: CoreState) -> SessionInputReceived:
    return SessionInputReceived(
        input=SessionInput(
            input_id=_INPUT,
            target=ScopeInputTarget(scope=Scope(owner=core.run.run_id, generation=0)),
            artifact=ArtifactRef(artifact_id=ArtifactId(root="steer"), digest="0" * 64),
            received_at=0.0,
            sequence=1,
            mode=InputMode.INTERRUPT,
        )
    )


@dataclass
class FakeTurnSteering:
    """A provider whose running turn answers each offer as scripted, recording what it saw."""

    answer: SteerOutcome
    core: Callable[[], CoreState]
    asked: list[tuple[InputId, InvocationRef]] = field(default_factory=list)

    def offer_mid_turn(self, offer: MidTurnOffer) -> SteerOutcome:
        (record,) = self.core().sessions.inputs
        # Asked only for what core has committed as offered to this very turn.
        assert record.offered_to == offer.invocation
        assert record.receipt is None
        self.asked.append((offer.input.input_id, offer.invocation))
        return self.answer


@dataclass
class _Run:
    core: CoreState
    provider: FakeTurnSteering | None = None
    steering: MidTurnSteering | None = None
    emitted: list[CoreEvent] = field(default_factory=list)

    def restart(self) -> None:
        assert self.provider is not None
        self.steering = MidTurnSteering(self.provider)

    def poll(self) -> tuple[CoreEvent, ...]:
        assert self.steering is not None
        events = self.steering.poll(self.core, 0.0)
        for event in events:
            self.core = step(self.core, event).state
        self.emitted.extend(events)
        return events

    def settle(self) -> None:
        """Poll until a poll changes nothing; bounded, as each offer is asked once a process."""
        for _ in range(5):
            if not self.poll():
                return
        pytest.fail("the offer never settled")

    def record(self) -> InputRecord:
        (record,) = self.core.sessions.inputs
        return record


def _run(answer: _Answer) -> _Run:
    core, _ref = _running()
    run = _Run(step(core, _interrupt(core)).state)
    run.provider = FakeTurnSteering(SteerOutcome(answer), lambda: run.core)
    run.restart()
    return run


@pytest.mark.parametrize("restart", ["never", "after-offer", "after-ask"])
@pytest.mark.parametrize("answer", ["delivered", "unsupported", "no_running_turn"])
def test_an_offer_is_asked_once_committed_and_a_declined_one_waits_for_the_next_turn(
    answer: _Answer, restart: _Restart
) -> None:
    run = _run(answer)
    _, ref = _running()

    offered = run.poll()
    assert offered == (InputOfferedMidTurn(input_id=_INPUT, invocation=ref, now_at=0.0),)
    assert run.provider is not None
    assert run.provider.asked == []  # nothing reaches the provider before the commit
    if restart == "after-offer":
        run.restart()
    answered = run.poll()
    assert run.provider.asked == [(_INPUT, ref)]
    taken = answer == "delivered"
    assert answered[0] == (
        InputTakenMidTurn(input_id=_INPUT, invocation=ref, now_at=0.0)
        if taken
        else InputMidTurnReleased(input_id=_INPUT, invocation=ref, now_at=0.0)
    )
    if restart == "after-ask":
        run.restart()  # a new host offers and asks again: delivery is at least once
        run.settle()
        assert run.provider.asked == [(_INPUT, ref)] * 2

    record = run.record()
    assert record.receipt is None
    assert record.offered_to == (ref if taken else None)
    assert (record.taken_by == ref) is taken
    # Settled: further polls neither ask again nor offer it to the same turn again.
    asked = list(run.provider.asked)
    assert run.poll() == ()
    assert run.provider.asked == asked


def test_an_offer_to_a_turn_core_no_longer_holds_is_released_without_asking() -> None:
    run = _run("delivered")
    run.poll()
    gone = run.core.sessions.model_copy(update={"invocations": ()})
    run.core = run.core.model_copy(update={"sessions": gone})

    (released,) = run.poll()

    assert isinstance(released, InputMidTurnReleased)
    assert run.provider is not None
    assert run.provider.asked == []
    assert run.record().offered_to is None


def _offer(invocation: str, scope: Scope = SCOPE) -> MidTurnOffer:
    item = SessionInput(
        input_id=_INPUT,
        target=ScopeInputTarget(scope=SCOPE),
        artifact=ArtifactRef(artifact_id=ArtifactId(root="steer"), digest="0" * 64),
        received_at=0.0,
        sequence=1,
        mode=InputMode.INTERRUPT,
    )
    return MidTurnOffer(
        input=item,
        invocation=InvocationRef(
            session_id=SESSION, invocation_id=InvocationId(root=invocation), generation=0
        ),
        scope=scope,
    )


@pytest.mark.asyncio
async def test_the_session_executor_reaches_only_the_turn_it_runs_for_the_offer() -> None:
    """The offer reaches the live provider turn of the invocation and scope it names, only.

    The Fake provider takes no message mid-turn, so a reached turn answers ``UNSUPPORTED``;
    any other turn, scope or a turn that ended is ``NO_RUNNING_TURN``.
    """
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        project = Project.open(base / "project")
        host = open_host(base / "workspace", hang_until_cancelled=True)
        store = KeyedDocuments(project.state.state_store_namespace("run"), threads=SimThreads())
        await host.run(ensure_request(), store)
        executor = host.executor(store)
        request = dispatch_request()
        context = context_for(request).model_copy(update={"until": Until(at=100.0)})
        dispatch = asyncio.ensure_future(executor.execute(request, context))
        await wait_until_started(host.turn_started, dispatch)

        later = Scope(owner=SCOPE.owner, generation=1)
        assert executor.offer_mid_turn(_offer("inv-1")) is SteerOutcome.UNSUPPORTED
        assert executor.offer_mid_turn(_offer("inv-2")) is SteerOutcome.NO_RUNNING_TURN
        assert executor.offer_mid_turn(_offer("inv-1", later)) is SteerOutcome.NO_RUNNING_TURN
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
        assert executor.offer_mid_turn(_offer("inv-1")) is SteerOutcome.NO_RUNNING_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("steering", [FakeSteering.CONSUMED, FakeSteering.REJECTED])
async def test_a_refused_steer_is_released_by_the_turn_result_ahead_of_its_end(
    steering: FakeSteering,
) -> None:
    """The turn's own result carries the refusal, so it commits with the turn and before it.

    Core then gives the input to the next turn instead of delivering it with this one; a crash
    cannot separate the two, because the result is one durable value.
    """
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        project = Project.open(base / "project")
        answers: list[SteerOutcome] = []
        executor_holder: list[RuntimeSessionRequests] = []

        def steer_now() -> None:
            if executor_holder:
                answers.append(executor_holder[0].offer_mid_turn(_offer("inv-1")))

        host = open_host(base / "workspace", effect=steer_now, steering=steering)
        store = KeyedDocuments(project.state.state_store_namespace("run"), threads=SimThreads())
        await host.run(ensure_request(), store)
        executor = host.executor(store)
        executor_holder.append(executor)
        request = dispatch_request()
        context = context_for(request).model_copy(update={"until": Until(at=100.0)})
        result = await executor.execute(request, context)

    assert answers == [SteerOutcome.DELIVERED]
    assert isinstance(result, ExecutionResult)
    *released, ended = result.owner_events
    assert isinstance(ended, TurnObserved)
    expected = (
        [
            InputMidTurnReleased(
                input_id=_INPUT, invocation=ended.invocation, now_at=context.now_at, refused=True
            )
        ]
        if steering is FakeSteering.REJECTED
        else []
    )
    assert released == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("steering", [FakeSteering.CONSUMED, FakeSteering.REJECTED])
async def test_a_restarted_host_settles_the_refusals_of_a_finished_turn_from_its_ledger(
    steering: FakeSteering,
) -> None:
    """A host that restarts after a turn ended learns of its refusals from the session ledger.

    Core never saw the turn's result, so it asks again (``InspectTurn``). The answer carries
    the refusals the ledger stored with the turn, ahead of the turn's end, exactly as the
    turn's own result did; without them core would deliver a refused steer with this turn.
    """
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        project = Project.open(base / "project")
        executor_holder: list[RuntimeSessionRequests] = []

        def steer_now() -> None:
            if executor_holder:
                executor_holder[0].offer_mid_turn(_offer("inv-1"))

        host = open_host(base / "workspace", effect=steer_now, steering=steering)
        store = KeyedDocuments(project.state.state_store_namespace("run"), threads=SimThreads())
        await host.run(ensure_request(), store)
        executor_holder.append(host.executor(store))
        request = dispatch_request()
        context = context_for(request).model_copy(update={"until": Until(at=100.0)})
        ran = await executor_holder[0].execute(request, context)
        replayed = await host.run(
            inspect_request("req-i2", "inv-1", dispatch="req-dispatch"),
            store,
            now_at=500.0,
        )

    assert isinstance(ran, ExecutionResult)
    *refused, ended = replayed.owner_events
    assert isinstance(ended, TurnObserved)
    first_end = ran.owner_events[-1]
    assert isinstance(first_end, TurnObserved)
    assert ended.invocation == first_end.invocation
    expected = (
        [
            InputMidTurnReleased(
                input_id=_INPUT, invocation=ended.invocation, now_at=500.0, refused=True
            )
        ]
        if steering is FakeSteering.REJECTED
        else []
    )
    assert refused == expected
