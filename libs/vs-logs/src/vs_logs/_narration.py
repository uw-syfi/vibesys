"""A process's own narration for humans: Python logging records as JSON Lines in a sink.

:class:`NarrationHandler` is a ``logging.Handler``. Each record becomes one
:class:`NarrationLine`, encoded as one JSON object on one line, written to an
``OutputSink`` (a log stream). Until a sink is attached, the newest records are kept in
a bounded buffer and written first, in order, when one is. A sink that fails closes the
handler and says so once through ``warn``; the caller never sees the failure.

The lines are for humans: a client renders them. Product code never parses them back.
"""

from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

from vs_sim.api import OutputStream

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from vs_sim.api import OutputSink

DEFAULT_BUFFERED_RECORDS = 1000
"""Records a handler keeps before a sink is attached; older ones are dropped and counted."""

UNCAUGHT_LOGGER = "uncaught"
"""The logger name an uncaught exception's line carries."""


class NarrationLevel(StrEnum):
    """A line's severity: the standard ``logging`` level at or below the record's."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @classmethod
    def of(cls, levelno: int) -> NarrationLevel:
        """The highest standard level at or below ``levelno`` (a custom level rounds down)."""
        for level, threshold in (
            (cls.CRITICAL, logging.CRITICAL),
            (cls.ERROR, logging.ERROR),
            (cls.WARNING, logging.WARNING),
            (cls.INFO, logging.INFO),
        ):
            if levelno >= threshold:
                return level
        return cls.DEBUG


class NarrationLine(BaseModel):
    """One narration record, as one JSON line of a narration stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: AwareDatetime
    """When the record was made, as ``logging`` stamped it."""
    level: NarrationLevel
    logger: str
    """The ``logging`` logger's name, or ``uncaught`` for an uncaught exception."""
    thread: str
    """The name of the thread that made the record."""
    message: str
    traceback: str | None = None
    """The formatted exception (and stack, when asked for) the record carries."""


class _State(StrEnum):
    BUFFERING = "buffering"
    ATTACHED = "attached"
    CLOSED = "closed"


class NarrationHandler(logging.Handler):
    """Write logging records as :class:`NarrationLine` JSON lines to a sink.

    Before :meth:`attach`, the newest ``buffered_records`` records wait in memory; an
    attached sink receives them first, in order, after a line counting any that were
    dropped. After :meth:`close`, or once the sink raised, records are dropped: whatever
    else handles them (``logging.lastResort`` on stderr) still does.
    """

    def __init__(
        self,
        *,
        warn: Callable[[str], object],
        buffered_records: int = DEFAULT_BUFFERED_RECORDS,
        level: int = logging.NOTSET,
    ) -> None:
        """Say through ``warn`` (once) when the sink fails; keep ``buffered_records`` before it."""
        super().__init__(level)
        self._warn = warn
        self._buffer: deque[bytes] = deque(maxlen=buffered_records)
        self._dropped = 0
        self._state = _State.BUFFERING
        self._sink: OutputSink | None = None

    @property
    def waiting(self) -> bool:
        """Whether no sink was attached yet and the handler is not closed."""
        return self._state is _State.BUFFERING

    def attach(self, sink: OutputSink) -> None:
        """Write the buffered records to ``sink``, then every later record as it comes.

        Attaching a handler that is already attached or closed is a programming error.
        """
        self.acquire()
        try:
            if self._state is not _State.BUFFERING:
                message = f"narration handler is {self._state}, not waiting for a sink"
                raise RuntimeError(message)
            self._sink = sink
            self._state = _State.ATTACHED
            pending = list(self._buffer)
            self._buffer.clear()
            if self._dropped:
                pending.insert(0, _dropped_line(self._dropped))
                self._dropped = 0
            for line in pending:
                if not self._write(line):
                    return
        finally:
            self.release()

    def close(self) -> None:
        """Stop writing: drop the buffer and every later record."""
        self.acquire()
        try:
            self._state = _State.CLOSED
            self._sink = None
            self._buffer.clear()
        finally:
            self.release()
        super().close()

    def report_uncaught(
        self,
        kind: type[BaseException],
        error: BaseException,
        trace: TracebackType | None,
        *,
        thread: str | None = None,
    ) -> None:
        """Record an exception nothing caught, with its traceback, as a ``CRITICAL`` line."""
        where = "" if thread is None else f" in thread {thread}"
        record = logging.LogRecord(
            name=UNCAUGHT_LOGGER,
            level=logging.CRITICAL,
            pathname=__file__,
            lineno=0,
            msg=f"Uncaught {kind.__name__}{where}: {error}",
            args=None,
            exc_info=(kind, error, trace),
        )
        if thread is not None:
            record.threadName = thread
        self.handle(record)

    def emit(self, record: logging.LogRecord) -> None:
        """Write ``record`` as one line, or buffer it until a sink is attached."""
        try:
            line = encode_record(record)
        except (TypeError, ValueError):
            # A message whose %-arguments do not fit: logging's own report, never a raise.
            self.handleError(record)
            return
        if self._state is _State.BUFFERING:
            if len(self._buffer) == self._buffer.maxlen:
                self._dropped += 1
            self._buffer.append(line)
        elif self._state is _State.ATTACHED:
            self._write(line)

    def _write(self, line: bytes) -> bool:
        """Write ``line``; on failure close, warn once, and return False."""
        sink = self._sink
        if sink is None:
            return False
        try:
            sink.write(OutputStream.STDERR, line)
        except OSError as exc:
            # A log stream never raises; another sink may (a full disk): degrade to stderr.
            self._state = _State.CLOSED
            self._sink = None
            self._warn(
                f"vibesys: the run narration stream failed ({type(exc).__name__}: {exc}); "
                "log records now reach only stderr.\n"
            )
            return False
        return True


