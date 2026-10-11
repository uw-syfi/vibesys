"""The protocol rules of a turn chain and an attempt track hold for every strategy."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptId,
    AttemptTrack,
    ContinuationId,
    DecisionId,
    IntentBlocked,
    InvocationId,
    InvocationRef,
    RequestId,
    RunId,
    Scope,
    SessionId,
    Step,
    TurnChain,
    Value,
    blocked_track,
)


class Note(Value):
    text: str


class Chain(TurnChain[Note]):
    """A strategy's own chain: extra fields survive every transition."""

    role: str = "implementer"


class Track(AttemptTrack[Chain]):
    sequence: int = 1


def _ref(name: str) -> InvocationRef:
    return InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root=name), generation=0
    )


_COUNTS = st.integers(min_value=0, max_value=6)


@given(corrections=_COUNTS, limit=_COUNTS)
def test_a_correction_names_the_invalid_turn_and_stops_at_its_limit(
    corrections: int, limit: int
) -> None:
    chain = Chain(serial=4, corrections=corrections, drops=2, role="judge")
    corrected = chain.corrected(_ref("bad"), Note(text="fix"), limit=limit)
    if corrections >= limit:
        assert corrected is None
        return
    assert corrected is not None
    assert (corrected.serial, corrected.corrections) == (5, corrections + 1)
    assert (corrected.charge, corrected.invocation) == ("correction", _ref("bad"))
    assert corrected.context == Note(text="fix")
    assert corrected.role == "judge"
    assert (corrected.drops, corrected.prompts, corrected.yielded) == (0, (), False)


@given(drops=_COUNTS, limit=_COUNTS, now=st.floats(0, 1e6), backoff=st.floats(0, 1e3))
def test_a_lost_turn_is_asked_again_after_the_backoff_and_never_past_its_limit(
    drops: int, limit: int, now: float, backoff: float
) -> None:
    chain = Chain(serial=2, drops=drops, charge="paid", context=Note(text="q"))
    again = chain.asked_again(_ref("lost"), now=now, limit=limit, backoff=backoff)
    if drops >= limit:
        assert again is None
        return
    assert again is not None
    assert (again.serial, again.drops, again.charge) == (3, drops + 1, "correction")
    assert (again.invocation, again.ask_not_before) == (_ref("lost"), now + backoff)
    assert again.context == Note(text="q")
    assert again.corrections == chain.corrections


@given(drops=_COUNTS, limit=st.integers(1, 6))
def test_a_resume_is_never_asked_again(drops: int, limit: int) -> None:
    resumed = Chain(charge="resume", drops=min(drops, limit - 1))
    assert resumed.asked_again(_ref("lost"), now=0.0, limit=limit, backoff=1.0) is None


def test_a_resume_names_its_continuation_and_the_authorized_invocation() -> None:
    suspended = Chain(serial=1).suspended(_ref("asked"), ContinuationId(root="cont"))
    assert suspended.yielded
    resumed = suspended.resumed(
        authorized=InvocationId(root="next"),
        continuation=ContinuationId(root="cont"),
        context=Note(text="resume"),
    )
    assert (resumed.charge, resumed.serial, resumed.yielded) == ("resume", 2, False)
    assert resumed.resume_as == InvocationId(root="next")
    assert resumed.continuation == ContinuationId(root="cont")
    assert resumed.invocation == _ref("asked")


@given(admitted=st.booleans(), step=st.sampled_from(list(Step)))
def test_a_failure_is_kept_once_and_an_admitted_attempt_returns_to_settle(
    *, admitted: bool, step: Step
) -> None:
    track = Track(attempt=AttemptId(root="a"), step=step, ready=admitted)
    track = track.model_copy(update={"awaiting": DecisionId(root="d")})
    failed = track.failed("first")
    assert failed.failed("second").failure == "first"
    assert failed.awaiting is None
    assert failed.owes_settlement is admitted
    assert failed.step is (Step.NEEDED if admitted else step)


def test_a_held_reply_is_released_with_the_phase_ready_to_continue() -> None:
    track = Track(attempt=AttemptId(root="a"), step=Step.AWAITING)
    held = track.holding('{"x": 1}', _ref("turn"))
    assert held.holds_reply
    assert (held.step, held.awaiting) == (Step.RETAINING, None)
    released = held.released()
    assert not released.holds_reply
    assert (released.step, released.held_reply, released.held_invocation) == (
        Step.NEEDED,
        None,
        None,
    )


def test_a_track_awaits_exactly_its_decision() -> None:
    track = Track(attempt=AttemptId(root="a"), awaiting=DecisionId(root="d"))
    assert track.awaits("d")
    assert not track.awaits("other")
    assert not track.awaits(None)


def _blocked(root: str, owner: AttemptId | RunId, generation: int) -> IntentBlocked:
    return IntentBlocked(
        request_id=RequestId(root="block"),
        target=RequestId(root=root),
        scope=Scope(owner=owner, generation=generation),
        diagnostic="blocked",
    )


@given(step=st.sampled_from(Step), generation=st.integers(0, 3), asked=st.integers(0, 3))
def test_a_blocked_request_of_an_attempt_belongs_to_it_only_while_one_is_in_flight(
    step: Step, generation: int, asked: int
) -> None:
    attempt = AttemptId(root="a1")
    track = Track(attempt=attempt, generation=generation, step=step)
    event = _blocked("turn-request", attempt, asked)
    in_flight = step in (Step.RENDERING, Step.AWAITING)
    assert step.has_request_in_flight == in_flight
    assert track.owns_blocked(event) == (in_flight and asked == generation)
    assert blocked_track([track], event) == (track if track.owns_blocked(event) else None)


@given(step=st.sampled_from(Step))
def test_a_blocked_request_of_another_attempt_or_a_run_belongs_to_no_track(step: Step) -> None:
    track = Track(attempt=AttemptId(root="a1"), step=step)
    assert not track.owns_blocked(_blocked("turn-request", AttemptId(root="a2"), 0))
    assert not track.owns_blocked(_blocked("turn-request", RunId(root="run"), 0))


@given(step=st.sampled_from(Step))
def test_a_blocked_operation_belongs_to_the_track_that_awaits_its_decision(step: Step) -> None:
    waiting = Track(attempt=AttemptId(root="a1"), step=step, awaiting=DecisionId(root="render-1"))
    other = Track(attempt=AttemptId(root="a2"), step=Step.AWAITING)
    event = _blocked("operation:render-1", RunId(root="run"), 0)
    assert blocked_track([other, waiting], event) == waiting
    assert blocked_track([other], event) is None
