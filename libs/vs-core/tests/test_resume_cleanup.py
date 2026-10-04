"""Irreversible cleanup cannot reopen through resume or pause/resume controls."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


@given(
    st.sampled_from([core.RunStatus.CLOSING, core.RunStatus.BLOCKED]),
    st.sampled_from(["resume", "pause"]),
)
def test_cleanup_status_cannot_resume_or_pause_into_an_admissible_run(
    status: core.RunStatus, action: Literal["pause", "resume"]
) -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": status,
                    "result": core.RunResultProposal(outcome="cancelled", reason="cleanup pending"),
                }
            )
        }
    )
    event = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="control"), action=action),
        now_at=10.0,
    )
    with pytest.raises(core.ContractError, match="cleanup"):
        core.trace_step(
            state,
            event,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=core.AdmissionControl(action=action),
                        change=core.SchedulingChange(state=state.scheduling),
                    ),
                )
            ),
        )
    assert state.run.status == status


def test_paused_run_with_stop_result_cannot_resume_before_cleanup_finishes() -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.PAUSED,
                    "result": core.RunResultProposal(outcome="cancelled", reason="stop committed"),
                }
            )
        }
    )
    event = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="resume"), action="resume"),
        now_at=10.0,
    )
    with pytest.raises(core.ContractError, match="cleanup"):
        core.trace_step(
            state,
            event,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=core.AdmissionControl(action="resume"),
                        change=core.SchedulingChange(state=state.scheduling),
                    ),
                )
            ),
        )


def test_run_drained_waits_for_registered_job_release() -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    job = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="job"),
        request_id=core.RequestId(root="request"),
        scope=scope,
        resource_pool=core.PoolId(root="jobs"),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": core.RunResultProposal(outcome="cancelled", reason="cleanup"),
                }
            ),
            "evaluation": core.EvaluationState(registered_jobs=(job,)),
        }
    )
    event = core.ClockAdvanced(now_at=10.0)
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=event,
                    change=core.SchedulingChange(
                        state=state.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )
    assert result.state.run.status == core.RunStatus.CLOSING
    assert result.events == ()
