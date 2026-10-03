"""The steer outbox's rules for any sequence of steers, deliveries, and settlements."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._support import portfolio

from vibesys.orchestration.dynamic import DynamicState, WorkstreamPlan
from vibesys.orchestration.dynamic.models import (
    AgentLoopState,
    DynamicWorkstream,
    WorkstreamPhase,
)
from vibesys.orchestration.dynamic.steers import (
    MAX_PENDING,
    MAX_TEXT_CHARS,
    SteerAccepted,
    SteerOutsideAgentModeError,
    SteerRefusal,
    SteerRefused,
    drop_pending,
    enqueue,
    mark_delivered,
    pending,
)

_IDS = ("alpha", "beta")


def _state(*, agent: bool = True) -> DynamicState:
    plans = portfolio(*_IDS)["workstreams"]
    assert isinstance(plans, list)
    return DynamicState(
        agent=AgentLoopState() if agent else None,
        workstreams=[
            DynamicWorkstream(
                hypothesis_id=plan["hypothesis_id"],
                sequence=sequence,
                planning_call=1,
                plan=WorkstreamPlan.model_validate(plan),
                parent_revision="base",
            )
            for sequence, plan in enumerate(plans, start=1)
        ],
    )


@dataclass(frozen=True)
class _Enqueue:
    target: str
    text: str
    interrupt: bool


@dataclass(frozen=True)
class _Deliver:
    target: str
    invocation: str


@dataclass(frozen=True)
class _Settle:
    target: str


_TARGETS = st.sampled_from((*_IDS, "unknown"))
_TEXTS = st.one_of(
    st.text(max_size=12),
    st.integers(MAX_TEXT_CHARS - 1, MAX_TEXT_CHARS + 1).map(lambda size: "x" * size),
)
_OPERATIONS = st.lists(
    st.one_of(
        st.builds(_Enqueue, _TARGETS, _TEXTS, st.booleans()),
        st.builds(_Deliver, _TARGETS, st.sampled_from(("turn-1", "turn-2", "turn-3"))),
        st.builds(_Settle, _TARGETS),
    ),
    max_size=30,
)


@settings(max_examples=300, deadline=None)
@given(operations=_OPERATIONS)
def test_any_sequence_keeps_the_outbox_invariants(
    operations: list[_Enqueue | _Deliver | _Settle],
) -> None:
    state = _state()
    settled: set[str] = set()
    # Every note's first settled form (delivered or dropped); it never changes.
    first_settled: dict[tuple[str, int], tuple[str | None, str | None]] = {}
    drops = 0
    for clock, operation in enumerate(operations):
        match operation:
            case _Enqueue(target, text, interrupt):
                before = len(pending(state, target))
                result = enqueue(state, target, text, at_s=float(clock), interrupt=interrupt)
                expected = (
                    SteerRefusal.TEXT_LENGTH
                    if not 1 <= len(text) <= MAX_TEXT_CHARS
                    else SteerRefusal.UNKNOWN_WORKSTREAM
                    if target not in _IDS
                    else SteerRefusal.WORKSTREAM_SETTLED
                    if target in settled
                    else SteerRefusal.RATE_LIMITED
                    if before >= MAX_PENDING
                    else None
                )
                if expected is None:
                    assert isinstance(result, SteerAccepted)
                    assert pending(state, target)[-1] == result.note
                else:
                    assert result == SteerRefused(expected)
            case _Deliver(target, invocation):
                fresh = pending(state, target)
                rendered = mark_delivered(state, target, invocation)
                assert all(note.delivered_to == invocation for note in rendered)
                assert set(fresh) <= {
                    note.model_copy(update={"delivered_to": None}) for note in rendered
                }
                assert pending(state, target) == ()
            case _Settle(target):
                fresh = pending(state, target)
                dropped = drop_pending(state, target, at_s=float(clock))
                assert len(dropped) == len(fresh)
                drops += len(dropped)
                settled.add(target)
                state.workstreams = [
                    item.model_copy(update={"phase": WorkstreamPhase.CANCELLED})
                    if item.hypothesis_id == target
                    else item
                    for item in state.workstreams
                ]
        assert state.agent is not None
        for target, notes in state.agent.steers.items():
            assert len(pending(state, target)) <= MAX_PENDING
            for position, note in enumerate(notes):
                assert note.delivered_to is None or note.dropped is None
                outcome = (note.delivered_to, note.dropped)
                if outcome != (None, None):
                    assert first_settled.setdefault((target, position), outcome) == outcome
    assert state.agent is not None
    journaled = [entry for entry in state.agent.journal if entry.kind == "steer"]
    assert len(journaled) == drops


def test_a_dropped_note_is_journaled_with_its_identity_and_time() -> None:
    state = _state()
    accepted = enqueue(state, "alpha", "Profile decode first.", at_s=958.0, interrupt=False)
    assert isinstance(accepted, SteerAccepted)

    dropped = drop_pending(state, "alpha", at_s=1200.0)

    assert [note.dropped for note in dropped] == ["workstream_settled"]
    assert state.agent is not None
    [entry] = state.agent.journal
    assert (entry.kind, entry.subject, entry.at_s) == ("steer", "alpha", 1200.0)
    assert accepted.note.note_sha256[:12] in entry.text
    assert "15:58" in entry.text


def test_planner_mode_has_no_steers() -> None:
    state = _state(agent=False)

    with pytest.raises(SteerOutsideAgentModeError):
        enqueue(state, "alpha", "note", at_s=0.0, interrupt=False)
    assert pending(state, "alpha") == ()
    assert mark_delivered(state, "alpha", "turn-1") == ()
    assert drop_pending(state, "alpha", at_s=0.0) == ()
    assert state.agent is None
