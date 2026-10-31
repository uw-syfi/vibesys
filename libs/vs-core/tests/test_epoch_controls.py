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
                core.EpochResumed(epoch=state.records.recovery.epoch + 1, now_at=float(index)),
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
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    result = core.step(state, core.RunControlEvent(control=control("pause_now", 1), now_at=1))
    assert result.state.run.status is core.RunStatus.DRAINING
    (cancel,) = [request for request in result.requests if isinstance(request, core.CancelTurn)]
    assert cancel.request_id is not None
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
    released = core.step(dispatched, core.RequestObserved(observation=observed))
    # The turn's own dispatch has not returned: the epoch exits only once it has recorded its end.
    assert released.state.run.status is core.RunStatus.DRAINING
    returned = terminal_yield(released.state, turn, None, core.ObservationStatus.CANCELLED)
    recorded = core.step(released.state, core.RequestObserved(observation=returned.observation))
    ended = core.step(recorded.state, returned)
    assert ended.state.run.status is core.RunStatus.PARKED
    assert ended.state.run.result is None
    assert ended.state.sessions.run_charges == state.sessions.run_charges


def test_pause_now_ends_a_dispatch_that_never_returns_a_cleanup_bound_after_its_deadline() -> None:
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    unbounded = tuple(
        row.model_copy(update={"until": row.until.model_copy(update={"at": 1e9})})
        for row in state.records.records
    )
    state = state.model_copy(
        update={"records": state.records.model_copy(update={"records": unbounded})}
    )
    state = core.step(state, core.RunControlEvent(control=control("pause_now", 1), now_at=1)).state
    bound = state.run.limits.cleanup_bound
    assert state.run.park is not None
    assert state.run.park.deadline_at == 1 + bound
    dispatch = state.records.record(turn.request_id)
    assert dispatch is not None
    assert dispatch.until.at == 1 + 2 * bound
    cancellation = next(
        row for row in state.records.records if isinstance(row.request, core.CancelTurn)
    )
    state = core.step(state, core.DispatchAuthorized(request_id=cancellation.request_id)).state
    state = core.step(state, core.ClockAdvanced(now_at=1 + bound)).state
    assert state.run.status is core.RunStatus.DRAINING
    ended = core.step(state, core.ClockAdvanced(now_at=1 + 2 * bound))
    assert ended.state.run.status is core.RunStatus.PARKED
    assert ended.state.run.result is None
    retained = ended.state.records.record(turn.request_id)
    assert retained is not None
    assert retained.phase is core.RequestPhase.BLOCKED


@pytest.mark.regression
@example(epoch=1, immediate=False)
@given(epoch=st.integers(min_value=1, max_value=100), immediate=st.booleans())
def test_writer_recovery_keeps_pause_authority_until_operator_resume(
    epoch: int, *, immediate: bool
) -> None:
    state = core.step(core.initial_state(), core.RecoveryStarted(epoch=epoch, now_at=0)).state
    state = core.step(
        state,
        core.RunControlEvent(control=control("pause_now" if immediate else "pause", 1), now_at=1),
    ).state
    assert state.run.status is core.RunStatus.PARKED
    park = state.run.park
    assert park is not None
    for recovered_epoch in (epoch + 1, epoch + 2):
        recovered = core.step(state, core.RecoveryStarted(epoch=recovered_epoch, now_at=2))
        assert recovered.state.run.status is core.RunStatus.PARKED
        assert recovered.state.run.controls == state.run.controls
        assert recovered.state.run.park is not None
        assert recovered.state.run.park.control_id == park.control_id
        assert recovered.state.run.park.epoch == recovered_epoch
        assert recovered.state.scheduling.admission_closed
        assert not any(isinstance(event, core.StopLiftRecorded) for event in recovered.events)
        state = recovered.state
    resumed = core.step(state, core.EpochResumed(epoch=epoch + 3, now_at=3))
    assert resumed.state.run.status is core.RunStatus.RUNNING
    assert resumed.state.run.park is None
    assert [control.action for control in resumed.state.run.controls] == ["resume"]
    assert not resumed.state.scheduling.admission_closed
    assert any(
        isinstance(event, core.StopLiftRecorded) and event.new_epoch for event in resumed.events
    )


@pytest.mark.regression
@example(epoch=1, outcome="success")
@example(epoch=1, outcome="cancelled")
@given(
    epoch=st.integers(min_value=1, max_value=100),
    outcome=st.sampled_from(["success", "failure", "cancelled", "blocked"]),
)
def test_writer_recovery_keeps_the_completed_epoch_and_never_lifts_its_stop(
    epoch: int, outcome: str
) -> None:
    state = core.step(core.initial_state(), core.RecoveryStarted(epoch=epoch, now_at=0)).state
    ended = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.TERMINAL,
                    "result": core.RunResultProposal.model_validate(
                        {"outcome": outcome, "reason": "done"}
                    ),
                    "controls": (
                        core.ControlInput(
                            control_id=core.ControlId(root="ending"),
                            action="stop",
                            mode=core.StopMode.DRAIN,
                        ),
                    ),
                }
            )
        }
    )
    assert ended.run.status is core.RunStatus.TERMINAL
    for recovered_epoch in (epoch + 1, epoch + 2):
        recovered = core.step(ended, core.RecoveryStarted(epoch=recovered_epoch, now_at=2))
        assert recovered.state.run.status is core.RunStatus.TERMINAL
        assert recovered.state.run.result == ended.run.result
        assert recovered.state.run.controls == ended.run.controls
        assert recovered.state.run.receipts == ended.run.receipts
        assert not recovered.requests
        assert not any(isinstance(event, core.StopLiftRecorded) for event in recovered.events)
        ended = recovered.state
    resumed = core.step(ended, core.EpochResumed(epoch=epoch + 3, now_at=3))
    assert resumed.state.run.status is core.RunStatus.RUNNING
    assert resumed.state.run.result is None
    assert [control.action for control in resumed.state.run.controls] == ["resume"]
    assert core.requested_stop_mode(resumed.state.run) is None


def test_a_parked_epoch_records_the_delivery_of_a_steer_it_already_holds() -> None:
    """A steer admitted while parked keeps the input the next turn will read.

    The control and its delivery are separate events, so a poll can commit the
    control after the epoch has parked and the delivery on the next step. The
    epoch still ignores an input that delivers no steer it holds.
    """
    paused = core.step(
        core.initial_state(),
        core.RunControlEvent(control=control("pause", 1), now_at=1),
    ).state
    assert paused.run.status is core.RunStatus.PARKED
    artifact = core.ArtifactRef(artifact_id=core.ArtifactId(root="steer-text"), digest="abc")
    steer = core.ControlInput(
        control_id=core.ControlId(root="op-s1"), action="steer", artifact=artifact
    )
    held = core.step(paused, core.RunControlEvent(control=steer, now_at=2)).state
    assert held.sessions.inputs == ()
    run = core.Scope(owner=held.run.run_id, generation=held.run.generation)
    delivery = core.SessionInput(
        input_id=core.InputId(root="steer-op-s1"),
        target=core.ScopeInputTarget(scope=run),
        artifact=artifact,
        received_at=2,
        sequence=2,
    )
    restored = core.step(held, core.SessionInputReceived(input=delivery)).state
    assert [row.input for row in restored.sessions.inputs] == [delivery]
    other = delivery.model_copy(
        update={"input_id": core.InputId(root="input-other"), "sequence": 3}
    )
    ignored = core.step(restored, core.SessionInputReceived(input=other))
    assert ignored.state.sessions.inputs == restored.sessions.inputs
    assert ignored.state.revision == restored.revision
