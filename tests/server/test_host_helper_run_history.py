"""``runs.read_events``: a run's recorded events, read from its journal in bounded pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host
from tests.support.task_project import write_task

from server.host_api import (
    RUN_EVENTS_FILE,
    HelperError,
    HelperErrorCode,
    RunsReadEventsReply,
    RunsReadEventsRequest,
)
from server.host_helper_requests import answer, event_page
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
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


def _project_with_run(root: Path) -> Path:
    root.mkdir()
    write_task(root, 'version = 1\n[agent]\ndomain = "generic"\n', objective="Faster.\n")
    project = Project.open(root)
    project.state.create_project(root.name, now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        root.name,
        run_id=_RUN_ID,
        branch=f"vibesys-runs/{_RUN_ID}",
        vibesys_version="0.2.0-test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="dynamic",
            config_version=1,
            options=DynamicLaunch(interface="service", max_rounds=2, judge_every=2).model_dump(
                mode="json", exclude={"profiling"}
            ),
        ),
        trusted_input_baseline="0" * 40,
        now=datetime(2026, 10, 10, 12, tzinfo=UTC),
    )
    project.state.create_run(manifest, make_current=True)
    return root


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
    project = _project_with_run(tmp_path / "proj")
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


def test_a_run_that_journaled_nothing_has_no_events(tmp_path: Path) -> None:
    project = _project_with_run(tmp_path / "proj")

    assert _read(project, _RUN_ID, 1, 5) == RunsReadEventsReply(
        request_id="h", events=(), next_sequence=1, complete=True
    )


def test_an_unknown_run_is_refused_naming_it(tmp_path: Path) -> None:
    project = _project_with_run(tmp_path / "proj")

    reply = _read(project, "20261010-120000-22222222-other", 1, 5)

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNKNOWN_RUN
    assert "20261010-120000-22222222-other" in reply.message
