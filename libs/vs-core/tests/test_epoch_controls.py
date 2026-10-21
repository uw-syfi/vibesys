"""Operator controls close one epoch without making a run permanently final."""

from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

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
