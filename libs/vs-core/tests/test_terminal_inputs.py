"""Run terminal publication follows input finalization and interruption cleanup."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def closing_state() -> core.CoreState:
    state = core.initial_state()
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": core.RunResultProposal(outcome="cancelled", reason="finished"),
                }
            )
        }
    )


def drain(state: core.CoreState) -> core.Transition:
    clock = core.ClockAdvanced(now_at=5.0)
    return core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(
                        state=state.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )


@given(target=st.sampled_from(["scope", "item"]))
def test_pending_inputs_reach_typed_inputs_finalization_before_run_ended(target: str) -> None:
    state = closing_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    session_input = core.SessionInput(
        input_id=core.InputId(root="input"),
        target=core.ScopeInputTarget(scope=scope)
        if target == "scope"
        else core.ItemInputTarget(item_id=core.ItemId(root="item")),
        artifact=core.ArtifactRef(artifact_id=core.ArtifactId(root="input"), digest="input"),
        received_at=1.0,
        sequence=0,
    )
    state = state.model_copy(
        update={"sessions": core.SessionsState(inputs=(core.InputRecord(input=session_input),))}
    )
    with pytest.raises(core.KernelNotImplementedError) as failure:
        drain(state)
    assert failure.value.subarea == "_session_inputs"
    assert failure.value.event_kind == "finish_run"
    assert state.run.status == core.RunStatus.CLOSING
    assert state.sessions.inputs[0].receipt is None


def test_existing_input_receipts_allow_one_final_publication_without_reissuing_them() -> None:
    state = closing_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    session_input = core.SessionInput(
        input_id=core.InputId(root="input"),
        target=core.ScopeInputTarget(scope=scope),
        artifact=core.ArtifactRef(artifact_id=core.ArtifactId(root="input"), digest="input"),
        received_at=1.0,
        sequence=0,
    )
    receipt = core.InputDropped(
        input_id=session_input.input_id,
        target=core.ScopeInputTarget(scope=scope),
        reason=core.InputDropReason.OWNER_CANCELLED,
        at=2.0,
    )
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(
                inputs=(core.InputRecord(input=session_input, receipt=receipt),)
            )
        }
    )
    result = drain(state)
    assert result.state.run.status == core.RunStatus.TERMINAL
    assert result.state.sessions.inputs == state.sessions.inputs
    assert len(result.events) == 1
    assert isinstance(result.events[-1], core.RunEnded)
    assert drain(result.state).events == ()


@given(phase=st.sampled_from(["pending", "draining", "checkpointed", "completed", "blocked"]))
def test_unfinished_interruption_claims_fence_terminal_publication(
    phase: Literal["pending", "draining", "checkpointed", "completed", "blocked"],
) -> None:
    state = closing_state()
    claim = core.InterruptClaim(
        invocation=core.InvocationRef(
            session_id=core.SessionId(root="session"),
            invocation_id=core.InvocationId(root="invocation"),
            generation=0,
        ),
        authority=core.RequestId(root="interrupt"),
        refund=0,
        phase=phase,
    )
    state = state.model_copy(update={"sessions": core.SessionsState(interrupts=(claim,))})
    result = drain(state)
    expected = core.RunStatus.TERMINAL if phase == "completed" else core.RunStatus.CLOSING
    assert result.state.run.status == expected
    assert bool(result.events) == (phase == "completed")


def test_terminal_transition_cannot_hide_new_outbox_work_not_yet_registered() -> None:
    state = closing_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    request = core.EnsureWorkspace(
        scope=scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        plan=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
    )
    clock = core.ClockAdvanced(now_at=5.0)
    with pytest.raises(core.ContractError, match="terminal transition"):
        core.trace_step(
            state,
            clock,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=clock,
                        change=core.SchedulingChange(
                            state=state.scheduling,
                            requests=(request,),
                            signals=(core.RunDrained(),),
                        ),
                    ),
                )
            ),
        )
