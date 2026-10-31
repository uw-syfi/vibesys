"""A run's rollup is its journal's usage, rolled up: conserved, deterministic, and always built.

The rollup is a cache of the journal. Its rows must add up to what the journal's turns spent
whatever the split of a fold into a cached part and a tail, a multi-model turn counts once, and a
run whose summary cache is valid but which has no rollup (the summary cache's own write is
skipped when it already covers the journal) still gets one, so a read settles in a finite number
of calls whatever mix of cached files a host holds.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.server.run_summary_support import RUN_ID, events, journal_lines
from tests.server.usage_support import MemoryRollupStore, MemoryUsageJournal

from server.events import EventType, RunEvent, TurnUsageData
from server.host_api import RUN_EVENTS_FILE
from server.run_summary import RunSummaryFold, Summarized, SummaryState
from server.run_summary_cache import summarize_journal
from server.usage_rollup import (
    ROLLUP_FILE,
    ROLLUP_VERSION,
    Budget,
    JournalIdentity,
    Outcome,
    build_rollup,
    is_fresh,
    quarter_of,
    read_or_build,
)
from server.usage_scan import FileRollupStore, collect_runs
from vs_project.api import Project
from vs_proto.api import validate

IDENTITY = JournalIdentity(1, "1" * 64, "2" * 64)
START = datetime(2026, 10, 10, 12, tzinfo=UTC)


def _spread(run_events: list[RunEvent], minutes: list[int]) -> list[RunEvent]:
    """The events with their own times, so turns fall in different quarter hours."""
    return [
        event.model_copy(
            update={"timestamp": START + timedelta(minutes=minutes[index % len(minutes)])}
        )
        for index, event in enumerate(run_events)
    ]


def _fold(run_events: list[RunEvent]) -> SummaryState:
    fold = RunSummaryFold()
    for line in journal_lines(run_events):
        assert fold.consume(line)
    return fold.state()


def _timed_events(max_size: int = 30) -> st.SearchStrategy[list[RunEvent]]:
    return st.tuples(
        events(max_size=max_size),
        st.lists(st.integers(min_value=0, max_value=600), min_size=1, max_size=12),
    ).map(lambda pair: _spread(pair[0], pair[1]))


@dataclass
class _Totals:
    turns: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    recorded: float = 0.0


def _expected(run_events: list[RunEvent]) -> _Totals:
    """What the journal's turns spent, worked out from the typed events alone.

    An event at or before the last folded sequence is skipped. A turn that names its models
    spends the sum of their rows and counts their requests; a turn that does not spends its own
    counts and is one request.
    """
    totals = _Totals()
    last = 0
    for event in run_events:
        if event.sequence <= last:
            continue
        last = event.sequence
        data: Any = event.data
        if event.type is not EventType.TURN_USAGE_RECORDED or not isinstance(data, TurnUsageData):
            continue
        if data.by_model:
            for row in data.by_model:
                totals.input += row.input_tokens
                totals.cache_read += row.cache_read_input_tokens
                totals.cache_write += row.cache_creation_input_tokens
                totals.output += row.output_tokens
                totals.turns += row.turns
                totals.recorded += row.recorded_usd or 0.0
        else:
            totals.input += data.input_tokens or 0
            totals.cache_read += data.cache_read_input_tokens or 0
            totals.cache_write += data.cache_creation_input_tokens or 0
            totals.output += data.output_tokens or 0
            totals.turns += 1
            totals.recorded += data.total_cost_usd or 0.0
    return totals


@given(run_events=_timed_events())
def test_a_rollup_holds_what_the_journals_turns_spent(run_events: list[RunEvent]) -> None:
    rollup = build_rollup(_fold(run_events), (), IDENTITY)
    validate(rollup)
    expected = _expected(run_events)
    assert sum(row.turns for row in rollup.rows) == expected.turns
    assert sum(row.input for row in rollup.rows) == expected.input
    assert sum(row.cache_read for row in rollup.rows) == expected.cache_read
    assert sum(row.cache_write for row in rollup.rows) == expected.cache_write
    assert sum(row.output for row in rollup.rows) == expected.output
    recorded = math.fsum(row.recorded_usd or 0.0 for row in rollup.rows)
    assert math.isclose(recorded, expected.recorded, rel_tol=1e-9, abs_tol=1e-9)


@given(run_events=_timed_events(), cut=st.integers(min_value=0, max_value=40))
def test_a_rollup_is_the_same_for_any_split_of_the_fold(
    run_events: list[RunEvent], cut: int
) -> None:
    """A fold resumed from a cached state gives the bytes a fold of the whole journal gives."""
    lines = journal_lines(run_events)
    head = RunSummaryFold()
    for line in lines[: cut % (len(lines) + 1)]:
        assert head.consume(line)
    resumed = RunSummaryFold.from_state(head.state())
    for line in lines[cut % (len(lines) + 1) :]:
        assert resumed.consume(line)
    whole = build_rollup(_fold(run_events), (), IDENTITY)
    assert build_rollup(resumed.state(), (), IDENTITY).to_binary() == whole.to_binary()


@given(run_events=_timed_events())
def test_rows_are_placed_by_the_quarter_hour_a_turn_ended_in(run_events: list[RunEvent]) -> None:
    state = _fold(run_events)
    rollup = build_rollup(state, (), IDENTITY)
    quarters = {quarter_of(record.ended_at) for record in state.records if record.ended_at}
    assert {row.quarter for row in rollup.rows} == (quarters if rollup.rows else set())
    assert all(row.quarter == math.floor(row.quarter) for row in rollup.rows)
    assert rollup.rows == sorted(
        rollup.rows,
        key=lambda row: (
            row.quarter,
            row.provider,
            row.model,
            int(row.speed),
            row.role,
            int(row.basis),
        ),
    )


@example(run_events=[])
@given(run_events=_timed_events(max_size=20))
def test_a_turn_with_several_models_counts_one_request(run_events: list[RunEvent]) -> None:
    """Whatever the models of a turn, the turn's requests are on one row: its primary's."""
    state = _fold(run_events)
    rollup = build_rollup(state, (), IDENTITY)
    requests = sum(
        sum(row.turns for row in record.by_model) if record.by_model else 1
        for record in state.records
        if record.ended_at is not None
    )
    assert sum(row.turns for row in rollup.rows) == requests


@given(run_events=_timed_events())
def test_the_session_ids_of_a_run_are_its_turns_and_complete_only_when_all_name_one(
    run_events: list[RunEvent],
) -> None:
    state = _fold(run_events)
    rollup = build_rollup(state, (), IDENTITY)
    named = {record.provider_session_id for record in state.records if record.provider_session_id}
    assert list(rollup.provider_session_ids) == sorted(named)
    assert rollup.session_ids_complete == all(
        record.provider_session_id is not None for record in state.records
    )


# --- read_or_build: the path from a journal to a rollup ---


@dataclass(frozen=True)
class _Run:
    size: int
    stored: str
    """``none``, ``fresh``, ``stale`` (an older journal's), or ``other`` (another writer's version)."""


_RUNS = st.lists(
    st.builds(
        _Run,
        size=st.integers(min_value=0, max_value=10_000),
        stored=st.sampled_from(["none", "fresh", "stale", "other"]),
    ),
    min_size=1,
    max_size=8,
)


def _journal_of(size: int) -> MemoryUsageJournal:
    text = (b'{"sequence":1}\n' * (size // 15 + 1))[: max(15, size)] if size else b""
    return MemoryUsageJournal(text=text, state=RunSummaryFold().state())


def _stored(journal: MemoryUsageJournal, kind: str) -> MemoryRollupStore:
    store = MemoryRollupStore()
    if kind == "none":
        return store
    assert journal.state is not None
    identity = journal.identity()
    stale = JournalIdentity(identity.boundary + 1, identity.head_sha256, identity.tail_sha256)
    store.write(build_rollup(journal.state, (), identity if kind in {"fresh", "other"} else stale))
    if kind == "other":
        rollup = store.read()
        assert rollup is not None
        rollup.version = ROLLUP_VERSION + 1
        store.stored = rollup.to_binary()
    store.writes = 0
    return store


@given(
    runs=_RUNS, budgets=st.lists(st.integers(min_value=1, max_value=20_000), min_size=1, max_size=6)
)
def test_reads_settle_every_run_in_a_finite_number_of_calls(
    runs: list[_Run], budgets: list[int]
) -> None:
    """From any mix of stored rollups, journal sizes and budgets, repeated reads reach no pending run.

    Every call with a positive budget settles at least one run that is pending, so the number of
    calls needed is at most the number of runs, whatever the budgets are.
    """
    journals = [_journal_of(run.size) for run in runs]
    stores = [_stored(journal, run.stored) for journal, run in zip(journals, runs, strict=True)]
    for call in range(len(runs) + 1):
        budget = Budget(budgets[call % len(budgets)])
        reads = [
            read_or_build(journal, store, budget)
            for journal, store in zip(journals, stores, strict=True)
        ]
        pending = sum(read.outcome is Outcome.PENDING for read in reads)
        if pending == 0:
            break
    assert pending == 0
    assert all(
        read.rollup is not None and read.outcome in {Outcome.FRESH, Outcome.BUILT} for read in reads
    )
    for journal, store in zip(journals, stores, strict=True):
        stored = store.read()
        assert stored is not None
        assert is_fresh(stored, journal.identity())


@given(runs=_RUNS)
def test_a_read_that_pays_for_nothing_still_settles_one_run(runs: list[_Run]) -> None:
    """The first charge of a budget is paid whatever it costs, so a one byte budget progresses."""
    journals = [_journal_of(run.size) for run in runs]
    stores = [_stored(journal, run.stored) for journal, run in zip(journals, runs, strict=True)]
    behind = [
        index
        for index, (journal, store) in enumerate(zip(journals, stores, strict=True))
        if not ((stored := store.read()) is not None and is_fresh(stored, journal.identity()))
    ]
    budget = Budget(1)
    reads = [
        read_or_build(journal, store, budget)
        for journal, store in zip(journals, stores, strict=True)
    ]
    built = [index for index, read in enumerate(reads) if read.outcome is Outcome.BUILT]
    assert bool(built) == bool(behind)
    if behind:
        assert built[0] == behind[0]


def test_a_journal_that_grew_is_folded_again() -> None:
    journal = _journal_of(100)
    store = MemoryRollupStore()
    assert read_or_build(journal, store, Budget(10**6)).outcome is Outcome.BUILT
    assert read_or_build(journal, store, Budget(10**6)).outcome is Outcome.FRESH
    journal.grow(b'{"sequence":2}\n')
    assert read_or_build(journal, store, Budget(10**6)).outcome is Outcome.BUILT
    assert journal.folds == 2


# --- the real files ---


class _Projects:
    """The host's projects for ``collect_runs``: the helper's own summary of the journals."""

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        return summarize_journal(Project.log_directory_for(project, run_id) / RUN_EVENTS_FILE)

    def run_digest(self, project_root: Path, run_id: str) -> None:
        del project_root, run_id


def _write_run(project: Path, run_id: str, body: bytes) -> Path:
    journal = Project.log_directory_for(project, run_id) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(body)
    return journal


@settings(max_examples=15)
@given(run_events=_timed_events(max_size=25).filter(bool))
def test_a_finished_run_with_a_valid_summary_and_no_rollup_gets_one(
    run_events: list[RunEvent],
) -> None:
    """The summary cache's own write is skipped when it covers the journal; the rollup is not.

    A run read once before rollups existed has a valid ``run-summary.json`` and no
    ``run-usage.pb``. The read must still build it, and a second read must find it fresh, or a
    host's pending runs would never reach zero.
    """
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        journal = _write_run(project, RUN_ID, b"".join(journal_lines(run_events)))
        rollup_file = journal.with_name(ROLLUP_FILE)
        summarize_journal(journal)  # the summary is cached, as for a run read before rollups
        since = START - timedelta(days=1)

        first = collect_runs(_Projects(), [project], since=since, budget=Budget(1))
        rollup = FileRollupStore(rollup_file).read()
        assert first.pending == 0
        assert rollup is not None
        assert rollup.journal is not None
        identity = JournalIdentity(
            rollup.journal.boundary, rollup.journal.head_sha256, rollup.journal.tail_sha256
        )
        assert rollup.to_binary() == build_rollup(_fold(run_events), (), identity).to_binary()

        stamp = rollup_file.stat().st_mtime_ns
        again = collect_runs(_Projects(), [project], since=since, budget=Budget(1))
        assert again.pending == 0
        assert rollup_file.stat().st_mtime_ns == stamp


def test_a_stale_journal_is_not_opened() -> None:
    """A journal not written since the window began cannot hold a turn in it."""
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        journal = _write_run(project, RUN_ID, b"".join(journal_lines([])))
        collected = collect_runs(
            _Projects(), [project], since=START + timedelta(days=3650), budget=Budget(1)
        )
        assert (collected.scanned, collected.skipped, collected.runs) == (0, 1, ())
        assert not journal.with_name(ROLLUP_FILE).exists()
