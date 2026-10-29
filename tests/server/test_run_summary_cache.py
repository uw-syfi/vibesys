"""The summary cache beside the journal never changes the summary: only how much is read to get it."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from tests.server.run_summary_support import RUN_ID, events, journal_lines

from server.events import EventType, OutputData, RunEvent, SpanStartedData
from server.host_run_summary import RUN_SUMMARY_FILE
from server.run_summary import RUN_SUMMARY_VERSION, RunSummaryFold, Summarized, SummaryState
from server.run_summary_cache import HEAD_BYTES, TAIL_BYTES, SummaryReader, summarize_journal
from vs_core.api import Span, SpanStatus, SpanType
from vs_sim.api.testing import SimThreads


def _scratch(lines: list[bytes]) -> bytes:
    return b"".join(lines)


def _from_scratch(journal: bytes) -> tuple[SummaryState, int]:
    """The summary of ``journal``, folded with no cache and no files: what any reader must get."""
    fold = RunSummaryFold()
    size = 0
    for line in journal.splitlines(keepends=True):
        if not fold.consume(line, complete=line.endswith(b"\n")):
            break
        size += len(line)
    return fold.state(), size


def _summarize(directory: Path) -> tuple[SummaryState, int]:
    summarized: Summarized = summarize_journal(directory / "run-events.jsonl")
    return summarized.fold.state(), summarized.journal_bytes


def test_helper_reader_returns_consistent_pages_from_an_unchanged_journal() -> None:
    lines = _padded_journal(100)
    with tempfile.TemporaryDirectory() as raw:
        journal = Path(raw) / "run-events.jsonl"
        journal.write_bytes(_scratch(lines))
        summarize_journal(journal)  # A prior helper process left a valid disk cache.
        reader = SummaryReader(SimThreads().lock())
        first = reader.summarize(journal)
        second = reader.summarize(journal)
        assert first.fold.state() == second.fold.state()
        assert first.fold.page(0) == second.fold.page(0)


def test_helper_reader_invalidates_on_append_replacement_and_cache_damage() -> None:
    lines = _padded_journal(120)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        reader = SummaryReader(SimThreads().lock())
        journal.write_bytes(_scratch(lines[:80]))
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]

        with journal.open("ab") as stream:
            stream.write(_scratch(lines[80:]))
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]

        other = [
            line.replace(RUN_ID.encode(), b"20261010-120000-22222222-dynamic") for line in lines
        ]
        assert len(_scratch(other)) == len(_scratch(lines))
        journal.write_bytes(_scratch(other))
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]

        (directory / RUN_SUMMARY_FILE).write_bytes(b"broken cache")
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]

        journal.write_bytes(_scratch(lines[:40]))
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]


def test_helper_reader_keeps_an_issued_page_stable_during_later_appends() -> None:
    lines = _padded_journal(120)
    with tempfile.TemporaryDirectory() as raw:
        journal = Path(raw) / "run-events.jsonl"
        journal.write_bytes(_scratch(lines[:80]))
        reader = SummaryReader(SimThreads().lock())
        first = reader.summarize(journal)
        state = first.fold.state()
        page = first.fold.page(0)

        with journal.open("ab") as stream:
            stream.write(_scratch(lines[80:]))
        second = reader.summarize(journal)

        assert second.fold.state() == _from_scratch(journal.read_bytes())[0]
        assert first.fold.state() == state
        assert first.fold.page(0) == page


def test_helper_reader_does_not_keep_an_unterminated_tail() -> None:
    lines = _padded_journal(12)
    with tempfile.TemporaryDirectory() as raw:
        journal = Path(raw) / "run-events.jsonl"
        reader = SummaryReader(SimThreads().lock())
        journal.write_bytes(_scratch(lines).rstrip(b"\n"))
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]
        journal.write_bytes(_scratch(lines))
        assert reader.summarize(journal).fold.state() == _from_scratch(journal.read_bytes())[0]


def _cache_boundary(directory: Path) -> int | None:
    try:
        document = json.loads((directory / RUN_SUMMARY_FILE).read_bytes())
    except (OSError, ValueError):
        return None
    return document["journal"]["boundary"]


@given(run_events=events(max_size=40), cuts=st.lists(st.integers(0, 40), max_size=4))
def test_a_summary_read_after_each_append_equals_the_whole_journals_fold(
    run_events: list, cuts: list[int]
) -> None:
    lines = journal_lines(run_events)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        journal.write_bytes(b"")
        written = 0
        for cut in [*sorted(cuts), len(lines)]:
            end = min(cut, len(lines))
            with journal.open("ab") as stream:
                stream.write(_scratch(lines[written:end]))
            written = max(written, end)
            assert _summarize(directory) == _from_scratch(journal.read_bytes())
            if written:
                assert _cache_boundary(directory) == journal.stat().st_size


@given(
    run_events=events(max_size=20).filter(bool),
    which=st.integers(0, 19),
    cut=st.integers(1, 5000),
)
def test_a_torn_last_line_is_no_event_until_it_is_finished(
    run_events: list, which: int, cut: int
) -> None:
    lines = journal_lines(run_events)
    which %= len(lines)
    body = lines[which].rstrip(b"\n")
    torn = body[: max(1, min(cut, len(body) - 1))]
    before = _scratch(lines[:which])
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        journal.write_bytes(before + torn)

        # a writer crashed mid-append: the summary is the journal before the torn line
        assert _summarize(directory) == _from_scratch(before)
        assert _cache_boundary(directory) in (None, len(before))

        # resume: the writer repairs the tail and appends the line whole, then the rest
        journal.write_bytes(_scratch(lines))
        assert _summarize(directory) == _from_scratch(_scratch(lines))


@given(run_events=events(max_size=20).filter(bool))
def test_a_complete_last_line_without_its_newline_counts_but_is_not_cached(
    run_events: list,
) -> None:
    lines = journal_lines(run_events)
    unterminated = _scratch(lines).rstrip(b"\n")
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        journal.write_bytes(unterminated)

        state, size = _summarize(directory)

        assert (state, size) == _from_scratch(unterminated)
        assert size == len(unterminated)
        assert _cache_boundary(directory) in (None, len(_scratch(lines[:-1])))
        journal.write_bytes(_scratch(lines))
        assert _summarize(directory) == _from_scratch(_scratch(lines))


@given(first=events(max_size=25), second=events(max_size=25))
def test_a_journal_replaced_by_another_is_summarized_afresh(first: list, second: list) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        journal.write_bytes(_scratch(journal_lines(first)))
        _summarize(directory)

        journal.write_bytes(_scratch(journal_lines(second)))

        assert _summarize(directory) == _from_scratch(journal.read_bytes())


@given(
    run_events=events(max_size=20),
    damage=st.sampled_from(
        [
            b"",
            b"not json",
            b"[]",
            b'{"format":"vibesys-run-summary"}',
            b'{"format":"vibesys-run-summary","version":0,"journal":{},"state":{}}',
        ]
    ),
)
def test_a_cache_that_is_not_one_is_ignored(run_events: list, damage: bytes) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        journal.write_bytes(_scratch(journal_lines(run_events)))
        (directory / RUN_SUMMARY_FILE).write_bytes(damage)

        assert _summarize(directory) == _from_scratch(journal.read_bytes())


def test_a_cache_of_the_previous_fold_version_is_dropped_and_the_journal_folded_afresh() -> None:
    """A version 2 cache holds no round count: serving it would call a legacy run's rounds none."""
    lines = _padded_journal(10)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "run-events.jsonl").write_bytes(_scratch(lines))
        assert _summarize(directory) == _from_scratch(_scratch(lines))
        cache = directory / RUN_SUMMARY_FILE
        written = json.loads(cache.read_bytes())
        written["version"] = RUN_SUMMARY_VERSION - 1
        del written["state"]["rounds_finished"]
        cache.write_bytes(json.dumps(written).encode())

        assert _summarize(directory) == _from_scratch(_scratch(lines))
        assert json.loads(cache.read_bytes())["version"] == RUN_SUMMARY_VERSION


