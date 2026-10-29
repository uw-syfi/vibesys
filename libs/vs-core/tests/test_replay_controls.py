"""Control classification and its fold are pure, total and never apply a stale control."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof

from vs_core.api.replay import (
    ControlView,
    PermitRefusal,
    ReplayControls,
    ReplayError,
    applied_control,
    classify_control,
    permit_refusal,
    stale_control,
)
from vs_proto.api import replay

BOUNDS = {
    "a": replay.ReplayTurnBound(origin="a", deadline=10, hold_credit_seconds=1),
    "b": replay.ReplayTurnBound(origin="b", deadline=50, hold_credit_seconds=0),
}
PERMITS = tuple(replay.ReplayTurnPermit(epoch=1, turn=t, bound=b) for t, b in BOUNDS.items())
VIEW = ControlView(
    epoch=1, open=True, permits=PERMITS, unfinished=("child-a",), live_turns=frozenset(BOUNDS)
)

actions = st.sampled_from(["pause", "pause_now", "steer"])
turns = st.sampled_from(["", "a", "b", "ended"])


def control(action: str, epoch: int, turn: str, key: str) -> replay.ReplayControl:
    match action:
        case "pause":
            value = Oneof("pause", replay.ReplayPause())
        case "pause_now":
            value = Oneof("pause_now", replay.ReplayPauseNow())
        case _:
            value = Oneof("steer", replay.ReplaySteer(input_id=f"in-{key}", turn=turn or None))
    return replay.ReplayControl(request_id=key, epoch=epoch, action=value)


@pytest.mark.regression
@example(controls=[("pause_now", 2, ""), ("pause", 1, "")], is_open=True)
@given(
    controls=st.lists(st.tuples(actions, st.integers(min_value=1, max_value=3), turns), max_size=6),
    is_open=st.booleans(),
)
def test_controls_fold_applies_only_current_controls(
    controls: list[tuple[str, int, str]], *, is_open: bool
) -> None:
    view = ControlView(
        epoch=1,
        open=is_open,
        permits=PERMITS,
        unfinished=("child-a",),
        live_turns=frozenset(BOUNDS),
    )
    state = ReplayControls()
    draining: frozenset[str] | None = None
    halted = False
    guidance: list[str] = []
    for index, (action, epoch, turn) in enumerate(controls):
        item = control(action, epoch, turn, str(index))
        disposition = classify_control(item, view)
        assert disposition.control == item
        stale = epoch != 1 or not is_open or (action == "steer" and turn == "ended")
        assert (stale_control(disposition) is not None) == stale
        assert (applied_control(disposition) is None) == stale
        state = state.record(disposition, view)
        with pytest.raises(ReplayError, match="disposed of once"):
            state.record(disposition, view)
        if stale:
            continue
        if action == "pause":
            draining = frozenset(BOUNDS) if draining is None else draining
        elif action == "pause_now":
            halted = True
        else:
            guidance.append(f"in-{index}")
    assert state.draining == draining
    assert state.halted == halted
    assert state.admits(None) == (not halted and draining is None)
    assert state.admits("a") == (not halted)
    expected = tuple(
        key
        for index, (action, epoch, turn) in enumerate(controls)
        if (key := f"in-{index}") in guidance and turn in {"", "a"}
    )
    assert state.guidance("a") == expected
    fresh = state.opened()
    assert fresh.draining is None
    assert not fresh.halted
    assert fresh.guidance("a") == expected


@pytest.mark.parametrize("outcome", ["applied", "stale"])
def test_a_recorded_disposition_must_be_the_one_the_prefix_classifies(outcome: str) -> None:
    """``outcome`` is the true disposition; the recorded one claims the other."""
    item = control("pause", 1 if outcome == "applied" else 2, "", "key")
    assert (applied_control(classify_control(item, VIEW)) is not None) == (outcome == "applied")
    forged = replay.ReplayControlDisposition(
        control=item,
        outcome=Oneof("stale", replay.ReplayControlStale.EPOCH)
        if outcome == "applied"
        else Oneof("applied", replay.ReplayControlApplied(draining=list(PERMITS))),
    )
    with pytest.raises(ReplayError, match="contradicts the committed prefix"):
        ReplayControls().record(forged, VIEW)


def test_a_start_consumes_exactly_its_pending_guidance_and_respects_the_drain() -> None:
    state = ReplayControls()
    for key, turn in (("one", "a"), ("two", ""), ("three", "b")):
        state = state.record(classify_control(control("steer", 1, turn, key), VIEW), VIEW)
    assert state.guidance("a") == ("in-one", "in-two")
    with pytest.raises(ReplayError, match="consumes exactly"):
        state.start("a", ("in-two",))
    state = state.start("a", ("in-one", "in-two"))
    assert state.guidance("a") == ()
    assert state.guidance("b") == ("in-three",)
    paused = state.record(classify_control(control("pause", 1, "", "pause"), VIEW), VIEW)
    with pytest.raises(ReplayError, match="admits only continuations"):
        paused.start("fresh", ())
    paused.start("b", ("in-three",))


@given(
    epoch=st.integers(min_value=1, max_value=3),
    turn=st.sampled_from(["a", "b"]),
    deadline=st.sampled_from([10.0, 50.0]),
)
def test_permit_refusal_names_the_first_difference(epoch: int, turn: str, deadline: float) -> None:
    admitted = PERMITS[0]
    bound = replay.ReplayTurnBound(origin="a", deadline=deadline, hold_credit_seconds=1)
    permit = replay.ReplayTurnPermit(epoch=epoch, turn=turn, bound=bound)
    refusal = permit_refusal(permit, admitted, epoch=1)
    if epoch != 1:
        assert refusal is PermitRefusal.STALE_EPOCH
    elif turn != "a":
        assert refusal is PermitRefusal.OTHER_TURN
    elif deadline != 10:
        assert refusal is PermitRefusal.OTHER_BOUND
    else:
        assert refusal is None
