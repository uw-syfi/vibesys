"""Tests for the public issue-queue progress-log interface."""

from pathlib import Path

from vibesys.loops.issue_queue.progress_log import FileProgressLog, ProgressLog


def test_file_progress_log_creates_and_appends_to_markdown(tmp_path: Path) -> None:
    path = tmp_path / "run" / "progress.md"

    log: ProgressLog = FileProgressLog(path)
    assert log.read() == "# Experiment Progress\n\n"

    log.append("## Iter 1 — Implementer\n\nDone.\n\n")
    log.append("### Iter 1 — Judge\n\nPass.\n\n")

    assert log.read() == (
        "# Experiment Progress\n\n"
        "## Iter 1 — Implementer\n\nDone.\n\n"
        "### Iter 1 — Judge\n\nPass.\n\n"
    )
