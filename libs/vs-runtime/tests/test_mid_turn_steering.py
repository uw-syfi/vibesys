"""An interrupt-mode steer is offered to a running turn only after core records the offer.

``MidTurnSteering`` runs at each control poll against the real core. It records an offer core
picks (``InputOfferedMidTurn``), asks the provider at the next poll, once the offer is
committed, and returns to core every offer the provider declined, took back later, or that no
turn core still holds could take (``InputMidTurnReleased``). Whatever the provider answers and
wherever the host restarts, the provider is asked only for committed offers, never twice for
one offer by one process, and a declined input goes back to waiting for the next turn instead
of being offered to the same turn at every poll.
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
    open_host,
)

from vs_agent.api import SteerOutcome
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
    TurnSpec,
    Until,
    step,
)
from vs_core.testing.builders import initial_state
from vs_project.api import Project
from vs_runtime.api.core import MidTurnSteering, ReceiptStore
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
    rejecters: list[Callable[[], None]] = field(default_factory=list)

    def offer_mid_turn(self, offer: MidTurnOffer, on_rejected: Callable[[], None]) -> SteerOutcome:
        (record,) = self.core().sessions.inputs
        # Asked only for what core has committed as offered to this very turn.
        assert record.offered_to == offer.invocation
        assert record.receipt is None
        self.asked.append((offer.input.input_id, offer.invocation))
        self.rejecters.append(on_rejected)
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


_CASES = [
    (answer, reject, restart)
    for answer in ("delivered", "unsupported", "no_running_turn")
    for reject in (False, True)
    for restart in ("never", "after-offer", "after-ask")
    if answer == "delivered" or not reject
]


@pytest.mark.parametrize(("answer", "reject", "restart"), _CASES)
def test_an_offer_is_asked_once_committed_and_a_declined_one_waits_for_the_next_turn(
    answer: _Answer, *, reject: bool, restart: _Restart
) -> None:
    run = _run(answer)
    _, ref = _running()

    offered = run.poll()
    assert offered == (InputOfferedMidTurn(input_id=_INPUT, invocation=ref, now_at=0.0),)
    assert run.provider is not None
    assert run.provider.asked == []  # nothing reaches the provider before the commit
    if restart == "after-offer":
        run.restart()
    run.poll()
    assert run.provider.asked == [(_INPUT, ref)]
    if restart == "after-ask":
        run.restart()  # a new host offers and asks again: delivery is at least once
        run.settle()
        assert run.provider.asked == [(_INPUT, ref)] * 2
    if reject:
        run.provider.rejecters[-1]()
        assert run.poll()[0] == InputMidTurnReleased(input_id=_INPUT, invocation=ref, now_at=0.0)

    kept = answer == "delivered" and not reject
    record = run.record()
    assert record.receipt is None
    assert record.offered_to == (ref if kept else None)
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
        store = ReceiptStore(project.state.state_store_namespace("run"), threads=SimThreads())
        await host.run(ensure_request(), store)
        executor = host.executor(store)
        request = dispatch_request()
        context = context_for(request).model_copy(update={"until": Until(at=100.0)})
        dispatch = asyncio.ensure_future(executor.execute(request, context))
        await wait_until_started(host.turn_started, dispatch)

        def ignore() -> None:
            return None

        later = Scope(owner=SCOPE.owner, generation=1)
        assert executor.offer_mid_turn(_offer("inv-1"), ignore) is SteerOutcome.UNSUPPORTED
        assert executor.offer_mid_turn(_offer("inv-2"), ignore) is SteerOutcome.NO_RUNNING_TURN
        assert (
            executor.offer_mid_turn(_offer("inv-1", later), ignore) is SteerOutcome.NO_RUNNING_TURN
        )
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
        assert executor.offer_mid_turn(_offer("inv-1"), ignore) is SteerOutcome.NO_RUNNING_TURN
