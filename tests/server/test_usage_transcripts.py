"""A person's CLI use is read from the provider's transcripts in bounded, resumable calls.

The transcripts are files the test writes (``tests/server/transcript_support.py``), as a provider
would grow them, and the host's cache is the real file. What a read finds must not depend on how
the files grew between reads or on how large the byte budget of each call was: the rows after
any schedule of appends and budgets equal the rows of one read of the finished files.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.transcript_support import START, Request, line, requests, session_file

from server.usage_transcripts import RETENTION, SCAN_FILE, SCAN_VERSION, scan_transcripts
from vs_proto.api import usage_state

SESSIONS = ("s1", "s2", "s3")
NOW = START + timedelta(days=1)


def _rows(scan_dir: Path, home: Path, *, budget: int, now=NOW):  # noqa: ANN001, ANN202
    scan = scan_transcripts(
        scan_dir, {"HOME": str(home)}, now=now, since=START - timedelta(days=1), budget=budget
    )
    return scan, sorted(
        (
            found.provider,
            found.row.quarter,
            found.row.session_id,
            found.row.cwd,
            found.row.model,
            found.row.input,
            found.row.cache_read,
            found.row.output,
            found.row.turns,
        )
        for found in scan.rows
    )


def _write(home: Path, batch: list[Request], written: dict[str, int]) -> None:
    for request in batch:
        index = written.get(request.session, 0)
        written[request.session] = index + 1
        with session_file(home, request.session).open("ab") as stream:
            stream.write(line(request, index))


@settings(max_examples=25)
@given(
    batches=st.lists(st.lists(requests(SESSIONS), min_size=1, max_size=6), min_size=1, max_size=4),
    budgets=st.lists(st.integers(min_value=1, max_value=4000), min_size=1, max_size=4),
)
def test_rows_do_not_depend_on_how_the_files_grew_or_how_the_calls_were_budgeted(
    batches: list[list[Request]], budgets: list[int]
) -> None:
    """Appends between reads and any budgets give the rows of one read of the whole."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        grown, whole = root / "grown", root / "whole"
        written: dict[str, int] = {}
        for number, batch in enumerate(batches):
            _write(grown, batch, written)
            _rows(root / "cache-grown", grown, budget=budgets[number % len(budgets)])
        for _ in range(sum(len(batch) for batch in batches) + len(SESSIONS) + 1):
            scan, found = _rows(root / "cache-grown", grown, budget=budgets[0])
            if scan.pending_files == 0:
                break
        assert scan.pending_files == 0

        written = {}
        for batch in batches:
            _write(whole, batch, written)
        _, expected = _rows(root / "cache-whole", whole, budget=10**9)
        assert found == expected
        assert found, "the generated sessions produced rows"


@settings(max_examples=25)
@given(
    batch=st.lists(requests(SESSIONS), min_size=1, max_size=20),
    budgets=st.lists(st.integers(min_value=1, max_value=3000), min_size=1, max_size=5),
)
def test_a_read_settles_every_file_in_a_finite_number_of_calls_for_any_budget(
    batch: list[Request], budgets: list[int]
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write(root / "home", batch, {})
        files = len({request.session for request in batch})
        pending = [files + 1]
        for call in range(len(batch) + files + 2):
            scan, _ = _rows(root / "cache", root / "home", budget=budgets[call % len(budgets)])
            pending.append(scan.pending_files)
            if scan.pending_files == 0:
                break
        assert pending[-1] == 0


def test_a_cache_that_fails_its_rules_is_rebuilt_whole() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write(root / "home", [Request("s1", 3, "claude-opus-5-5", "/work/alpha", 100, 10, 5)], {})
        _, before = _rows(root / "cache", root / "home", budget=10**9)
        (root / "cache" / SCAN_FILE).write_bytes(b"\xff\x00 not a cache")
        _, after = _rows(root / "cache", root / "home", budget=10**9)
        assert after == before != []
        stale = usage_state.TranscriptScanCache(version=SCAN_VERSION + 1)
        (root / "cache" / SCAN_FILE).write_bytes(stale.to_binary())
        _, again = _rows(root / "cache", root / "home", budget=10**9)
        assert again == before


def test_a_file_that_was_rewritten_is_read_again_and_its_old_rows_are_dropped() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "home"
        _write(home, [Request("s1", 3, "claude-opus-5-5", "/work/alpha", 100, 0, 5)] * 3, {})
        _, first = _rows(root / "cache", home, budget=10**9)
        assert sum(row[-1] for row in first) == 3
        session_file(home, "s1").write_bytes(
            line(Request("s1", 40, "claude-haiku-5-5", "/work/alpha", 7, 0, 1), 0)
        )
        _, second = _rows(root / "cache", home, budget=10**9)
        assert [(row[4], row[5]) for row in second] == [("claude-haiku-5-5", 7)]


def test_a_request_still_being_written_is_counted_once_it_is_whole() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "home"
        whole = line(Request("s1", 3, "claude-opus-5-5", "/work/alpha", 100, 0, 5), 0)
        path = session_file(home, "s1")
        path.write_bytes(whole[: len(whole) // 2])
        _, torn = _rows(root / "cache", home, budget=10**9)
        assert torn == []
        path.write_bytes(whole)
        _, done = _rows(root / "cache", home, budget=10**9)
        assert [row[-1] for row in done] == [1]


def test_rows_older_than_the_retention_are_dropped_whatever_the_files_hold() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "home"
        _write(home, [Request("s1", 3, "claude-opus-5-5", "/work/alpha", 100, 0, 5)], {})
        _, kept = _rows(
            root / "cache", home, budget=10**9, now=START + RETENTION - timedelta(days=1)
        )
        assert kept != []
        _, dropped = _rows(
            root / "cache", home, budget=10**9, now=START + RETENTION + timedelta(days=2)
        )
        assert dropped == []


def test_a_host_with_no_transcript_directory_reports_it_missing_and_has_no_rows() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        scan, rows = _rows(root / "cache", root / "empty-home", budget=10**9)
        assert rows == []
        assert {report.status for report in scan.reports} == {usage_state.UsageSourceStatus.MISSING}
