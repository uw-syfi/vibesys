"""``runs.summary``: a run's run level, read in pages through the helper, equal to its journal's fold."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from tests.server.host_helper_support import fake_host, project_with_run
from tests.server.run_summary_support import RUN_ID, client_fold, events, journal_lines

from server.events import EventType, RunEvent, SpanStartedData
from server.host_api import (
    HELPER_LINE_MAX_BYTES,
    RUN_EVENTS_FILE,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    RunsReadEventsReply,
    RunsReadEventsRequest,
    parse_helper_message,
    parse_helper_request,
    request_features,
)
from server.host_helper_requests import answer
from server.host_run_summary import (
    RUN_SUMMARY_FILE,
    RUN_SUMMARY_PAGE_MAX_BYTES,
    RunsSummaryReply,
    RunsSummaryRequest,
    TokenTally,
    TraceFormat,
)
from server.run_usage import usage_records
from vs_core.api import Span, SpanStatus, SpanType
from vs_decode.api import Unreadable
from vs_project.api import Project
from vs_sim.api.testing import SimNetwork, SimThreads

_LEGACY_FIXTURE = (
    Path(__file__).parents[1] / "conformance" / "fixtures" / "run-summary" / "predates-trace.json"
)


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return project_with_run(tmp_path_factory.mktemp("summary") / "proj", RUN_ID)


def _journal(project: Path) -> Path:
    return Project.log_directory_for(project.resolve(), RUN_ID) / RUN_EVENTS_FILE


def _ask(project: Path, run_id: str, offset: int) -> RunsSummaryReply | HelperError:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = RunsSummaryRequest(
        request_id="s", deadline_ms=5000, project=str(project), run_id=run_id, offset=offset
    )
    reply = answer(request, host, threads.event())
    assert isinstance(reply, RunsSummaryReply | HelperError)
    return reply


def _pages(project: Path) -> list[RunsSummaryReply]:
    pages: list[RunsSummaryReply] = []
    offset: int | None = 0
    while offset is not None:
        reply = _ask(project, RUN_ID, offset)
        assert isinstance(reply, RunsSummaryReply)
        pages.append(reply)
        offset = reply.next_offset
    return pages


def _write(project: Path, run_events: list[RunEvent]) -> None:
    journal = _journal(project)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(journal_lines(run_events)))


def test_a_journal_from_before_the_trace_is_summarized_as_having_none_and_its_rounds_counted(
    tmp_path: Path,
) -> None:
    fixture = json.loads(_LEGACY_FIXTURE.read_text())
    legacy = project_with_run(tmp_path / "proj", RUN_ID)
    _journal(legacy).parent.mkdir(parents=True, exist_ok=True)
    _journal(legacy).write_bytes(
        b"".join(json.dumps(event).encode() + b"\n" for event in fixture["events"])
    )

    (page,) = _pages(legacy)

    assert (page.trace, page.rounds_finished, page.span_count) == (
        TraceFormat.PREDATES_TRACE,
        fixture["summary"]["rounds_finished"],
        0,
    )


@given(run_events=events())
def test_the_pages_of_a_summary_assemble_to_the_clients_fold_of_the_journal(
    project: Path, run_events: list[RunEvent]
) -> None:
    _write(project, run_events)
    expected = client_fold(run_events)

    pages = _pages(project)

    assert {page.through_sequence for page in pages} == {expected.through_sequence}
    assert {page.event_count for page in pages} == {expected.event_count}
    assert all(page.overview == expected.overview for page in pages)
    assert tuple(span for page in pages for span in page.spans) == expected.spans
    assert pages[0].span_count == len(expected.spans)
    assert pages[0].reply_count == len(expected.replies)
    schemas = {key: schema for page in pages for key, schema in page.schemas.items()}
    assert tuple(
        {
            "span_id": reply.span_id,
            "kind": reply.kind,
            "json_schema": schemas[reply.schema_id],
            "value": reply.value,
        }
        for page in pages
        for reply in page.replies
    ) == tuple(expected.replies)
    records = tuple(usage_records([json.loads(line) for line in journal_lines(run_events)]))
    assert pages[0].record_count == len(records)
    assert tuple(record for page in pages for record in page.records) == records
    assert pages[-1].next_offset is None
    assert all(page.journal_bytes == _journal(project).stat().st_size for page in pages)


def _long_run() -> list[RunEvent]:
    """Spans that pass one page, each with its attributes: what a run of hundreds of ideas holds."""
    return [
        RunEvent(
            sequence=index + 1,
            run_id=RUN_ID,
            timestamp=datetime(2026, 10, 10, 12, tzinfo=UTC),
            type=EventType.SPAN_STARTED,
            data=SpanStartedData(
                span=Span(
                    id=f"op:{index:06d}/gate.1",
                    parent=None,
                    type=SpanType.OPERATION,
                    name="gate " + "x" * 400,
                    status=SpanStatus.DONE,
                    started_at=float(index),
                )
            ),
        )
        for index in range(3000)
    ]


def test_every_page_of_a_long_run_is_one_helper_line_and_the_pages_cover_it(
    project: Path,
) -> None:
    run_events = _long_run()
    _write(project, run_events)

    pages = _pages(project)

    assert len(pages) > 2
    for page in pages:
        assert len(page.model_dump_json()) < HELPER_LINE_MAX_BYTES
        assert len(page.model_dump_json()) < RUN_SUMMARY_PAGE_MAX_BYTES * 1.2
    assert tuple(span for page in pages for span in page.spans) == client_fold(run_events).spans


def test_the_events_after_a_summary_are_the_live_tail(project: Path) -> None:
    _write(project, _long_run()[:5])
    cursor = _pages(project)[0].through_sequence
    _write(project, _long_run()[:8])

    summary = _pages(project)[0]
    threads = SimThreads()
    tail = answer(
        RunsReadEventsRequest(
            request_id="t",
            deadline_ms=5000,
            project=str(project),
            run_id=RUN_ID,
            from_sequence=cursor + 1,
            max=100,
        ),
        fake_host(threads, SimNetwork(threads)),
        threads.event(),
    )

    assert isinstance(tail, RunsReadEventsReply)
    assert [event["sequence"] for event in tail.events] == [6, 7, 8]
    assert (cursor, summary.through_sequence) == (5, 8)


def test_a_run_recorded_with_no_journal_here_has_no_summary_not_an_empty_one(
    tmp_path: Path,
) -> None:
    """Its journal is in another project's log store, say: an empty summary would call it an empty run."""
    project = project_with_run(tmp_path / "proj", RUN_ID)

    reply = _ask(project, RUN_ID, 0)

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNKNOWN_RUN
    assert RUN_ID in reply.message


