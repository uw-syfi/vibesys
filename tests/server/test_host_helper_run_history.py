"""``runs.read_events``: a run's recorded events, read from its journal in bounded pages."""

from __future__ import annotations

import io
import itertools
import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host, project_with_run

from server.host_api import (
    RUN_EVENTS_FILE,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    HelperRequest,
    RunsEventsForSpanRequest,
    RunsReadEventsReply,
    RunsReadEventsRequest,
    RunsReadTailRequest,
    request_features,
)
from server.host_examples import ProjectLookupError
from server.host_helper_os import FileProjects, JournalTally, tally_journal
from server.host_helper_requests import answer, event_page
from server.host_run_summary import RUN_SUMMARY_FILE, RunsSummaryRequest
from vs_project.api import GitFaultSink, GitRepository, Project
from vs_project.api.testing import FakeGitRepositories
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

_RUN_ID = "20261010-120000-11111111-dynamic"


def _event(sequence: int, text: str = "") -> dict[str, JsonValue]:
    return {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": _RUN_ID,
        "type": "output",
        "text": text,
        "data": {"nested": {"sequence": 0}},
    }


def _lines(events: list[dict[str, JsonValue]]) -> list[bytes]:
    return [json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events]


_JOURNALS = st.lists(st.text(max_size=40), max_size=30).map(
    lambda texts: [_event(index + 1, text) for index, text in enumerate(texts)]
)


@given(
    journal=_JOURNALS,
    limit=st.integers(1, 8),
    max_bytes=st.integers(1, 600),
    start=st.integers(1, 5),
)
def test_pages_of_any_size_concatenate_to_the_journal_from_where_reading_began(
    journal: list[dict[str, JsonValue]], limit: int, max_bytes: int, start: int
) -> None:
    lines = _lines(journal)
    read: list[dict[str, JsonValue]] = []
    sequence = start
    for _page in range(len(journal) + 2):
        page = event_page(lines, sequence, limit, max_bytes)
        assert 0 < len(page.events) <= limit or page.complete
        assert len(page.events) <= 1 or len(b"".join(_lines(list(page.events)))) <= max_bytes
        read.extend(page.events)
        sequence = page.next_sequence
        if page.complete:
            break
    assert page.complete
    assert read == journal[start - 1 :]  # sequence n is the journal's n-th event


def test_a_page_never_passes_its_byte_bound_unless_one_event_alone_does() -> None:
    lines = _lines([_event(1, "a" * 50), _event(2, "b" * 50), _event(3, "c" * 500)])
    one = len(lines[0])

    assert [e["sequence"] for e in event_page(lines, 1, 10, 2 * one).events] == [1, 2]
    assert [e["sequence"] for e in event_page(lines, 3, 10, one).events] == [3]


def test_an_unfinished_last_line_ends_the_journal_before_it() -> None:
    lines = [*_lines([_event(1), _event(2)]), b'{"protocol_version":1,"sequence":3,"ty']

    page = event_page(lines, 1, 10, 1 << 20)

    assert [e["sequence"] for e in page.events] == [1, 2]
    assert (page.next_sequence, page.complete) == (3, True)


def _read(project: Path, run_id: str, from_sequence: int, limit: int) -> object:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = RunsReadEventsRequest(
        request_id="h",
        deadline_ms=5000,
        project=str(project),
        run_id=run_id,
        from_sequence=from_sequence,
        max=limit,
    )
    return answer(request, host, threads.event())


def test_an_ended_runs_journal_is_read_through_the_helper(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", _RUN_ID)
    journal = Project.log_directory_for(project.resolve(), _RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(_lines([_event(1), _event(2), _event(3)])))

    first = _read(project, _RUN_ID, 1, 2)
    rest = _read(project, _RUN_ID, 3, 2)

    assert first == RunsReadEventsReply(
        request_id="h", events=(_event(1), _event(2)), next_sequence=3, complete=False
    )
    assert rest == RunsReadEventsReply(
        request_id="h", events=(_event(3),), next_sequence=4, complete=True
    )


def _history_requests(project: Path, run_id: str) -> list[HelperRequest]:
    return [
        RunsReadEventsRequest(
            request_id="h",
            deadline_ms=5000,
            project=str(project),
            run_id=run_id,
            from_sequence=1,
            max=5,
        ),
        RunsReadTailRequest(
            request_id="h", deadline_ms=5000, project=str(project), run_id=run_id, tail=5
        ),
        RunsEventsForSpanRequest(
            request_id="h",
            deadline_ms=5000,
            project=str(project),
            run_id=run_id,
            span_id="flow:a",
            from_sequence=1,
            max=5,
        ),
        RunsSummaryRequest(request_id="h", deadline_ms=5000, project=str(project), run_id=run_id),
    ]


@pytest.mark.parametrize(
    ("recorded", "journaled", "cached"), list(itertools.product([True, False], repeat=3))
)
def test_every_history_read_of_a_run_missing_any_piece_is_typed_never_empty_or_silent(
    tmp_path: Path, *, recorded: bool, journaled: bool, cached: bool
) -> None:
    """A run is readable only with its record and its journal; otherwise every read says why.

    Every combination of the pieces a read needs (the run's record, its journal, the summary cache
    beside it). Regression: a run its project records but whose journal this machine does not hold
    answered an empty, complete page to events, tail and span reads (a run with no events), while
    its summary refused it, so a client without summaries opened it as an empty run.
    """
    project = project_with_run(tmp_path / "proj", _RUN_ID)
    directory = Project.log_directory_for(project.resolve(), _RUN_ID)
    if journaled:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / RUN_EVENTS_FILE).write_bytes(b"".join(_lines([_event(1), _event(2)])))
        if cached:
            (directory / RUN_SUMMARY_FILE).write_text("not a cache")
    run_id = _RUN_ID if recorded else "20261010-120000-22222222-other"
    for request in _history_requests(project, run_id):
        threads = SimThreads()
        reply = answer(request, fake_host(threads, SimNetwork(threads)), threads.event())
        if recorded and journaled:
            assert not isinstance(reply, HelperError), reply
        else:
            assert isinstance(reply, HelperError), (request.type, reply)
            assert reply.code is HelperErrorCode.UNKNOWN_RUN
            assert run_id in reply.message


