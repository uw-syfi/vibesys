"""RunLogger unit tests — focus on the process-global stderr tee ownership."""

from __future__ import annotations

import sys

from vibesys.run import RunLogger


def test_tee_logger_owns_and_restores_stderr(tmp_path):
    original = sys.stderr
    logger = RunLogger(tmp_path)
    try:
        assert sys.stderr is not original  # tee installed
        print("diagnostic", file=sys.stderr)
    finally:
        logger.close()
    assert sys.stderr is original  # restored on close
    assert "diagnostic" in logger.path.read_text()


def test_no_tee_logger_leaves_stderr_untouched(tmp_path):
    original = sys.stderr
    logger = RunLogger(tmp_path, tee_stderr=False)
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


def test_switch_closes_each_superseded_log_file(tmp_path):
    logger = RunLogger(tmp_path, tee_stderr=False)
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


def test_stable_writer_follows_switch_after_old_file_is_closed(tmp_path):
    logger = RunLogger(tmp_path, tee_stderr=False)
    writer = logger.writer
    first = logger.file

    logger.switch("round001")
    writer.write("written after switch\n")
    writer.flush()

    assert first.closed
    assert "written after switch" in logger.path.read_text()
    logger.close()
