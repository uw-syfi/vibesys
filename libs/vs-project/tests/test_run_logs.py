"""``read_latest_run_log`` is the one reader of the run logs ``RunLogger`` writes."""

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import RunLogger, read_latest_run_log
from vs_sim.api.testing import ManualClock


def test_a_missing_or_empty_directory_has_no_run_log(tmp_path: Path) -> None:
    assert read_latest_run_log(tmp_path / "absent") is None
    assert read_latest_run_log(tmp_path) is None


def test_reads_back_what_the_run_logger_wrote(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, tee_stderr=False, clock=ManualClock())
    logger.lprint("round 1 started\n")
    logger.close()

    latest = read_latest_run_log(tmp_path)

    assert latest is not None
    assert latest.name == logger.path.name
    assert "round 1 started" in latest.text
    assert not latest.truncated


def test_newest_regular_file_wins_and_links_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "run-1.log").write_text("old\n", encoding="utf-8")
    (tmp_path / "run-2.log").write_text("new\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (tmp_path / "run-3.log").symlink_to(outside)
    (tmp_path / "run-4.log").mkdir()

    latest = read_latest_run_log(tmp_path)

    assert latest is not None
    assert (latest.name, latest.text) == ("run-2.log", "new\n")


@settings(max_examples=25)
@given(content=st.binary(max_size=200), cap=st.integers(min_value=1, max_value=64))
def test_returns_at_most_the_capped_tail_of_any_bytes(
    tmp_path_factory: pytest.TempPathFactory, content: bytes, cap: int
) -> None:
    directory = tmp_path_factory.mktemp("logs")
    (directory / "run-1.log").write_bytes(content)

    latest = read_latest_run_log(directory, max_bytes=cap)

    assert latest is not None
    assert latest.text == content[-cap:].decode("utf-8", errors="replace")
    assert latest.truncated == (len(content) > cap)
