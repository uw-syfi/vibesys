"""``runs.summary``: a run's run level, read in pages through the helper, equal to its journal's fold."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from tests.server.host_helper_support import fake_host, project_with_run
from tests.server.run_summary_support import RUN_ID, client_fold, events, journal_lines

from server.events import (
    EventStatus,
    EventType,
    GateFinishedData,
    RunEvent,
    SpanStartedData,
)
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
    INPUT_GATE_TAIL_MAX_CHARS,
    INPUT_GATES_MAX,
    RUN_SUMMARY_FILE,
    RUN_SUMMARY_PAGE_MAX_BYTES,
    RUN_TERMINAL_MESSAGE_MAX_CHARS,
    InputGate,
    RunsSummaryReply,
    RunsSummaryRequest,
    RunTerminalKind,
    TokenTally,
    TraceFormat,
)
from server.run_usage import usage_records
from vibesys.api import GateKind
from vs_core.api import Span, SpanLabel, SpanStatus, SpanType
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


def _ask(
    project: Path, run_id: str, offset: int, *, with_ending: bool | None = True
) -> RunsSummaryReply | HelperError:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = RunsSummaryRequest(
        request_id="s",
        deadline_ms=5000,
        project=str(project),
        run_id=run_id,
        offset=offset,
        with_ending=with_ending,
    )
    reply = answer(request, host, threads.event())
    assert isinstance(reply, RunsSummaryReply | HelperError)
    return reply


def _pages(project: Path, *, with_ending: bool | None = True) -> list[RunsSummaryReply]:
    pages: list[RunsSummaryReply] = []
    offset: int | None = 0
    while offset is not None:
        reply = _ask(project, RUN_ID, offset, with_ending=with_ending)
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
    assert all(
        tuple(gate.model_dump(mode="json") for gate in _gates(page)) == expected.input_gates
        for page in pages
    )
    assert all(
        (None if page.terminal is None else page.terminal.model_dump(mode="json"))
        == expected.terminal
        for page in pages
    )
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

    # The reply names ``input_gates`` and ``terminal``: a client from before them (``run_summary`` alone) is refused.
    assert request_features(request) == (HelperFeature.RUN_TRACE, HelperFeature.RUN_SUMMARY)
    assert parse_helper_request(line) == request
    # Asking for the ending needs ``run_ending``: a helper without it refuses before it answers.
    ending = request.model_copy(update={"with_ending": True})
    assert request_features(ending) == (
        HelperFeature.RUN_TRACE,
        HelperFeature.RUN_SUMMARY,
        HelperFeature.RUN_ENDING,
    )
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
        input_gates=(),
        terminal=None,
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
    # An input gate or a terminal event with a key it does not declare is refused too.
    gate = {"sequence": 1, "gate": "build", "recipe": None, "passed": True, "output_tail": None}
    base = json.loads(reply.model_dump_json())
    ok = base | {"input_gates": [gate], "terminal": None}
    assert isinstance(parse_helper_message(json.dumps(ok).encode()), RunsSummaryReply)
    stray = base | {"input_gates": [gate | {"unexpected": 1}]}
    assert isinstance(parse_helper_message(json.dumps(stray).encode()), Unreadable)
    ended = {"sequence": 2, "kind": "exploded", "message": None}
    assert isinstance(
        parse_helper_message(json.dumps(base | {"terminal": ended}).encode()), Unreadable
    )


def _gates(page: RunsSummaryReply) -> tuple[InputGate, ...]:
    assert page.input_gates is not None
    return page.input_gates


def _event(
    sequence: int,
    kind: EventType,
    *,
    text: str = "",
    data: GateFinishedData | SpanStartedData | None = None,
    status: EventStatus | None = None,
) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id=RUN_ID,
        timestamp=datetime(2026, 10, 10, 12, tzinfo=UTC),
        type=kind,
        text=text,
        data=data,
        status=status,
    )


def _gate(sequence: int, gate: GateKind, tail: str | None, *, passed: bool = False) -> RunEvent:
    return _event(
        sequence,
        EventType.GATE_FINISHED,
        data=GateFinishedData(gate=gate, output_tail=tail),
        status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
    )


def _idea(sequence: int) -> RunEvent:
    span = Span(
        id="flow:000001/#1",
        parent=None,
        type=SpanType.FLOW,
        name="idea",
        status=SpanStatus.RUNNING,
        started_at=1.0,
        label=SpanLabel(kind="workstream"),
    )
    return _event(sequence, EventType.SPAN_STARTED, data=SpanStartedData(span=span))


def test_a_run_whose_input_gates_failed_and_that_crashed_is_summarized_with_both(
    tmp_path: Path,
) -> None:
    whole = ("NestedRepositoryError: " + "a very long reason; " * 40).rstrip()
    crashed = project_with_run(tmp_path / "proj", RUN_ID)
    _write(
        crashed,
        [
            _gate(1, GateKind.ACCURACY, "No module named 'engine'\n"),
            _gate(2, GateKind.BUILD, None, passed=True),
            _idea(3),
            _gate(4, GateKind.BENCHMARK, "a gate under an idea"),
            _event(5, EventType.RUN_FAILED, text=whole),
        ],
    )

    pages = _pages(crashed)

    assert len(whole) > 200
    # A client that did not ask (one from before ``run_ending``) gets the reply it always did.
    (plain,) = _pages(crashed, with_ending=None)
    assert "input_gates" not in plain.model_dump_json()
    assert "terminal" not in plain.model_dump_json()
    for page in pages:
        assert [(gate.sequence, gate.gate, gate.passed) for gate in _gates(page)] == [
            (1, "accuracy", False),
            (2, "build", True),
        ]
        assert _gates(page)[0].output_tail == "No module named 'engine'\n"
        assert page.terminal is not None
        assert (page.terminal.kind, page.terminal.message) == (RunTerminalKind.FAILED, whole)


def test_a_gate_tail_and_a_failure_message_are_bounded_and_credentials_are_redacted(
    tmp_path: Path,
) -> None:
    bounded = project_with_run(tmp_path / "proj", RUN_ID)
    tail = "x" * (INPUT_GATE_TAIL_MAX_CHARS + 500) + " the end"
    secret = _gate(INPUT_GATES_MAX + 2, GateKind.BUILD, "API_TOKEN=hunter2 failed")
    _write(
        bounded,
        [
            _gate(1, GateKind.ACCURACY, tail),
            *[_gate(n, GateKind.BUILD, None) for n in range(2, INPUT_GATES_MAX + 1)],
            secret,
            _event(
                INPUT_GATES_MAX + 3,
                EventType.RUN_FAILED,
                text="y" * (RUN_TERMINAL_MESSAGE_MAX_CHARS + 10),
            ),
        ],
    )

    (page,) = _pages(bounded)

    assert len(_gates(page)) == INPUT_GATES_MAX
    first = _gates(page)[0].output_tail
    assert first is not None
    assert len(first) == INPUT_GATE_TAIL_MAX_CHARS
    assert first.endswith("the end")
    assert page.terminal is not None
    assert page.terminal.message is not None
    assert len(page.terminal.message) == RUN_TERMINAL_MESSAGE_MAX_CHARS
    assert page.terminal.message.endswith("\u2026")
    assert "hunter2" not in page.model_dump_json()


def test_a_run_that_has_not_ended_has_no_terminal_and_the_last_terminal_event_wins(
    tmp_path: Path,
) -> None:
    ending = project_with_run(tmp_path / "proj", RUN_ID)
    _write(ending, [_gate(1, GateKind.ACCURACY, None)])
    (running,) = _pages(ending)
    _write(
        ending,
        [
            _gate(1, GateKind.ACCURACY, None),
            _event(2, EventType.RUN_INTERRUPTED, text="stopped by the operator"),
            _event(3, EventType.RUN_FINISHED, text=""),
        ],
    )
    (done,) = _pages(ending)

    assert running.terminal is None
    assert done.terminal is not None
    assert (done.terminal.kind, done.terminal.message) == (RunTerminalKind.FINISHED, None)
