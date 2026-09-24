"""Boot-stage timing spans for everything that runs before a run log exists.

Boot spends most of its wall clock in two stretches that cannot log normally
while they run:

* the *dispatch preamble* — ``_dispatch`` (``main.py``) parses the CLI
  invocation and announces the run before any loop starts;
* *run-context assembly* (``context.py``), whose early stages run before the
  ``RunLogger`` they will eventually write to is open.

Both are measured with :func:`span`, an OpenTelemetry-shaped context manager
built on nothing but the standard library. A span times its own body, records
on exit (including when the body raises), and nests: the enclosing span's name
qualifies the inner one, so the outermost span of a region doubles as that
region's total.

    with boot_trace.span("context"):
        with boot_trace.span("project_open"):
            project = Project.open(project_root)

    boot span context.project_open: 12ms
    boot span context: 431ms

Where the lines go
------------------

Recorded lines always land in the persistent run log: they buffer here, and
each consumer drains them with :func:`drain_log_lines` once it has a logger
(``_assemble_run_resources`` drains the preamble's lines at entry, ahead of its
own, so the log reads in the order the work happened). They reach stderr only
when ``VIBESYS_BOOT_TRACE=1`` (see :func:`trace_enabled`), because stderr is
the operator's terminal, not a diagnostics channel::

    VIBESYS_BOOT_TRACE=1 vibesys --input ... 2>trace.log

This module imports nothing from VibeSys, so ``main.py`` and ``cli.py`` can
use it before paying for the framework packages that ``vibesys.context``
pulls in (``vs_agent``, ``vibesys.backends``, ...). There is no
exporter, sampler, or propagation machinery: the span shape is the point, so
one could be added later without touching call sites.
"""

import functools
import os
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import ParamSpec, TypeVar

#: Set to exactly ``"1"`` to echo boot-trace lines to stderr.
BOOT_TRACE_ENV = "VIBESYS_BOOT_TRACE"

#: Epoch milliseconds at which the user's ``vibesys`` invocation started.
#: The CLI marks it (:func:`mark_launch`) and passes it to every process it
#: spawns (:func:`child_env`), so a frontend can report time since launch
#: rather than time since its own process started.
LAUNCH_START_ENV = "VIBESYS_LAUNCH_START_MS"

_P = ParamSpec("_P")
_R = TypeVar("_R")


def trace_enabled() -> bool:
    """Whether boot-trace lines should also be written to stderr.

    The rule is exact: the environment variable must be ``"1"``. Anything
    else, including ``"true"``, leaves the trace quiet.
    """
    return os.environ.get(BOOT_TRACE_ENV) == "1"


class _BootTrace:
    """Process-lifetime span stack and line buffer.

    One instance (:data:`_TRACE`) backs the module-level functions, which are
    its entire interface. Boot happens once per process on one thread, so the
    state is deliberately unsynchronized.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._open: list[str] = []
        self._launched_at_ms: int | None = None

    def push(self, name: str) -> str:
        """Open a span and return its name qualified by any enclosing spans."""
        self._open.append(name)
        return ".".join(self._open)

    def pop(self) -> None:
        self._open.pop()

    def record(self, line: str) -> None:
        self._lines.append(line)
        if trace_enabled():
            # The process's own stderr, not whatever ``RunLogger`` may have
            # teed over it: draining already writes this line to the run log,
            # and a teed echo would put a second copy there.
            stream = sys.__stderr__ or sys.stderr
            stream.write(f"{line}\n")

    def drain(self) -> list[str]:
        lines = list(self._lines)
        self._lines.clear()
        return lines

    def mark_launch(self) -> int:
        self._launched_at_ms = int(time.time() * 1000)
        return self._launched_at_ms

    def launched_at_ms(self) -> int:
        if self._launched_at_ms is None:
            return self.mark_launch()
        return self._launched_at_ms


_TRACE = _BootTrace()


@contextmanager
def span(name: str) -> Generator[None]:
    """Time this block and record ``boot span <qualified name>: <ms>ms``.

    Spans nest: *name* is qualified by whatever spans are still open, so an
    outermost span reports its region's total after its children report their
    parts. A body that raises is still recorded, tagged with the exception
    type, and the exception propagates unchanged.
    """
    qualified = _TRACE.push(name)
    started = time.perf_counter()
    outcome = ""
    try:
        yield
    except BaseException as exc:
        outcome = f" (raised {type(exc).__name__})"
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        _TRACE.pop()
        _TRACE.record(f"boot span {qualified}: {elapsed_ms:.0f}ms{outcome}")


def traced(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorator form of :func:`span`, for whole functions."""

    def decorate(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with span(name):
                return func(*args, **kwargs)

        return wrapper

    return decorate


def drain_log_lines() -> list[str]:
    """Pop every recorded line, for the caller to write to the run log.

    Draining is the only way lines reach the persistent log, and it empties
    the buffer, so each line is written once by whichever consumer takes it.
    """
    return _TRACE.drain()


def mark_launch() -> None:
    """Anchor "time since launch" at this moment.

    ``cli.main`` calls this before any doctor check, staleness check, or
    rebuild, so a frontend's own boot measurements can be reported against
    when the user actually ran the command.
    """
    _TRACE.mark_launch()


def child_env() -> dict[str, str]:
    """Environment entries to merge into any process the CLI spawns.

    Carries the launch anchor, and the stderr-trace request when one is
    active, so the whole process tree traces or stays quiet together. The
    anchor is set on demand, so a spawn that skipped :func:`mark_launch`
    still anchors (at spawn time) rather than shipping nothing.
    """
    env = {LAUNCH_START_ENV: str(_TRACE.launched_at_ms())}
    if trace_enabled():
        env[BOOT_TRACE_ENV] = "1"
    return env
