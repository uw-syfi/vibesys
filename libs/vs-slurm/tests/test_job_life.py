"""The Slurm lifecycle translator: forward only, total, and never guessing."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_slurm.api import (
    INITIAL_LIFE,
    AccountingRow,
    Completing,
    Ended,
    EndOutcome,
    JobLife,
    NoAccountingRow,
    NotInQueue,
    Queued,
    QueueRow,
    Running,
    SlurmJobStatus,
    SlurmPhase,
    SlurmRawState,
    SlurmStatus,
    Unknown,
    advance,
    needs_accounting,
    observe,
    parse_accounting_row,
    parse_queue_row,
    phase_of,
    status_of,
)

_DOCUMENTED = [state.value for state in SlurmRawState]
_STATE_TEXT = st.one_of(
    st.sampled_from(_DOCUMENTED),
    st.sampled_from(["SOMETHING_NEW", "OUT_OF_ME+", "running", "", "COMPLETED+"]),
    st.text(max_size=12),
)
_EXIT_TEXT = st.one_of(
    st.sampled_from(["0:0", "1:0", "0:9", "137:9", "garbage", "0", ""]), st.text(max_size=6)
)
_ATTEMPT = st.integers(0, 2)
_RAW: st.SearchStrategy[SlurmStatus] = st.one_of(
    st.builds(QueueRow, _STATE_TEXT, attempt=_ATTEMPT),
    st.builds(AccountingRow, _STATE_TEXT, _EXIT_TEXT, attempt=_ATTEMPT),
    st.just(NotInQueue()),
    st.just(NoAccountingRow()),
)
_RANK = {"unknown": -1, "queued": 0, "running": 1, "completing": 2, "ended": 3}


def _order(life: JobLife) -> tuple[int, int]:
    return (life.attempt, _RANK[life.kind])


@given(raws=st.lists(_RAW, max_size=30))
def test_observe_is_monotone_over_any_raw_sequence(raws: list[SlurmStatus]) -> None:
    """The held life never moves to an earlier (attempt, phase) and never leaves Ended."""
    held = INITIAL_LIFE
    for raw in raws:
        after = observe(held, raw)
        if isinstance(held, Unknown):
            assert _order(after) >= _order(held)
        else:
            assert not isinstance(after, Unknown)
            assert _order(after) >= _order(held)
        if isinstance(held, Ended):
            assert after == held
        held = after


@given(raws=st.lists(_RAW, max_size=30))
def test_an_unrecognised_state_never_replaces_a_known_life(raws: list[SlurmStatus]) -> None:
    held = INITIAL_LIFE
    for raw in raws:
        after = observe(held, raw)
        if not isinstance(held, Unknown):
            assert not isinstance(after, Unknown)
        held = after


@given(prev=st.sampled_from([INITIAL_LIFE, Queued(), Running(attempt=1), Completing()]))
def test_absence_from_the_queue_says_nothing(prev: JobLife) -> None:
    """Not listed in squeue is not an outcome: it leaves the life alone and asks sacct."""
    assert observe(prev, NotInQueue()) == prev
    assert observe(prev, NoAccountingRow()) == prev
    assert needs_accounting(observe(INITIAL_LIFE, NotInQueue()))


@given(raw=_RAW, prev=st.sampled_from([INITIAL_LIFE, Queued(), Running(), Completing()]))
def test_every_raw_reading_maps_to_a_defined_life(raw: SlurmStatus, prev: JobLife) -> None:
    life = observe(prev, raw)
    assert phase_of(life) in set(SlurmPhase)
    assert status_of(life) in set(SlurmJobStatus)
    assert (phase_of(life) is SlurmPhase.UNKNOWN) == (status_of(life) is SlurmJobStatus.UNKNOWN)


@given(text=_STATE_TEXT, row=st.sampled_from([QueueRow, AccountingRow]))
def test_only_documented_states_are_known(text: str, row: type) -> None:
    raw = QueueRow(text) if row is QueueRow else AccountingRow(text, "0:0")
    life = observe(INITIAL_LIFE, raw)
    if text in _DOCUMENTED:
        assert not isinstance(life, Unknown)
    else:
        assert life == Unknown(raw=text)


@given(first=st.sampled_from(_DOCUMENTED), second=st.sampled_from(_DOCUMENTED))
def test_nothing_follows_an_end(first: str, second: str) -> None:
    ended = observe(INITIAL_LIFE, AccountingRow(first, "0:0"))
    if isinstance(ended, Ended):
        assert observe(ended, QueueRow(second)) == ended
        assert observe(ended, AccountingRow(second, "0:0", attempt=5)) == ended


def test_running_after_ended_is_ignored() -> None:
    ended = observe(INITIAL_LIFE, AccountingRow("COMPLETED", "0:0"))
    assert observe(ended, QueueRow("RUNNING")) == ended


def test_a_requeue_restarts_the_order_only_before_the_end() -> None:
    running = Running()
    assert advance(running, Queued(attempt=1)) == Queued(attempt=1)
    assert advance(Queued(attempt=1), Running(attempt=0)) == Queued(attempt=1)


@given(state=st.sampled_from([SlurmRawState.PREEMPTED, SlurmRawState.REVOKED]))
def test_a_scheduler_ended_job_is_not_read_as_cancelled(state: SlurmRawState) -> None:
    """Regression: PREEMPTED and REVOKED read as CANCELLED, claiming a stop nobody asked for."""
    life = observe(INITIAL_LIFE, AccountingRow(state.value, "0:0"))
    assert isinstance(life, Ended)
    assert life.outcome in {EndOutcome.PREEMPTED, EndOutcome.REVOKED}
    assert status_of(life) is SlurmJobStatus.FAILED


@given(code=_EXIT_TEXT)
def test_a_completed_job_with_an_unreadable_exit_code_is_not_a_success(code: str) -> None:
    life = observe(INITIAL_LIFE, AccountingRow("COMPLETED", code))
    clean = code == "0:0"
    assert (status_of(life) is SlurmJobStatus.COMPLETED) == clean


def test_queue_and_accounting_rows_parse_to_typed_readings() -> None:
    assert parse_queue_row("") == NotInQueue()
    assert parse_queue_row("PENDING|Resources|2026-10-04T06:00:00\n") == QueueRow(
        "PENDING", "Resources", "2026-10-04T06:00:00"
    )
    assert parse_queue_row("PENDING|None|N/A") == QueueRow("PENDING", "None", None)
    assert parse_accounting_row("CANCELLED by 1000|0:15\n") == AccountingRow("CANCELLED", "0:15")
    assert parse_accounting_row("garbage\n") == NoAccountingRow()
