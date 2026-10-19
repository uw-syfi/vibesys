"""A turn waiting out a provider quota stop does not spend its limit.

A turn that stopped on a quota waits on the shell's side of its dispatch while the run is
held by a quota pause, until the policy's timed resume. Core's limit on the dispatch counts
the run clock, so a wait longer than what was left of the limit ended the turn as lost in the
middle of the wait, cancelled it, and left the run paused with nothing to resume it.

Core learns of the wait only as the run's quota pause. While one holds the run, a dispatched
turn's limit moves by the time that passes, exactly as host downtime moves it, and the limit
counts again from the resume.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_turn_limit_from_dispatch import _RUN_DEADLINE, _prepared, _reserved

_LIMIT = st.integers(min_value=10, max_value=1000).map(float)


def _dispatched(limit: float) -> tuple[core.CoreState, core.RequestId]:
    """A turn dispatched at time zero with ``limit``: core ends it at ``limit``, not before."""
    state = _reserved(limit)
    record = _prepared(state, core.DispatchTurn)
    started = record.model_copy(
        update={"phase": core.RequestPhase.DISPATCHED, "until": core.Until(at=limit)}
    )
    held = state.records.model_copy(update={"records": (started,)})
    return state.model_copy(update={"records": held}), record.request_id


def _control(
    state: core.CoreState,
    name: str,
    action: Literal["pause", "resume"],
    at: float,
    owner: core.PauseOwner | None = None,
) -> core.CoreState:
    when: Literal["now"] | None = "now" if action == "resume" else None
    control = core.ControlInput(
        control_id=core.ControlId(root=name), action=action, owner=owner, when=when
    )
    return core.step(state, core.RunControlEvent(control=control, now_at=at)).state


def _record(state: core.CoreState, request_id: core.RequestId) -> core.RequestRecord:
    return next(row for row in state.records.records if row.request_id == request_id)


def _ticked(state: core.CoreState, times: list[float]) -> core.CoreState:
    for at in times:
        state = core.step(state, core.ClockAdvanced(now_at=at)).state
    return state


def _blocked(state: core.CoreState, request_id: core.RequestId) -> bool:
    return _record(state, request_id).phase == core.RequestPhase.BLOCKED


@given(
    limit=_LIMIT,
    paused_at=st.integers(min_value=1, max_value=9).map(float),
    waits=st.lists(st.integers(min_value=1, max_value=5000).map(float), min_size=1, max_size=6),
)
def test_a_quota_pause_of_any_length_leaves_a_dispatched_turn_the_time_it_had(
    limit: float, paused_at: float, waits: list[float]
) -> None:
    """However long the quota pause, the turn ends ``limit`` after dispatch plus the pause.

    The waits run past the limit by far (a reset hours away against a limit of minutes), and
    the clock is read at each step of them.
    """
    state, request_id = _dispatched(limit)
    state = _ticked(state, [paused_at / 2])
    state = _control(state, "quota", "pause", paused_at, core.PauseOwner.QUOTA)

    now = paused_at
    for wait in waits:
        now += wait
        state = _ticked(state, [now])
        assert not _blocked(state, request_id), "the turn ended inside the quota wait"
    waited = now - paused_at

    state = _control(state, "capacity", "resume", now)
    assert not _blocked(state, request_id)
    assert not _blocked(_ticked(state, [limit + waited - 1.0]), request_id)
    assert _blocked(_ticked(state, [limit + waited]), request_id)


@given(limit=_LIMIT, wait=st.integers(min_value=1, max_value=5000).map(float))
def test_an_operator_pause_does_not_move_a_turns_limit(limit: float, wait: float) -> None:
    """Only a quota stop is a wait for capacity; a turn the operator's pause lets run is timed."""
    state, request_id = _dispatched(limit)
    state = _control(state, "hold", "pause", 1.0, core.PauseOwner.OPERATOR)
    state = _ticked(state, [1.0 + wait])

    assert _blocked(state, request_id) == (1.0 + wait >= limit)


@given(limit=_LIMIT)
def test_a_quota_pause_does_not_carry_a_limit_past_the_run_deadline(limit: float) -> None:
    """A pause that never resumes still ends with the run."""
    state, request_id = _dispatched(limit)
    state = _control(state, "quota", "pause", 1.0, core.PauseOwner.QUOTA)

    state = _ticked(state, [_RUN_DEADLINE - 1.0])
    assert _record(state, request_id).until.at <= _RUN_DEADLINE
