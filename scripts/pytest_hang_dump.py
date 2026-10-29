"""Name each failure as it is reported, and dump every thread before a CI job is cancelled.

Quiet progress (``-q``, and the default one-character reporter) keeps a
failure's node id for the session summary. A cancel before that summary
leaves only ``F`` marks. This plugin writes the node id when the report
arrives, from the controller, so the line is already in the log.

The deadline dump is separate. Its absolute unix time is
``VIBESYS_PYTEST_DUMP_AT``, which ``scripts/pytest_job_deadline.py`` derives
from the job's ``timeout-minutes``. Unset, as in a local run, nothing is armed.

Stacks are printed with ``traceback.print_stack`` while this thread holds the
GIL. ``faulthandler.dump_traceback_later`` is not used: its watchdog dumps from
a C thread that holds no GIL, and that has segfaulted workers in this suite
(see the timeout note in ``pyproject.toml``). pytest-timeout already owns
SIGALRM for the per-test bound, so this deadline waits on a daemon timer
instead of a second alarm.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from scripts.pytest_job_deadline import DUMP_AT_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import FrameType

_TIMER = pytest.StashKey[threading.Timer]()
# pytest prints the node id itself once verbosity reaches ``-v``.
_NAMED_PROGRESS = 1


class _Running:
    """Which test this process is inside, and whether its deadline timer is armed."""

    def __init__(self) -> None:
        self.nodeid: str | None = None
        self.armed = False
        self.verbose = 0
        self.worker = False


_state = _Running()


@contextmanager
def running_test(nodeid: str) -> Iterator[None]:
    """Name ``nodeid`` as the test a deadline dump should report."""
    previous = _state.nodeid
    _state.nodeid = nodeid
    try:
        yield
    finally:
        _state.nodeid = previous


def current_nodeid() -> str | None:
    """The test running on this worker, or None between tests."""
    return _state.nodeid


@contextmanager
def reporting_as(*, verbose: int, worker: bool) -> Iterator[None]:
    """Report failures as a session with this verbosity and worker role, then restore."""
    previous = (_state.verbose, _state.worker)
    _state.verbose = verbose
    _state.worker = worker
    try:
        yield
    finally:
        _state.verbose, _state.worker = previous


def failure_notice(report: pytest.TestReport, *, verbose: int, worker: bool) -> str | None:
    """The line naming one failure, or None when this process should stay quiet.

    An xdist worker stays quiet: the controller reports the same outcome, and
    a worker's print is captured until the summary this line exists to replace.
    At ``-v`` and above, pytest already prints the node id as the report arrives.
    """
    if worker or verbose >= _NAMED_PROGRESS or not report.failed:
        return None
    kind = "ERROR" if report.when != "call" else "FAILED"
    return f"{kind} {report.nodeid}\n"


def render_dump(
    nodeid: str | None,
    frames: Mapping[int, FrameType],
    names: Mapping[int, str],
) -> str:
    """The cancel dump: the running test, then one stack per thread."""
    running = nodeid or "(no test is running)"
    parts = [
        "pytest job deadline: dumping every thread before the job is cancelled\n",
        f"running test: {running}\n",
    ]
    for ident, frame in frames.items():
        name = names.get(ident, "unknown")
        parts.append(f"\nThread {name} ({ident}):\n")
        buffer = _stack(frame)
        parts.append(buffer)
    return "".join(parts)


def pytest_configure(config: pytest.Config) -> None:
    """Arm the cancel dump when the workflow passed an absolute deadline."""
    _state.verbose = int(config.option.verbose)
    # workerinput exists only in an xdist worker. The controller reports outcomes.
    _state.worker = hasattr(config, "workerinput")
    raw = os.environ.get(DUMP_AT_ENV)
    if _state.armed or raw is None or raw == "":
        return
    try:
        deadline = int(raw)
    except ValueError as error:
        message = f"{DUMP_AT_ENV} must be a unix time, got {raw!r}"
        raise pytest.UsageError(message) from error
    delay = deadline - time.time()
    timer = threading.Timer(max(delay, 0.0), _dump)
    timer.daemon = True
    timer.start()
    _state.armed = True
    # The timer is the only reference; without the stash a worker can collect it
    # before it fires. It is a daemon, so a finished session still exits.
    config.stash[_TIMER] = timer


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    """Remember the test a deadline dump should name, for the whole protocol."""
    with running_test(item.nodeid):
        yield


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Print a failing node id now, before a cancel can skip the session summary."""
    line = failure_notice(report, verbose=_state.verbose, worker=_state.worker)
    if line is None:
        return
    sys.stderr.write(line)
    sys.stderr.flush()


def _dump() -> None:
    frames = _thread_frames()
    names = {
        thread.ident: thread.name for thread in threading.enumerate() if thread.ident is not None
    }
    sys.stderr.write(render_dump(current_nodeid(), frames, names))
    sys.stderr.flush()


def _thread_frames() -> dict[int, FrameType]:
    # lint-waiver: LW-339311 [SLF001]; traceback walks one thread.
    # > faulthandler's watchdog dumps without the GIL and has segfaulted workers.
    return sys._current_frames()  # noqa: SLF001


def _stack(frame: FrameType) -> str:
    buffer = io.StringIO()
    traceback.print_stack(frame, file=buffer)
    return buffer.getvalue()
