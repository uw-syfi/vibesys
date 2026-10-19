"""Core takes an explicit resume as the lift of a standing stop, until the run's ending commits.

A recorded stop stands until a resume follows it. While it stands the run takes no pause and no
wait for capacity, a stop only escalates (a cancel over a drain), and an immediate resume lifts
it: the stop mode reads none again, and core publishes ``StopLiftRecorded`` after the
``ControlChanged`` of that control. A run whose ending has committed (it is closing) takes no
resume, so the stop is final from there.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

type _Kind = Literal["drain", "cancel", "pause", "resume", "capacity"]
_KINDS: tuple[_Kind, ...] = ("drain", "cancel", "pause", "resume", "capacity")


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


def test_a_resume_after_the_ending_committed_is_refused_and_the_stop_stays() -> None:
    """A stop that carries its result closes the run: nothing lifts it from there."""
    state = core.initial_state()
    proposal = core.RunResultProposal(outcome="cancelled", reason="operator stop")
    stop = core.RunControlEvent(control=_control("drain", 0), now_at=0.0, result=proposal)
    state = core.step(state, stop).state
    assert state.run.status in {core.RunStatus.CLOSING, core.RunStatus.TERMINAL}
    resume = _control("resume", 1)
    assert core.control_refusal(state, resume) is not None
    assert core.requested_stop_mode(state.run) is core.StopMode.DRAIN