def encode_record(record: logging.LogRecord) -> bytes:
    """``record`` as one JSON line of a narration stream."""
    trace = None
    if record.exc_info is not None and record.exc_info[0] is not None:
        trace = "".join(traceback.format_exception(*record.exc_info))
    if record.stack_info:
        trace = record.stack_info if trace is None else f"{trace}{record.stack_info}\n"
    line = NarrationLine(
        at=datetime.fromtimestamp(record.created, UTC),
        level=NarrationLevel.of(record.levelno),
        logger=record.name,
        thread=record.threadName or "",
        message=record.getMessage(),
        traceback=trace,
    )
    return line.model_dump_json().encode() + b"\n"


def _dropped_line(count: int) -> bytes:
    record = logging.LogRecord(
        name="vs_logs",
        level=logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg="%d earlier records were dropped before the narration stream opened",
        args=(count,),
        exc_info=None,
    )
    return encode_record(record)


class ThreadFailure(Protocol):
    """What ``threading.excepthook`` is called with (``threading.ExceptHookArgs``)."""

    @property
    def exc_type(self) -> type[BaseException]: ...

    @property
    def exc_value(self) -> BaseException | None: ...

    @property
    def exc_traceback(self) -> TracebackType | None: ...

    @property
    def thread(self) -> _Named | None: ...


class _Named(Protocol):
    @property
    def name(self) -> str: ...


type UncaughtHook = Callable[[type[BaseException], BaseException, TracebackType | None], object]
"""The shape of ``sys.excepthook``."""


def uncaught_hook(
    handler: NarrationHandler, previous: UncaughtHook
) -> Callable[[type[BaseException], BaseException, TracebackType | None], None]:
    """A ``sys.excepthook`` that records the exception in ``handler``, then calls ``previous``."""

    def hook(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
        handler.report_uncaught(kind, error, trace)
        previous(kind, error, trace)

    return hook


def uncaught_thread_hook[FailureT: ThreadFailure](
    handler: NarrationHandler, previous: Callable[[FailureT], object]
) -> Callable[[FailureT], None]:
    """A ``threading.excepthook`` that records the exception, then calls ``previous``.

    A ``SystemExit`` ends its thread quietly, as the default hook treats it.
    """

    def hook(failure: FailureT) -> None:
        error = failure.exc_value
        if error is not None and not isinstance(error, SystemExit):
            thread = None if failure.thread is None else failure.thread.name
            handler.report_uncaught(failure.exc_type, error, failure.exc_traceback, thread=thread)
        previous(failure)

    return hook


def installed_narration() -> NarrationHandler | None:
    """The root logger's :class:`NarrationHandler` still waiting for a sink, if any.

    Logging is configured per process: the entrypoint that owns the process installs the
    handler on the root logger, and the code that opens the stream finds it here.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, NarrationHandler) and handler.waiting:
            return handler
    return None
