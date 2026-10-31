"""A cache another reader wrote is never served, and the fold keeps the latest limit readings.

The summary cache beside a journal is read by every VibeSys that opens the run, whichever version
wrote it. A cache is valid only for the fold version that wrote it, and a record that gained
fields (``cost_basis``, ``by_model``, ``provider_session_id``) is a change of version: a cache an
older reader wrote holds the records it knew, without the fields the journal's newer lines carry,
and would be served as if it described them.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.run_summary_support import (
    EXECUTION_IDS,
    RUN_ID,
    events,
    journal_lines,
)

from server.events import (
    EventType,
    ModelTurnUsageData,
    RateLimitUpdateData,
    RunEvent,
    TurnUsageData,
)
from server.host_run_summary import RUN_SUMMARY_FILE
from server.run_summary import RUN_SUMMARY_VERSION, RunSummaryFold
from server.run_summary_cache import summarize_journal

_JOURNAL = "run-events.jsonl"
_NEWER_FIELDS = ("cost_basis", "by_model", "provider_session_id")
_FIRST_VERSION_WITHOUT_THEM = 5
"""The version hosts stamped before records gained ``_NEWER_FIELDS`` (#3534)."""


def _lines(run_events: list[RunEvent]) -> bytes:
    return b"".join(journal_lines(run_events))


def _from_scratch(journal: bytes) -> RunSummaryFold:
    fold = RunSummaryFold()
    for line in journal.splitlines(keepends=True):
        if not fold.consume(line, complete=line.endswith(b"\n")):
            break
    return fold


def _event(sequence: int, data: TurnUsageData, execution_id: str) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id=RUN_ID,
        timestamp=datetime(2026, 10, 10, 12, 0, sequence, tzinfo=UTC),
        type=EventType.TURN_USAGE_RECORDED,
        data=data,
        execution_id=execution_id,
    )


def _split_turn() -> RunEvent:
    """A turn of a newer writer: it names its basis, its models and its conversation."""
    return _event(
        1,
        TurnUsageData(
            input_tokens=100,
            output_tokens=10,
            total_cost_usd=0.5,
            cost_basis="price_table",
            by_model=(
                ModelTurnUsageData(
                    model="opus",
                    input_tokens=100,
                    cache_creation_input_tokens=0,
                    cache_creation_1h_input_tokens=0,
                    cache_read_input_tokens=0,
                    output_tokens=10,
                    reasoning_output_tokens=0,
                    turns=1,
                    recorded_usd=0.5,
                    speed=None,
                    primary=True,
                ),
            ),
            provider_session_id="conversation-1",
        ),
        EXECUTION_IDS[0],
    )


def _stamped_by_an_older_reader(directory: Path, version: int) -> None:
    """Rewrite the cache as a reader of ``version`` that did not know the newer fields wrote it."""
    cache = directory / RUN_SUMMARY_FILE
    if not cache.exists():  # a journal with no complete line is never cached
        return
    document = json.loads(cache.read_text(encoding="utf-8"))
    document["version"] = version
    for record in document["state"]["records"]:
        for name in _NEWER_FIELDS:
            record.pop(name, None)
    document["state"].pop("rate_limits", None)
    cache.write_text(json.dumps(document), encoding="utf-8")


def _summary_of(journal: bytes, *, version: int) -> tuple[Any, Any]:
    """What a read returns when the cache beside ``journal`` was stamped ``version``, and the truth."""
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        path = directory / _JOURNAL
        path.write_bytes(journal)
        summarize_journal(path)
        _stamped_by_an_older_reader(directory, version)
        return summarize_journal(path).fold.state(), _from_scratch(journal).state()


def _limit_reading(sequence: int, used: float) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id=RUN_ID,
        timestamp=datetime(2026, 10, 10, 12, 0, sequence, tzinfo=UTC),
        type=EventType.RATE_LIMIT_UPDATE,
        data=RateLimitUpdateData(
            provider="claude", window="5h", limit="primary", used_fraction=used, exhausted=False
        ),
    )


