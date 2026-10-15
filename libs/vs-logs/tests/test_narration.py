"""``NarrationHandler`` writes a process's log records as JSON lines to a log stream."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

from vs_logs.api import (
    UNCAUGHT_LOGGER,
    MemoryLogStore,
    NarrationHandler,
    NarrationLevel,
    NarrationLine,
    StreamOutcome,
    uncaught_hook,
    uncaught_thread_hook,
)
from vs_logs.api.testing import CONTRACT_OWNER, ContractOwner, no_warning, read_everything
from vs_logs.read import reader_of
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from types import TracebackType

    from vs_sim.api import OutputStream

_LINES: TypeAdapter[NarrationLine] = TypeAdapter(NarrationLine)
_LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)


@dataclass(frozen=True)
class _Producer:
    """Makes records as a ``logging.Logger`` named ``name`` does, and hands them to ``handler``.

    A logger of the test's own, so the process's logging registry is left as it was.
    """

    name: str
    handler: NarrationHandler

    def log(
        self, level: int, message: str, *args: object, exc_info: BaseException | None = None
    ) -> None:
        trace = None if exc_info is None else (type(exc_info), exc_info, exc_info.__traceback__)
        record = logging.LogRecord(self.name, level, __file__, 0, message, args, trace)
        self.handler.handle(record)

    def info(self, message: str, *args: object) -> None:
        self.log(logging.INFO, message, *args)

    def warning(self, message: str, *args: object) -> None:
        self.log(logging.WARNING, message, *args)

    def error(self, message: str, *args: object, exc_info: BaseException | None = None) -> None:
        self.log(logging.ERROR, message, *args, exc_info=exc_info)


@dataclass
class _Subject:
    store: MemoryLogStore[ContractOwner]
    handler: NarrationHandler
    logger: _Producer
    warnings: list[str]


def _subject(name: str, *, buffered_records: int = 1000) -> _Subject:
    warnings: list[str] = []
    handler = NarrationHandler(warn=warnings.append, buffered_records=buffered_records)
    store = MemoryLogStore(CONTRACT_OWNER, threads=SimThreads(), warn=no_warning)
    return _Subject(store, handler, _Producer(name, handler), warnings)


def _lines(subject: _Subject, stream_id: str) -> list[NarrationLine]:
    reader = reader_of(subject.store, CONTRACT_OWNER)
    data = read_everything(reader, stream_id, 1 << 16).data
    return [_LINES.validate_json(line) for line in data.splitlines()]


def _fail(message: str) -> None:
    raise ValueError(message)


def _raise(message: str) -> BaseException:
    """A raised exception, with the traceback a real raise gives it."""
    try:
        _fail(message)
    except ValueError as error:
        return error
    raise AssertionError


@given(records=st.lists(st.tuples(st.sampled_from(_LEVELS), st.text(max_size=40)), max_size=20))
def test_records_at_every_level_reach_the_stream_with_level_logger_and_message(
    records: list[tuple[int, str]],
) -> None:
    subject = _subject("producer.module")
    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        subject.handler.attach(stream)
        for level, message in records:
            subject.logger.log(level, "%s", message)
    lines = _lines(subject, stream.stream_id)
    assert [(line.level, line.logger, line.message) for line in lines] == [
        (NarrationLevel(logging.getLevelName(level)), "producer.module", message)
        for level, message in records
    ]
    assert all(line.traceback is None for line in lines)


@given(
    early=st.lists(st.text(max_size=10), max_size=30),
    late=st.lists(st.text(max_size=10), max_size=5),
    capacity=st.integers(min_value=1, max_value=10),
)
def test_records_before_attach_are_flushed_in_order_and_overflow_is_counted(
    early: list[str], late: list[str], capacity: int
) -> None:
    subject = _subject("early", buffered_records=capacity)
    for message in early:
        subject.logger.info("%s", message)
    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        subject.handler.attach(stream)
        for message in late:
            subject.logger.info("%s", message)
    messages = [line.message for line in _lines(subject, stream.stream_id)]
    dropped = max(0, len(early) - capacity)
    notice = (
        [f"{dropped} earlier records were dropped before the narration stream opened"]
        if dropped
        else []
    )
    assert messages == [*notice, *early[dropped:], *late]


def test_an_exception_logged_on_the_main_path_carries_its_traceback() -> None:
    subject = _subject("main")
    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        subject.handler.attach(stream)
        error = _raise("boom")
        subject.logger.error("step failed", exc_info=error)
    (line,) = _lines(subject, stream.stream_id)
    assert (line.level, line.message) == (NarrationLevel.ERROR, "step failed")
    assert line.traceback is not None
    assert "ValueError: boom" in line.traceback
    assert "_raise" in line.traceback


def test_uncaught_exceptions_on_the_main_thread_and_in_a_thread_land_with_tracebacks() -> None:
    subject = _subject("hooks")
    seen: list[str] = []
    main_hook = uncaught_hook(
        subject.handler, lambda kind, _error, _trace: seen.append(kind.__name__)
    )
    thread_hook = uncaught_thread_hook(
        subject.handler, lambda failure: seen.append(failure.thread.name if failure.thread else "")
    )

    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        subject.handler.attach(stream)
        main_error = _raise("on main")
        main_hook(type(main_error), main_error, main_error.__traceback__)
        thread_error = _raise("in worker")
        thread_hook(_ThreadFailure(thread_error, _Thread("worker-1")))
    main_line, thread_line = _lines(subject, stream.stream_id)
    assert seen == ["ValueError", "worker-1"]
    for line, text in ((main_line, "on main"), (thread_line, "in worker")):
        assert (line.level, line.logger) == (NarrationLevel.CRITICAL, UNCAUGHT_LOGGER)
        assert line.traceback is not None
        assert f"ValueError: {text}" in line.traceback
    assert thread_line.thread == "worker-1"
    assert "in thread worker-1" in thread_line.message


@dataclass(frozen=True)
class _Thread:
    name: str


@dataclass(frozen=True)
class _ThreadFailure:
    """What ``threading.excepthook`` is called with, for a thread that raised ``error``."""

    error: BaseException
    thread: _Thread | None

    @property
    def exc_type(self) -> type[BaseException]:
        return type(self.error)

    @property
    def exc_value(self) -> BaseException:
        return self.error

    @property
    def exc_traceback(self) -> TracebackType | None:
        return self.error.__traceback__


class _FailingSink:
    def write(self, stream: OutputStream, data: bytes) -> None:
        message = f"No space left on device for {len(data)} bytes of {stream}"
        raise OSError(28, message)


def test_a_failing_stream_never_raises_into_the_caller_and_warns_once() -> None:
    subject = _subject("failing")
    subject.logger.info("buffered")
    subject.handler.attach(_FailingSink())
    subject.logger.warning("after")
    subject.logger.error("and again")
    assert len(subject.warnings) == 1
    assert "No space left on device" in subject.warnings[0]
    assert not subject.handler.waiting


def test_a_stream_closed_under_the_handler_drops_later_records_quietly() -> None:
    subject = _subject("closed")
    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        subject.handler.attach(stream)
        subject.logger.info("in time")
        stream.close(StreamOutcome.FAILED)
        subject.logger.info("too late")
    data = read_everything(reader_of(subject.store, CONTRACT_OWNER), stream.stream_id, 1 << 16)
    assert b"in time" in data.data
    assert b"too late" not in data.data
    assert subject.warnings == []


def test_records_after_close_are_dropped_and_attach_is_refused() -> None:
    subject = _subject("closing")
    subject.logger.info("never written")
    subject.handler.close()
    subject.logger.info("dropped")
    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        refused = False
        try:
            subject.handler.attach(stream)
        except RuntimeError:
            refused = True
    assert refused
    assert _lines(subject, stream.stream_id) == []
    assert subject.warnings == []


def test_a_record_whose_arguments_do_not_fit_is_not_raised_into_the_caller() -> None:
    subject = _subject("bad-args")
    with subject.store.open(ContractOwner(name="narration"), "vibesys") as stream:
        subject.handler.attach(stream)
        subject.logger.info("%d", "not a number")
        subject.logger.info("fine")
    assert [line.message for line in _lines(subject, stream.stream_id)] == ["fine"]
