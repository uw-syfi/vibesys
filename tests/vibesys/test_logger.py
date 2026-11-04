"""RunLogger unit tests — focus on the process-global stderr tee ownership."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from vs_logs.api import NarrationHandler, NarrationLevel, NarrationLine
from vs_project.api import LPRINT_LOGGER, RunLogger
from vs_sim.api import Capture, OutputStream
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from pathlib import Path


def test_tee_logger_owns_and_restores_stderr(tmp_path: Path) -> None:
    original = sys.stderr
    logger = RunLogger(tmp_path, clock=ManualClock())
    try:
        assert sys.stderr is not original  # tee installed
        sys.stderr.write("diagnostic\n")
    finally:
        logger.close()
    assert sys.stderr is original  # restored on close
    assert "diagnostic" in logger.path.read_text()


def test_tee_logger_preserves_real_stderr_file_descriptor(tmp_path: Path) -> None:
    original = sys.stderr
    logger = RunLogger(tmp_path, clock=ManualClock())
    try:
        assert sys.stderr.fileno() == original.fileno()
    finally:
        logger.close()


def test_no_tee_logger_leaves_stderr_untouched(tmp_path: Path) -> None:
    original = sys.stderr
    logger = RunLogger(tmp_path, tee_stderr=False, clock=ManualClock())
    try:
        # A no-tee sub-logger must never touch the process-global stderr, so
        # concurrent per-candidate loggers can't fight over (and mis-restore) it.
        assert sys.stderr is original
        logger.switch("gen001")
        assert sys.stderr is original
        logger.lprint("candidate line")
    finally:
        logger.close()
    assert sys.stderr is original
    assert "candidate line" in logger.path.read_text()


def test_switch_closes_each_superseded_log_file(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, tee_stderr=False, clock=ManualClock())
    first = logger.file

    second = logger.switch("round001")
    assert first.closed
    assert not second.closed

    third = logger.switch("round002")
    assert second.closed
    assert not third.closed

    logger.close()
    assert third.closed
    logger.close()  # Cleanup remains idempotent.


def test_stable_writer_follows_switch_after_old_file_is_closed(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, tee_stderr=False, clock=ManualClock())
    writer = logger.writer
    first = logger.file

    logger.switch("round001")
    writer.write("written after switch\n")
    writer.flush()

    assert first.closed
    assert "written after switch" in logger.path.read_text()
    logger.close()


def test_lprint_lines_are_logged_for_the_narration_stream(tmp_path: Path) -> None:
    sink = Capture(1 << 16)
    handler = NarrationHandler(warn=sys.stderr.write)
    handler.attach(sink)
    narration = logging.getLogger(LPRINT_LOGGER)
    previous_level = narration.level
    narration.addHandler(handler)
    narration.setLevel(logging.INFO)
    logger = RunLogger(tmp_path, tee_stderr=False, clock=ManualClock())
    try:
        logger.lprint("\x1b[32mround 1 started\x1b[0m")
    finally:
        logger.close()
        narration.removeHandler(handler)
        narration.setLevel(previous_level)
    (line,) = [
        NarrationLine.model_validate_json(raw)
        for raw in sink.kept(OutputStream.STDERR).splitlines()
    ]
    assert (line.level, line.logger, line.message) == (
        NarrationLevel.INFO,
        LPRINT_LOGGER,
        "round 1 started",
    )