@pytest.mark.regression
@example(used=0.25, poison=0.95)
@given(
    used=st.floats(min_value=0.0, max_value=0.9), poison=st.floats(min_value=0.91, max_value=1.0)
)
def test_a_cache_the_previous_version_stamped_is_not_served_even_when_it_is_whole(
    used: float, poison: float
) -> None:
    """Only the version keeps a cache of the right shape and the wrong content from being served.

    Version 5 is what hosts stamped before the fold kept the latest limit readings. The cache
    here has every field of today's shape (so nothing but its stamp is wrong) and a reading the
    journal does not hold. A reader that took it for its own would serve that reading; the same
    cache stamped with the current version is served, which shows the reading is observable.
    """
    journal = _lines([_limit_reading(1, used)])
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / _JOURNAL
        path.write_bytes(journal)
        summarize_journal(path)
        cache = Path(raw) / RUN_SUMMARY_FILE
        document = json.loads(cache.read_text(encoding="utf-8"))
        document["state"]["rate_limits"][0]["used_fraction"] = poison

        def served(version: int) -> float | None:
            document["version"] = version
            cache.write_text(json.dumps(document), encoding="utf-8")
            return summarize_journal(path).fold.state().rate_limits[0].used_fraction

        assert served(RUN_SUMMARY_VERSION) == poison
        assert served(_FIRST_VERSION_WITHOUT_THEM) == used


@pytest.mark.regression
def test_a_cache_an_older_reader_wrote_does_not_hide_the_models_of_a_turn() -> None:
    """A version 5 cache over a journal whose turn names its models serves those models."""
    served, truth = _summary_of(_lines([_split_turn()]), version=_FIRST_VERSION_WITHOUT_THEM)
    assert truth.records[0].by_model != ()
    assert served == truth


@pytest.mark.regression
@example(run_events=[_split_turn()], version=_FIRST_VERSION_WITHOUT_THEM)
@given(
    run_events=events(max_size=25),
    version=st.integers(min_value=0, max_value=RUN_SUMMARY_VERSION + 3).filter(
        lambda each: each != RUN_SUMMARY_VERSION
    ),
)
def test_a_cache_of_any_other_fold_version_is_rebuilt_from_the_journal(
    run_events: list[RunEvent], version: int
) -> None:
    """Whatever version stamped a cache, a read of the journal returns the journal's own fold."""
    served, truth = _summary_of(_lines(run_events), version=version)
    assert served == truth


@given(run_events=events(max_size=30))
def test_the_fold_keeps_the_latest_reading_of_each_limit_window(
    run_events: list[RunEvent],
) -> None:
    """One reading per window, the latest in journal order, in the order the windows first appeared.

    The expected readings are worked out from the typed events by the contract alone: an event at
    or before the last folded sequence is skipped, a window is its provider (the event's, else its
    execution's harness), limit and window words, and a reading with no stated ``exhausted`` or
    time is not one.
    """
    starts: dict[str, str | None] = {}
    expected: dict[tuple[str | None, str | None, str | None], RateLimitUpdateData] = {}
    last = 0
    for event in run_events:
        if event.sequence <= last:
            continue
        last = event.sequence
        if event.type is EventType.AGENT_EXECUTION_STARTED and event.execution_id is not None:
            starts[event.execution_id] = getattr(event.data, "provider", None)
        elif event.type is EventType.RATE_LIMIT_UPDATE and isinstance(
            event.data, RateLimitUpdateData
        ):
            provider = event.data.provider or starts.get(event.execution_id or "")
            expected[(provider, event.data.limit, event.data.window)] = event.data
    readings = _from_scratch(_lines(run_events)).state().rate_limits
    assert [(each.provider, each.limit, each.window) for each in readings] == list(expected)
    for reading, data in zip(readings, expected.values(), strict=True):
        assert reading.exhausted == data.exhausted
        assert reading.window_minutes == data.window_minutes
        assert (
            reading.used_fraction is None
            if data.used_fraction is None
            else reading.used_fraction == data.used_fraction
        )
        assert (reading.resets_at is None) == (data.resets_at is None)


@given(run_events=events(max_size=25))
def test_the_readings_survive_the_cache(run_events: list[RunEvent]) -> None:
    """A read that restores the cache returns the readings a fold of the whole journal does."""
    journal = _lines(run_events)
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / _JOURNAL
        path.write_bytes(journal)
        summarize_journal(path)
        assert summarize_journal(path).fold.state().rate_limits == (
            _from_scratch(journal).state().rate_limits
        )
