"""The confirmed log appends core records one at a time and treats the log as the only truth."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    EpochStarted,
    InterruptionReason,
    Oneof,
    Record,
    RunEnded,
    StepCancelReason,
    StepFinished,
    StepStarted,
    Timestamp,
    ValidationError,
)
from vs_runlog.api import (
    Appended,
    ConfirmedLog,
    CoreMessage,
    FileRunLogWriter,
    Uncertain,
    open_run_log,
)
from vs_sim.api.testing import ManualClock, SimThreads, examples

_TAG = st.text("abc", min_size=1, max_size=3)
_BYTES = st.binary(min_size=1, max_size=8)
_ATTEMPT = st.integers(1, 5)
_CANCELLED = [reason for reason in StepCancelReason if reason is not StepCancelReason.UNSPECIFIED]
_INTERRUPTIONS = [
    reason for reason in InterruptionReason if reason is not InterruptionReason.UNSPECIFIED
]
_OUTCOME = st.one_of(
    _BYTES.map(lambda body: Oneof("finished", body)),
    _BYTES.map(lambda body: Oneof("failed", body)),
    st.sampled_from(_CANCELLED).map(lambda reason: Oneof("cancelled", reason)),
)
_RESULT = st.builds(
    StepFinished,
    tag=_TAG,
    attempt=st.integers(0, 5),
    schema_version=st.just(1),
    result=_OUTCOME,
)
MESSAGES: st.SearchStrategy[CoreMessage] = st.one_of(
    st.builds(EpochStarted, epoch=st.integers(1, 3)),
    st.builds(RunEnded, schema_version=st.just(1), result=_BYTES),
    st.builds(CoreInput, schema_version=st.just(1), event=_BYTES),
    st.builds(
        StepStarted,
        tag=_TAG,
        attempt=_ATTEMPT,
        schema_version=st.just(1),
        request=_BYTES,
        deadline_at=st.floats(0, 1e6),
    ),
    _RESULT,
    st.builds(
        AttemptInterrupted, tag=_TAG, attempt=_ATTEMPT, reason=st.sampled_from(_INTERRUPTIONS)
    ),
)
STAMPS = st.tuples(st.integers(1, 3), st.floats(0, 1e6))


def stamp(
    message: CoreMessage, log: ConfirmedLog, epoch: int, clock: float
) -> Appended | Uncertain:
    return log.append(message, epoch=epoch, at=Timestamp(seconds=1), clock=clock)


@settings(max_examples=examples(pr=8, nightly=100))
@given(messages=st.lists(st.tuples(MESSAGES, STAMPS), max_size=8))
def test_what_is_appended_is_what_a_reopened_log_reads(
    messages: list[tuple[CoreMessage, tuple[int, float]]],
) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            log = ConfirmedLog(writer)
            for message, (epoch, clock) in messages:
                assert isinstance(stamp(message, log, epoch, clock), Appended)
            written = log.records()
        with open_run_log(path) as reader:
            assert tuple(reader) == written
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as reopened:
            assert ConfirmedLog(reopened).records() == written
        assert [record.seq for record in written] == list(range(1, len(messages) + 1))


@settings(max_examples=examples(pr=8, nightly=100))
@given(first=MESSAGES, skipped=st.integers(0, 4).filter(lambda seq: seq != 2))
def test_only_the_record_that_follows_the_confirmed_prefix_is_appended(
    first: CoreMessage, skipped: int
) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            log = ConfirmedLog(writer)
            stamp(first, log, 1, 0.0)
            before = log.records()
            stray = Record(
                seq=skipped, epoch=1, at=Timestamp(seconds=1), clock=0.0, body=before[0].body
            )
            with pytest.raises(ValueError, match="does not follow"):
                log.extend(stray)
            assert log.records() == before


@settings(max_examples=examples(pr=8, nightly=100))
@given(message=MESSAGES)
def test_a_record_that_breaks_its_validation_rules_is_refused_and_leaves_the_log_unchanged(
    message: CoreMessage,
) -> None:
    broken = StepStarted(tag="a", attempt=0, schema_version=1, request=b"x", deadline_at=0.0)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            log = ConfirmedLog(writer)
            stamp(message, log, 1, 0.0)
            before = log.records()
            with pytest.raises(ValidationError):
                stamp(broken, log, 1, 0.0)
            assert log.records() == before


class FlakyRecords:
    """An in-memory run.log whose next commit can fail after the record reached the file."""

    def __init__(self) -> None:
        self.durable: list[Record] = []
        self.fail_next_commit = False
        self._written: list[Record] = []

    def append(self, record: Record) -> None:
        self._written.append(record)

    def commit(self) -> None:
        self.durable.extend(self._written)
        self._written = []
        if self.fail_next_commit:
            self.fail_next_commit = False
            message = "sync failed"
            raise OSError(message)

    def read(self) -> tuple[Record, ...]:
        return tuple(self.durable)


@settings(max_examples=examples(pr=8, nightly=100))
@given(before=st.lists(MESSAGES, max_size=3), failed=MESSAGES, after=MESSAGES)
def test_an_uncertain_append_forgets_the_prefix_so_the_log_says_what_happened(
    before: list[CoreMessage], failed: CoreMessage, after: CoreMessage
) -> None:
    storage = FlakyRecords()
    log = ConfirmedLog(storage)
    for message in before:
        stamp(message, log, 1, 0.0)
    storage.fail_next_commit = True
    assert isinstance(stamp(failed, log, 1, 0.0), Uncertain)
    confirmed = log.records()
    assert confirmed == tuple(storage.durable)
    assert isinstance(stamp(after, log, 1, 0.0), Appended)
    assert [record.seq for record in log.records()] == list(range(1, len(confirmed) + 2))