def test_an_unknown_run_is_refused_naming_it(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", _RUN_ID)

    reply = _read(project, "20261010-120000-22222222-other", 1, 5)

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNKNOWN_RUN
    assert "20261010-120000-22222222-other" in reply.message


class _CountingRepositories:
    """A ``GitRepositoryFactory`` over the Fake that counts the repositories opened."""

    def __init__(self) -> None:
        self.opened = 0
        self._fake = FakeGitRepositories()

    def __call__(self, root: Path, faults: GitFaultSink) -> GitRepository:
        self.opened += 1
        return self._fake.repository(root, faults)


def test_one_channel_looks_a_run_up_once_however_many_pages_it_reads(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", _RUN_ID)
    journal = Project.log_directory_for(project.resolve(), _RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"".join(_lines([_event(n) for n in range(1, 8)])))
    repositories = _CountingRepositories()
    projects = FileProjects(repositories, SimThreads())

    for from_sequence in (1, 3, 5, 7):
        projects.run_events(project, _RUN_ID, from_sequence, 2, with_totals=True)

    assert repositories.opened == 1


def test_a_run_not_found_is_looked_up_again_so_one_that_starts_later_is_found(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", _RUN_ID)
    repositories = _CountingRepositories()
    projects = FileProjects(repositories, SimThreads())
    other = "20261010-120000-22222222-other"

    for _attempt in range(2):
        with pytest.raises(ProjectLookupError) as refused:
            projects.run_events(project, other, 1, 2, with_totals=False)
        assert refused.value.code is HelperErrorCode.UNKNOWN_RUN

    assert repositories.opened == 2


def test_totals_are_named_only_for_a_request_that_asked_for_them(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", _RUN_ID)
    journal = Project.log_directory_for(project.resolve(), _RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(_lines([_event(1), _event(2), _event(3)]))
    journal.write_bytes(body)
    projects = FileProjects(FakeGitRepositories().repository, SimThreads())

    plain = projects.run_events(project, _RUN_ID, 1, 2, with_totals=False)
    totals = projects.run_events(project, _RUN_ID, 1, 2, with_totals=True)

    assert (plain.total_events, plain.total_bytes) == (None, None)
    assert (totals.total_events, totals.total_bytes) == (3, len(body))
    reply = RunsReadEventsReply(
        request_id="h", events=(), next_sequence=1, complete=True,
        total_events=plain.total_events, total_bytes=plain.total_bytes,
    )  # fmt: skip
    # An older desktop validates replies strictly, so an unasked-for total is absent, not null.
    assert "total_" not in reply.model_dump_json()


def test_asking_for_totals_needs_the_page_feature() -> None:
    request = RunsReadEventsRequest(
        request_id="h", deadline_ms=5000, project="p", run_id=_RUN_ID,
        from_sequence=1, max=2, with_totals=True,
    )  # fmt: skip
    bare = request.model_copy(update={"with_totals": None})

    assert HelperFeature.RUN_HISTORY_PAGES in request_features(request)
    assert HelperFeature.RUN_HISTORY_PAGES not in request_features(bare)


@given(
    appended=st.lists(st.binary(max_size=60), max_size=12),
)
def test_a_tally_taken_as_a_journal_grows_equals_a_count_of_the_whole(
    appended: list[bytes],
) -> None:
    content = b""
    tally = JournalTally(0, 0)
    for chunk in appended:
        content += chunk
        tally = tally_journal(io.BytesIO(content), tally)
        complete = content[: content.rfind(b"\n") + 1]
        assert tally == JournalTally(len(complete), complete.count(b"\n"))
    assert tally == tally_journal(io.BytesIO(content), JournalTally(0, 0))


@given(content=st.binary(max_size=80), stale=st.integers(0, 200), lines=st.integers(0, 50))
def test_a_tally_for_a_longer_journal_than_the_file_counts_afresh(
    content: bytes, stale: int, lines: int
) -> None:
    fresh = tally_journal(io.BytesIO(content), JournalTally(0, 0))
    if stale > len(content):
        assert tally_journal(io.BytesIO(content), JournalTally(stale, lines)) == fresh
