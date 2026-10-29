"""A detached run sends its log records and uncaught exceptions to its narration stream."""

from __future__ import annotations

import logging
import sys

from entrypoints.detached_run import narration_installed
from vs_logs.api import (
    UNCAUGHT_LOGGER,
    NarrationLevel,
    NarrationLine,
    installed_narration,
)
from vs_sim.api import Capture, OutputStream


def _lines(sink: Capture) -> list[NarrationLine]:
    return [
        NarrationLine.model_validate_json(raw)
        for raw in sink.kept(OutputStream.STDERR).splitlines()
    ]


def _raised(message: str) -> BaseException:
    def fail() -> None:
        raise RuntimeError(message)

    try:
        fail()
    except RuntimeError as error:
        return error
    raise AssertionError


def test_records_and_main_thread_tracebacks_wait_for_the_stream_then_reach_it() -> None:
    root = logging.getLogger()
    level, handlers, hook = root.level, list(root.handlers), sys.excepthook
    sink = Capture(1 << 16)
    with narration_installed() as handler:
        assert installed_narration() is handler
        logging.getLogger("vibesys.example").info("resolving the project")
        error = _raised("import failed")
        sys.excepthook(type(error), error, error.__traceback__)
        handler.attach(sink)
        logging.getLogger("vibesys.example").debug("below the configured level")
    lines = _lines(sink)
    assert [(line.level, line.logger) for line in lines] == [
        (NarrationLevel.INFO, "vibesys.example"),
        (NarrationLevel.CRITICAL, UNCAUGHT_LOGGER),
    ]
    assert lines[1].traceback is not None
    assert "RuntimeError: import failed" in lines[1].traceback
    assert (root.level, root.handlers, sys.excepthook) == (level, handlers, hook)
    assert installed_narration() is None
