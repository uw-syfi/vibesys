"""Operator controls close one epoch without making a run permanently final."""

from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_yield_completion import terminal_yield
from .test_write_turn_retention import write_turn_world

type _Action = Literal["pause", "pause_now", "resume"]


def control(action: _Action, index: int) -> core.ControlInput:
    return core.ControlInput(
        control_id=core.ControlId(root=f"operator-{index}"),
        action=action,
        owner=core.PauseOwner.OPERATOR if action == "pause" else None,
        when="now" if action == "resume" else None,
    )


@pytest.mark.regression
@example(outcome="failure", source="run-environment")
@example(outcome="cancelled", source="deadline")
@example(outcome="success", source="operator")
@given(
    outcome=st.sampled_from(["success", "failure", "cancelled", "blocked"]),
    source=st.sampled_from(["operator", "deadline", "run-environment"]),
)
def test_resume_accepts_every_prior_ending(outcome: str, source: str) -> None:
    state = core.initial_state()
    result = core.RunResultProposal.model_validate({"outcome": outcome, "reason": source})
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.TERMINAL,
                    "result": result,
                    "controls": (
                        core.ControlInput(
                            control_id=core.ControlId(root=source),
                            action="stop",
                            mode=core.StopMode.CANCEL,
                        ),
                    ),
                }
            )
        }
    )
    resumed = core.step(state, core.RunControlEvent(control=control("resume", 1), now_at=1))
    assert resumed.state.run.status == core.RunStatus.RUNNING
    assert resumed.state.run.result is None
    assert core.requested_stop_mode(resumed.state.run) is None
    assert not resumed.state.scheduling.admission_closed


@pytest.mark.regression
@example(actions=["pause", "pause_now", "resume", "pause"])
@given(actions=st.lists(st.sampled_from(["pause", "pause_now", "resume"]), min_size=1, max_size=40))
def test_controls_are_fenced_to_their_epoch(actions: list[_Action]) -> None:
    state = core.initial_state()
    for index, action in enumerate(actions):
        request = control(action, index)
        if action == "resume" and state.run.status == core.RunStatus.PARKED:
            state = core.step(
                state,
                core.RecoveryStarted(epoch=state.records.recovery.epoch + 1, now_at=float(index)),
            ).state
        refusal = core.control_refusal(state, request)
        if refusal is not None:
            assert state.run.status == core.RunStatus.PARKED
            continue
        transition = core.step(state, core.RunControlEvent(control=request, now_at=float(index)))
        state = transition.state
        assert state.run.result is None
        assert not any(isinstance(event, core.Failed) for event in transition.events)
        if action == "resume":
            assert state.run.status == core.RunStatus.RUNNING
            assert state.run.park is None
            assert not state.scheduling.admission_closed
        else:
            assert state.run.status == core.RunStatus.PARKED
            assert state.scheduling.admission_closed


@pytest.mark.regression
@example(immediate=False, independent=False)
@given(immediate=st.booleans(), independent=st.booleans())
def test_pause_preserves_only_an_admitted_logical_turns_successors(
    *, immediate: bool, independent: bool
) -> None:
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    paused = core.step(
        state,
        core.RunControlEvent(control=control("pause_now" if immediate else "pause", 1), now_at=1.0),
    ).state
    original = state.sessions.invocations[0]
    if not immediate:
        ended = terminal_yield(paused, turn, None)
        paused = core.step(paused, core.RequestObserved(observation=ended.observation)).state
        paused = core.step(paused, ended).state
    successor = turn.turn.model_copy(
        update={
            "invocation_id": core.InvocationId(root="successor"),
            "predecessor": None if independent else original.invocation,
            "charge_class": "paid" if independent else "correction",
        }
    )
    command = core.Wait(
        request=core.RequestTurn(
            decision_id=core.DecisionId(root="successor"), scope=turn.scope, turn=successor
        ),
        until=core.Within(limit=1000),
    )
    assert core.deferred(paused, command) is (immediate or independent)
    if not immediate and not independent:
        accepted = core.step(
            paused, core.DecisionSubmitted(decision=command, expected_revision=paused.revision)
        )
        assert any(isinstance(event, core.Accepted) for event in accepted.events), accepted.events
        receipt = next(
            row
            for row in accepted.state.run.receipts
            if row.decision_id == command.request.decision_id
        )
        assert receipt.until is not None
        assert receipt.until.at <= original.until.at


def test_pause_now_releases_active_invocations_before_the_epoch_exits() -> None:
    state, _ = write_turn_world(access=core.Access.READ_ONLY)
    result = core.step(state, core.RunControlEvent(control=control("pause_now", 1), now_at=1))
    assert result.state.run.status is core.RunStatus.DRAINING
    (cancel,) = [request for request in result.requests if isinstance(request, core.CancelTurn)]
    assert cancel.invocation == state.sessions.invocations[0].invocation
    assert not any(isinstance(event, core.Failed) for event in result.events)
    repeated = core.step(result.state, core.ClockAdvanced(now_at=2))
    assert not repeated.requests
    dispatched = core.step(
        repeated.state, core.DispatchAuthorized(request_id=cancel.request_id)
    ).state
    observed = core.Observation(
        event_id=core.EventId(root="cancelled-invocation"),
        request_id=cancel.request_id,
        scope=cancel.scope,
        admission_id=cancel.admission_id,
        sequence=1,
        observed_at=2,
        status=core.ObservationStatus.CANCELLED,
        accepted=True,
        terminal=True,
        released=True,
    )
    ended = core.step(dispatched, core.RequestObserved(observation=observed))
    assert ended.state.run.status is core.RunStatus.PARKED
    assert ended.state.run.result is None
    assert ended.state.sessions.run_charges == state.sessions.run_charges