def test_a_cache_that_cannot_be_written_costs_nothing_but_time() -> None:
    lines = _padded_journal(10)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "run-events.jsonl").write_bytes(_scratch(lines))
        (directory / RUN_SUMMARY_FILE).mkdir()  # a directory in the cache's place

        assert _summarize(directory) == _from_scratch(_scratch(lines))


def test_a_run_that_journaled_nothing_has_an_empty_summary() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)

        assert _summarize(directory) == _from_scratch(b"")
        (directory / "run-events.jsonl").write_bytes(b"")
        assert _summarize(directory) == _from_scratch(b"")
        assert _cache_boundary(directory) is None


def _padded_journal(count: int) -> list[bytes]:
    """Events whose bytes pass both fingerprint windows, with spans changing along the way."""
    made: list[RunEvent] = []
    for sequence in range(1, count + 1):
        if sequence % 7 == 0:
            span = Span(
                id=f"op:{sequence % 5}",
                parent=None,
                type=SpanType.OPERATION,
                name="gate",
                status=SpanStatus.DONE,
                started_at=float(sequence),
            )
            data: SpanStartedData | OutputData = SpanStartedData(span=span)
            event_type = EventType.SPAN_STARTED
        else:
            data = OutputData(stream="stdout", content="x" * 1500)
            event_type = EventType.OUTPUT
        made.append(
            RunEvent(
                sequence=sequence,
                run_id=RUN_ID,
                timestamp=datetime(2026, 10, 10, 12, tzinfo=UTC),
                type=event_type,
                data=data,
            )
        )
    return journal_lines(made)


def test_a_journal_longer_than_the_fingerprint_windows_extends_its_cache() -> None:
    lines = _padded_journal(400)
    assert sum(map(len, lines[:100])) > HEAD_BYTES + TAIL_BYTES
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        for end in (100, 250, 400):
            journal.write_bytes(_scratch(lines[:end]))
            assert _summarize(directory) == _from_scratch(journal.read_bytes())
            assert _cache_boundary(directory) == journal.stat().st_size


def test_a_cache_is_dropped_when_the_start_of_the_journal_differs() -> None:
    lines = _padded_journal(120)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        journal = directory / "run-events.jsonl"
        journal.write_bytes(_scratch(lines))
        _summarize(directory)
        other = [
            line.replace(RUN_ID.encode(), b"20261010-120000-22222222-dynamic") for line in lines
        ]

        journal.write_bytes(_scratch(other))

        assert _summarize(directory) == _from_scratch(journal.read_bytes())
