"""A pause's drain lets the turns it admitted go on: the gates that answer "may it?" agree (#3475).

Three places decide whether a run that is draining an operator's pause may go on with a turn:
the decision gate (``deferred`` and ``validate_decision``), the effect gate (``dispatch_allowed``)
and the sessions' check that the scope still owns the turn. The last one refused a draining run,
so a run-scoped turn asked for before the pause never got its input reserved, its acquisition
stalled and the search lost the turn (a resumed run with no winner). These cases drive the public
``step`` over every pause, scope and turn it can meet: a turn asked for before a pause is admitted
once its session answers, and a turn the decision gate lets through a drain is admitted too.
"""

from __future__ import annotations

import enum
from typing import Literal

import pytest

import vs_core.api as core
from vs_core.api.testing import sample_limits

from .test_adoption import RUN_SCOPE
from .test_run_session_drain import Executor
from .test_turn_permit import write_turn_world


class Pause(enum.StrEnum):
    """What holds the run when its session answers: nothing, or one way of pausing it."""

    NONE = "none"
    OPERATOR = "operator"
    NOW = "now"
    PARK = "park"
    QUOTA = "quota"


type Owner = Literal["run", "attempt"]
type Turn = Literal["first", "successor"]

OWNERS: tuple[Owner, ...] = ("run", "attempt")
TURNS: tuple[Turn, ...] = ("first", "successor")


def _with_turns(state: core.CoreState) -> core.CoreState:
    """The run may spend as many turns as the cases ask for."""
    return state.model_copy(
        update={"run": state.run.model_copy(update={"limits": sample_limits(max_turns=10)})}
    )


def _world(owner: Owner) -> tuple[Executor, core.Scope]:
    """A run, or a run with one active attempt, in which no session exists yet."""
    if owner == "run":
        state = core.initial_state()
        return Executor(_with_turns(state)), RUN_SCOPE
    state, held = write_turn_world(access=core.Access.READ_ONLY)
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(),
            "records": state.records.model_copy(update={"records": ()}),
        }
    )
    return Executor(_with_turns(state)), held.scope


def paid_or_free(scope: core.Scope) -> Literal["paid", "free"]:
    """An attempt pays for its turns; the run's own turns are free."""
    return "free" if scope == RUN_SCOPE else "paid"


def _spec(
    scope: core.Scope, number: int, predecessor: core.InvocationRef | None = None
) -> core.TurnSpec:
    return core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="planner"),
            policy="reuse",
            lifetime="owner" if scope != RUN_SCOPE else "ephemeral",
            access=core.Access.READ_ONLY,
        ),
        invocation_id=core.InvocationId(root=f"invocation-{number}"),
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="plan", version=1),
        charge_class="correction" if predecessor is not None else paid_or_free(scope),
        predecessor=predecessor,
    )


def _ask(scope: core.Scope, spec: core.TurnSpec) -> core.Wait:
    return core.Wait(
        request=core.RequestTurn(
            decision_id=core.DecisionId(root=spec.invocation_id.root), scope=scope, turn=spec
        ),
        until=core.Within(limit=100.0),
    )


def _submit(executor: Executor, decision: core.Wait) -> core.Transition:
    return core.step(
        executor.state,
        core.DecisionSubmitted(decision=decision, expected_revision=executor.state.revision),
    )


HOLDS: dict[Pause, tuple[core.ControlAction, core.PauseOwner | None]] = {
    Pause.OPERATOR: ("pause", core.PauseOwner.OPERATOR),
    Pause.NOW: ("pause_now", None),
    Pause.PARK: ("park", None),
    Pause.QUOTA: ("pause", core.PauseOwner.QUOTA),
}
"""The control that puts the run under each pause."""


def _hold(executor: Executor, pause: Pause) -> None:
    if pause is Pause.NONE:
        return
    action, owner = HOLDS[pause]
    executor.feed(
        core.RunControlEvent(
            control=core.ControlInput(
                control_id=core.ControlId(root=f"hold-{pause.value}"), action=action, owner=owner
            ),
            now_at=executor.now,
        )
    )


def _pending[R: core.Request](executor: Executor, kind: type[R]) -> R:
    return next(row for row in executor.pending() if isinstance(row, kind))


def _under_way(executor: Executor, request: core.Request) -> None:
    """The shell authorized the request's dispatch: it is in flight."""
    assert request.request_id is not None
    executor.authorized.add(request.request_id.root)
    executor.feed(core.DispatchAuthorized(request_id=request.request_id))


def _dispatched(state: core.CoreState, number: int) -> bool:
    """Whether the sessions took the turn: its dispatch is in the ledger, not a dead acquisition."""
    return any(
        isinstance(row.request, core.DispatchTurn)
        and row.request.turn.invocation_id.root == f"invocation-{number}"
        for row in state.records.records
    )


def _first_turn_under_a_pause(owner: Owner, pause: Pause) -> Executor:
    """The turn was asked for and its session is being created when the pause lands."""
    executor, scope = _world(owner)
    executor.feed(
        core.DecisionSubmitted(
            decision=_ask(scope, _spec(scope, 1)), expected_revision=executor.state.revision
        )
    )
    ensure = _pending(executor, core.EnsureSession)
    _under_way(executor, ensure)
    _hold(executor, pause)
    executor.answer(ensure, core.ObservationStatus.SUCCEEDED, twice=False)
    return executor


@pytest.mark.parametrize("pause", list(Pause))
@pytest.mark.parametrize("owner", OWNERS)
def test_a_turn_asked_for_before_a_pause_is_admitted_when_its_session_answers_under_it(
    owner: Owner, pause: Pause
) -> None:
    executor = _first_turn_under_a_pause(owner, pause)

    assert _dispatched(executor.state, 1), (owner, pause)


def _successor_after_a_pause(owner: Owner, pause: Pause) -> tuple[Executor, core.Wait]:
    """A turn runs when the pause lands and fails under it; its correction is then asked for."""
    executor, scope = _world(owner)
    first = _spec(scope, 1)
    executor.feed(
        core.DecisionSubmitted(
            decision=_ask(scope, first), expected_revision=executor.state.revision
        )
    )
    ensure = _pending(executor, core.EnsureSession)
    executor.answer(ensure, core.ObservationStatus.SUCCEEDED, twice=False)
    running = _pending(executor, core.DispatchTurn)
    _under_way(executor, running)
    _hold(executor, pause)
    executor.answer(running, core.ObservationStatus.FAILED, twice=False)
    ref = core.InvocationRef(
        session_id=first.session.session_id, invocation_id=first.invocation_id, generation=0
    )
    return executor, _ask(scope, _spec(scope, 2, predecessor=ref))


HOLDS_A_SUCCESSOR = frozenset({Pause.NOW, Pause.QUOTA})
"""The pauses whose decision gate waits for the resume instead of letting a successor through."""


@pytest.mark.parametrize("pause", list(Pause))
@pytest.mark.parametrize("owner", OWNERS)
def test_a_successor_the_decision_gate_lets_through_a_pause_is_admitted_by_the_sessions(
    owner: Owner, pause: Pause
) -> None:
    executor, decision = _successor_after_a_pause(owner, pause)

    held = core.deferred(executor.state, decision)

    assert held == (pause in HOLDS_A_SUCCESSOR), (owner, pause)
    if not held:
        result = _submit(executor, decision)
        assert any(isinstance(event, core.Accepted) for event in result.events), (owner, pause)
        assert _dispatched(result.state, 2), (owner, pause)
