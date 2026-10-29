"""The registry record's ``end`` and ``paused``: closed values, one-line reasons, and their rules."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from server.host_api import (
    END_REASON_MAX_CHARACTERS,
    EndRecord,
    InstanceStatus,
    LiveInstanceRecord,
    PauseCause,
    RunEndOutcome,
    RunPhase,
    RunStatus,
    one_line,
)
from server.registry import CRASHED, CRASHED_END, FakeInstanceStore, LiveRegistry
from vs_core.api import EndReason
from vs_sim.api.testing import ManualClock

INSTANCE = "0123456789ab"


def _record(phase: RunPhase, **fields: object) -> LiveInstanceRecord:
    return LiveInstanceRecord.model_validate(
        {
            "id": INSTANCE,
            "status": InstanceStatus.SERVING,
            "phase": phase,
            "socket_path": "/run/s.sock",
            "log_path": "/run/server.log",
            "project_root": "/project",
            "run_id": "run-1",
            "pid": 4242,
            "started_at": 1.0,
            "hostname": "node",
            "vibesys_version": "0+test",
            **fields,
        }
    )


def test_every_core_end_reason_is_an_outcome_and_only_the_shells_force_stop_is_added() -> None:
    outcomes = {outcome.value for outcome in RunEndOutcome}
    assert outcomes - {reason.value for reason in EndReason} == {"force_stopped"}
    assert {reason.value for reason in EndReason} <= outcomes


@given(st.text())
def test_a_reason_is_one_bounded_line_of_the_text_or_nothing(text: str) -> None:
    line = one_line(text)
    if not text.strip():
        assert line is None
        return
    assert line is not None
    assert 0 < len(line) <= END_REASON_MAX_CHARACTERS
    assert not any(break_ in line for break_ in "\n\r")
    first = next(each.strip() for each in text.splitlines() if each.strip())
    assert first.startswith(line.removesuffix("…"))


def test_an_end_names_its_resume_round_only_when_it_can_resume() -> None:
    with pytest.raises(ValidationError, match="resume_from_round"):
        EndRecord(outcome=RunEndOutcome.FAILED, reason="boom", resumable=False, resume_from_round=2)


@pytest.mark.parametrize("reason", ["", "two\nlines", "x" * (END_REASON_MAX_CHARACTERS + 1)])
def test_an_end_reason_is_one_non_empty_bounded_line(reason: str) -> None:
    with pytest.raises(ValidationError, match="reason"):
        EndRecord(outcome=RunEndOutcome.FAILED, reason=reason, resumable=False)


def test_an_end_rejects_an_unknown_key_by_name() -> None:
    with pytest.raises(ValidationError, match="archived"):
        EndRecord.model_validate(
            {"outcome": "stopped", "reason": "Stopped.", "resumable": True, "archived": True}
        )


@pytest.mark.parametrize(
    "status", [status for status in RunStatus if status is not RunStatus.PAUSED]
)
def test_only_a_paused_run_names_who_paused_it(status: RunStatus) -> None:
    phase = (
        RunPhase(status=status, stage="resolving")
        if status is RunStatus.STARTING
        else RunPhase(status=status, reason=CRASHED)
        if status is RunStatus.FAILED
        else RunPhase(status=status)
    )
    with pytest.raises(ValidationError, match="paused"):
        _record(phase, paused=PauseCause.USER)


def test_only_an_ended_run_has_an_end() -> None:
    with pytest.raises(ValidationError, match="end is set only"):
        _record(RunPhase(status=RunStatus.RUNNING), end=CRASHED_END)


def test_a_crashed_server_is_buried_with_a_resumable_failed_end_and_no_pause() -> None:
    store = FakeInstanceStore()
    hold = store.hold(INSTANCE)
    hold.publish(_record(RunPhase(status=RunStatus.PAUSED), paused=PauseCause.QUOTA))
    hold.crash()

    (buried,) = LiveRegistry(store, ManualClock(100.0)).list().ended

    assert buried.paused is None
    assert buried.end == EndRecord(
        outcome=RunEndOutcome.FAILED, reason=CRASHED.message, resumable=True
    )
