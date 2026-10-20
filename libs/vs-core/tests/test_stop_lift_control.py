"""Core takes an explicit resume as the lift of a standing stop, until the run's ending commits.

A recorded stop stands until a resume follows it. While it stands the run takes no pause and no
wait for capacity, a stop only escalates (a cancel over a drain), and an immediate resume lifts
it: the stop mode reads none again, and core publishes ``StopLiftRecorded`` after the
``ControlChanged`` of that control. A run whose ending has committed (it is closing) takes no
resume, so the stop is final from there.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

type _Kind = Literal["drain", "cancel", "pause", "resume", "capacity"]
_KINDS: tuple[_Kind, ...] = ("drain", "cancel", "pause", "resume", "capacity")
type _Outcome = Literal["success", "failure", "cancelled", "blocked"]
_OUTCOMES: tuple[_Outcome, ...] = ("cancelled", "success", "failure")


def _control(kind: _Kind, index: int) -> core.ControlInput:
    identity = core.ControlId(root=f"op-{index}")
    match kind:
        case "drain" | "cancel":
            return core.ControlInput(control_id=identity, action="stop", mode=core.StopMode(kind))
        case "pause":
            return core.ControlInput(
                control_id=identity, action="pause", owner=core.PauseOwner.OPERATOR
            )
        case "resume":
            return core.ControlInput(control_id=identity, action="resume", when="now")
        case "capacity":
            return core.ControlInput(control_id=identity, action="resume", when="capacity_returns")


@given(st.lists(st.sampled_from(_KINDS), max_size=14))
def test_a_resume_lifts_a_standing_stop_and_nothing_else_does(kinds: list[_Kind]) -> None:
    state = core.initial_state()
    standing: core.StopMode | None = None
    paused = False
    for index, kind in enumerate(kinds):
        control = _control(kind, index)
        event = core.RunControlEvent(control=control, now_at=float(index))
        refused = core.control_refusal(state, control)
        if standing is not None and kind in {"pause", "capacity"}:
            assert refused is not None  # a stop that stands takes no pause and no capacity wait
            continue
        if kind == "capacity" and not paused:
            assert refused is not None  # only a paused run waits for capacity
            continue
        assert refused is None
        transition = core.step(state, event)
        state = transition.state
        lifted = [e for e in transition.events if isinstance(e, core.StopLiftRecorded)]
        changed = [e for e in transition.events if isinstance(e, core.ControlChanged)]
        assert len(changed) == 1
        match kind:
            case "drain" | "cancel":
                mode = core.StopMode(kind)
                standing = core.escalate_stop(standing, mode)
                paused = False
            case "pause":
                paused = True
            case _:
                assert bool(lifted) == (kind == "resume" and standing is not None)
                if kind == "resume":
                    standing = None
                    paused = False
        if not lifted:
            assert not [e for e in transition.events if isinstance(e, core.StopLiftRecorded)]
        assert core.requested_stop_mode(state.run) is standing


def test_a_lift_is_published_after_the_resume_that_made_it() -> None:
    state = core.initial_state()
    state = core.step(state, core.RunControlEvent(control=_control("drain", 0), now_at=0.0)).state
    lift = core.step(state, core.RunControlEvent(control=_control("resume", 1), now_at=1.0))
    kinds = [type(event) for event in lift.events]
    assert kinds.index(core.ControlChanged) < kinds.index(core.StopLiftRecorded)
    assert core.requested_stop_mode(lift.state.run) is None


def _ended_by(control: core.ControlInput) -> core.CoreState:
    """A run that ended terminal under ``control``, a stop that carried its cancelled result."""
    proposal = core.RunResultProposal(
        outcome="cancelled", reason=f"stopped: by {control.control_id.root}"
    )
    stop = core.RunControlEvent(control=control, now_at=0.0, result=proposal)
    state = core.step(core.initial_state(), stop).state
    assert state.run.status is core.RunStatus.TERMINAL
    return state


def _with_outcome(
    state: core.CoreState, outcome: Literal["success", "failure", "blocked"]
) -> core.CoreState:
    result = core.RunResultProposal(outcome=outcome, reason=outcome)
    return state.model_copy(update={"run": state.run.model_copy(update={"result": result})})


def test_a_resume_reopens_a_run_the_operators_stop_ended() -> None:
    """A stop is a pause: the run it ended is resumable, and goes on with what it held.

    Regression: a run the operator stopped ended for good, so a resume of it was refused and its
    best, its rounds and its ledger could only be abandoned for a new run.
    """
    state = _ended_by(_control("drain", 0))
    assert core.reopens_on_resume(state.run)
    resume = _control("resume", 1)
    assert core.control_refusal(state, resume) is None
    reopened = core.step(state, core.RunControlEvent(control=resume, now_at=1.0))
    run = reopened.state.run
    assert (run.status, run.result) == (core.RunStatus.RUNNING, None)
    assert core.requested_stop_mode(run) is None
    assert not [row for row in run.receipts if isinstance(row.decision, core.Finish)]
    assert not reopened.state.scheduling.admission_closed
    kinds = [type(event) for event in reopened.events]
    assert kinds.index(core.ControlChanged) < kinds.index(core.StopLiftRecorded)
    # the run's next ending is its first: core takes it as it would a run never stopped
    ending = core.Finish(
        decision_id=core.DecisionId(root="finish.2"),
        scope=core.Scope(owner=run.run_id, generation=run.generation),
        mode="drain",
        result=core.RunResultProposal(outcome="failure", reason="no-winner: done"),
    )
    again = core.step(
        reopened.state,
        core.DecisionSubmitted(decision=ending, expected_revision=reopened.state.revision),
    )
    assert not [event for event in again.events if isinstance(event, core.Rejected)]


def test_a_run_the_deadline_ended_is_not_reopened() -> None:
    state = _ended_by(
        core.ControlInput(
            control_id=core.ControlId(root="deadline"), action="stop", mode=core.StopMode.DRAIN
        )
    )
    assert not core.reopens_on_resume(state.run)
    assert core.control_refusal(state, _control("resume", 1)) is not None


def test_a_run_that_ended_by_itself_is_not_reopened() -> None:
    for outcome in ("success", "failure", "blocked"):
        state = _with_outcome(_ended_by(_control("drain", 0)), outcome)
        assert not core.reopens_on_resume(state.run)
        assert core.control_refusal(state, _control("resume", 1)) is not None


def test_a_run_whose_ending_is_still_closing_takes_no_resume() -> None:
    """Cleanup finishes first: a resume reopens the run only once the ending is committed."""
    state = _ended_by(_control("drain", 0))
    closing = state.model_copy(
        update={"run": state.run.model_copy(update={"status": core.RunStatus.CLOSING})}
    )
    assert not core.reopens_on_resume(closing.run)
    assert core.control_refusal(closing, _control("resume", 1)) is not None


@given(
    kinds=st.lists(st.sampled_from(("drain", "cancel", "resume")), min_size=1, max_size=6),
    outcome=st.sampled_from(_OUTCOMES),
    deadline=st.booleans(),
)
def test_a_resume_reopens_exactly_the_run_a_standing_operator_stop_cancelled(
    kinds: list[_Kind], outcome: _Outcome, *, deadline: bool
) -> None:
    """The rule over the controls a run recorded before it ended, and its result."""
    state = core.initial_state()
    standing = False
    for index, kind in enumerate(kinds):
        if kind == "resume" and not standing:
            continue  # a resume with no stop standing is a plain resume
        state = core.step(
            state, core.RunControlEvent(control=_control(kind, index), now_at=float(index))
        ).state
        standing = kind != "resume"
    if standing and deadline:
        stop = core.ControlInput(
            control_id=core.ControlId(root="deadline"), action="stop", mode=core.StopMode.DRAIN
        )
        state = core.step(state, core.RunControlEvent(control=stop, now_at=9.0)).state
    ended = state.run.model_copy(
        update={
            "status": core.RunStatus.TERMINAL,
            "result": core.RunResultProposal(outcome=outcome, reason="x"),
        }
    )
    expected = standing and outcome == "cancelled" and not deadline
    assert core.reopens_on_resume(ended) is expected


type _Requester = Literal["operator", "deadline", "environment"]


def _stop_by(requester: _Requester, index: int) -> core.ControlInput:
    """A stop asked for by the operator (an ``op-`` id), the deadline, or the run environment."""
    identity = {
        "operator": f"op-{index}",
        "deadline": core.DEADLINE_STOP,
        "environment": core.ENVIRONMENT_STOP,
    }[requester]
    return core.ControlInput(
        control_id=core.ControlId(root=identity), action="stop", mode=core.StopMode.CANCEL
    )


@given(
    stops=st.lists(
        st.sampled_from(("operator", "deadline", "environment", "resume")), min_size=1, max_size=6
    ),
    outcome=st.sampled_from(_OUTCOMES),
)
@example(stops=["environment"], outcome="cancelled")  # the reported case: a failed probe
@example(stops=["operator", "environment"], outcome="cancelled")
def test_only_an_operator_stop_reopens_a_run_and_a_final_stop_since_the_last_resume_never_does(
    stops: list[_Requester | Literal["resume"]], outcome: _Outcome
) -> None:
    """Regression (sweep F2): the run environment's stop was taken for an operator's pause.

    A probe that failed for good stopped the run with no id of its own, so its record read
    resumable and a resume replayed the failure. The deadline's stop and the environment's are
    the host's verdicts: once either stands, no resume reopens the run.
    """
    controls: list[core.ControlInput] = []
    standing = final = False
    for index, kind in enumerate(stops):
        if kind == "resume":
            controls.append(_control("resume", index))
            standing = final = False
            continue
        controls.append(_stop_by(kind, index))
        standing = True
        final = final or kind != "operator"
    result = core.RunResultProposal(outcome=outcome, reason="x")
    expected = standing and not final and outcome == "cancelled"
    assert core.reopens_after(core.RunStatus.TERMINAL, result, controls) is expected


type _HostKind = Literal["drain", "cancel", "resume", "deadline", "environment"]
_HOST_KINDS: tuple[_HostKind, ...] = ("drain", "cancel", "resume", "deadline", "environment")


def _host_control(kind: _HostKind, index: int) -> core.ControlInput:
    """An operator's stop or resume, or one of the host's own stops under its reserved id."""
    if kind in {"deadline", "environment"}:
        identity = core.DEADLINE_STOP if kind == "deadline" else core.ENVIRONMENT_STOP
        return core.ControlInput(
            control_id=core.ControlId(root=identity), action="stop", mode=core.StopMode.CANCEL
        )
    return _control(kind, index)


@pytest.mark.xfail(strict=True, reason="B1: a resume lifts the host's own stops")
@given(st.lists(st.sampled_from(_HOST_KINDS), min_size=1, max_size=10))
@example(["environment", "resume"])  # the reported case: a resume after the probe's stop
@example(["deadline", "resume"])
@example(["drain", "environment", "resume"])
def test_no_resume_lifts_a_stop_the_host_delivered_while_the_run_runs(
    kinds: list[_HostKind],
) -> None:
    """Regression (review B1 of #3327): a resume lifted the deadline's and the environment's stop.

    A process that died before the run closed was resumed; the resume lifted the host's stop,
    and the stop came back under the id core already held, so core took it as recorded and the
    run went on with no stop standing. The host's stops are verdicts, not pauses: while one
    stands core refuses a resume, so the stop stands to the run's end.
    """
    state = core.initial_state()
    final = False
    for index, kind in enumerate(kinds):
        control = _host_control(kind, index)
        refused = core.control_refusal(state, control)
        if kind == "resume" and final:
            assert refused is not None
            continue
        if kind == "resume" and core.requested_stop_mode(state.run) is None:
            continue  # a resume with no stop standing is a plain resume
        assert refused is None
        state = core.step(state, core.RunControlEvent(control=control, now_at=float(index))).state
        final = final or kind in {"deadline", "environment"}
        if kind == "resume":
            assert core.requested_stop_mode(state.run) is None
    assert (core.requested_stop_mode(state.run) is not None) or not final