def test_a_journal_from_before_the_trace_is_summarized_as_one(tmp_path: Path) -> None:
    fixture = json.loads(_LEGACY_FIXTURE.read_text())
    run_id = fixture["events"][0]["run_id"]
    project = project_with_run(tmp_path / "proj", run_id)
    journal = Project.log_directory_for(project.resolve(), run_id) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(json.dumps(event).encode() + b"\n" for event in fixture["events"]))

    reply = _ask(project, run_id, 0)

    assert isinstance(reply, RunsSummaryReply)
    assert reply.trace is TraceFormat.PREDATES_TRACE
    assert reply.rounds_finished == fixture["summary"]["rounds_finished"]


def test_an_unknown_run_is_refused_naming_it(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)

    reply = _ask(project, "20261010-120000-22222222-other", 0)

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNKNOWN_RUN
    assert "20261010-120000-22222222-other" in reply.message


def test_the_summary_is_cached_beside_the_journal_and_the_journal_is_not_touched(
    project: Path,
) -> None:
    run_events = _long_run()[:50]
    _write(project, run_events)
    journal = _journal(project)
    before = journal.read_bytes()

    _pages(project)

    assert (journal.parent / RUN_SUMMARY_FILE).is_file()
    assert journal.read_bytes() == before


def test_the_method_needs_its_feature_and_the_messages_are_strict() -> None:
    request = RunsSummaryRequest(
        request_id="s", deadline_ms=5000, project="/p", run_id=RUN_ID, offset=0
    )
    line = request.model_dump_json().encode()

    # The reply names ``trace``: a client from before it (``run_summary`` alone) is refused.
    assert request_features(request) == (HelperFeature.RUN_TRACE, HelperFeature.RUN_SUMMARY)
    assert parse_helper_request(line) == request
    unknown = json.loads(line) | {"unexpected": 1}
    assert isinstance(parse_helper_request(json.dumps(unknown).encode()), Unreadable)

    reply = RunsSummaryReply(
        request_id="s",
        through_sequence=0,
        event_count=0,
        journal_bytes=0,
        trace=TraceFormat.RECORDED,
        rounds_finished=0,
        span_count=0,
        reply_count=0,
        usage_count=0,
        record_count=0,
        tokens=TokenTally(
            input=None, cache_read=None, cache_write=None, output=None, reasoning=None
        ),
        overview=None,
        spans=(),
        replies=(),
        schemas={},
        usage=(),
        records=(),
        next_offset=None,
    )
    assert parse_helper_message(reply.model_dump_json().encode()) == reply
    extra = json.loads(reply.model_dump_json()) | {"unexpected": 1}
    assert isinstance(parse_helper_message(json.dumps(extra).encode()), Unreadable)
    # A format this helper does not know is refused, never read as a recorded trace.
    future = json.loads(reply.model_dump_json()) | {"trace": "from_the_future"}
    assert isinstance(parse_helper_message(json.dumps(future).encode()), Unreadable)
