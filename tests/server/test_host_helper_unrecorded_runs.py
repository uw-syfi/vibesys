"""A run is found by its own identity: a journal in its project's log store names it.

A run that stopped at start-up leaves its journal in the machine's log store but no manifest
in the checkout or on a run branch. Two checkouts of one project name keep separate stores,
so each run is named at the checkout whose store holds it and nowhere else.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import RUN_ID, fake_host, project_with_run

from server.host_api import (
    RUN_EVENTS_FILE,
    HelperError,
    HelperErrorCode,
    RunDigest,
    RunsListReply,
    RunsListRequest,
    RunsReadEventsReply,
    RunsReadEventsRequest,
)
from server.host_helper_requests import answer
from vs_project.api import Project
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

_CHECKOUTS = itertools.count()
_STARTED = "2026-10-16T19:59:31.479201+00:00"


def _unrecorded(project: Path, run_id: str, started: str = _STARTED) -> None:
    """Journal ``run_id`` in ``project``'s log store with no manifest anywhere."""
    journal = Project.log_directory_for(project.resolve(), run_id) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    event = {"sequence": 1, "run_id": run_id, "timestamp": started, "type": "run_started"}
    journal.write_bytes(json.dumps(event).encode() + b"\n")


def _ask(project: Path, request_type: str, run_id: str | None = None) -> object:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    if request_type == "list":
        request: RunsListRequest | RunsReadEventsRequest = RunsListRequest(
            request_id="a",
            deadline_ms=5000,
            project=str(project),
            digest_runs=None if run_id is None else (run_id,),
        )
    else:
        assert run_id is not None
        request = RunsReadEventsRequest(
            request_id="a",
            deadline_ms=5000,
            project=str(project),
            run_id=run_id,
            from_sequence=1,
            max=5,
        )
    return answer(request, host, threads.event())


def test_a_run_with_a_journal_and_no_manifest_is_listed_digested_and_readable(
    tmp_path: Path,
) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)
    stopped = "20261016-195926-83eaab12-proj-20261016-195926"
    _unrecorded(project, stopped)

    listed = _ask(project, "list", stopped)
    read = _ask(project, "read", stopped)

    assert isinstance(listed, RunsListReply)
    by_id = {run.run_id: run for run in listed.runs.runs}
    assert set(by_id) == {RUN_ID, stopped}
    assert by_id[stopped].created_at == datetime(2026, 10, 16, 19, 59, 31, 479201, tzinfo=UTC)
    assert isinstance(by_id[stopped].digest, RunDigest)
    assert isinstance(read, RunsReadEventsReply)
    assert [event["sequence"] for event in read.events] == [1]


def test_two_checkouts_of_one_project_name_each_find_only_the_runs_their_store_holds(
    tmp_path: Path,
) -> None:
    (tmp_path / "e2e").mkdir()
    (tmp_path / "projects").mkdir()
    first = project_with_run(tmp_path / "e2e" / "queue", RUN_ID)
    second = project_with_run(tmp_path / "projects" / "queue", RUN_ID)
    here, there = "20261016-195926-83eaab12-queue-20261016-195926", "20261016-042258-7e01f20f-queue"
    _unrecorded(first, here)
    _unrecorded(second, there)

    assert isinstance(_ask(first, "read", here), RunsReadEventsReply)
    assert isinstance(_ask(second, "read", there), RunsReadEventsReply)
    for project, other in ((first, there), (second, here)):
        refused = _ask(project, "read", other)
        assert isinstance(refused, HelperError)
        assert refused.code is HelperErrorCode.UNKNOWN_RUN


def test_a_run_with_no_journal_and_no_manifest_is_unknown(tmp_path: Path) -> None:
    project = project_with_run(tmp_path / "proj", RUN_ID)

    refused = _ask(project, "read", "20261016-195926-83eaab12-proj-20261016-195926")

    assert isinstance(refused, HelperError)
    assert refused.code is HelperErrorCode.UNKNOWN_RUN


_IDS = st.sets(st.integers(0, 59).map(lambda n: f"20261016-1959{n:02}-83eaab12-proj-1"), max_size=6)


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(ids=_IDS)
def test_every_journaled_run_is_listed_once_after_the_recorded_older_one(
    tmp_path: Path, ids: set[str]
) -> None:
    project = project_with_run(tmp_path / f"proj-{next(_CHECKOUTS)}", RUN_ID)
    for run_id in ids:
        _unrecorded(project, run_id)

    listed = _ask(project, "list")

    assert isinstance(listed, RunsListReply)
    assert [run.run_id for run in listed.runs.runs] == [RUN_ID, *sorted(ids)]


_EPOCH_SECONDS = st.integers(min_value=946_684_800, max_value=4_102_444_800)  # 2000 to 2100
_OFFSETS = st.integers(min_value=-14 * 60, max_value=14 * 60).map(
    lambda minutes: timezone(timedelta(minutes=minutes))
)
_STAMPS = st.one_of(
    _EPOCH_SECONDS.map(lambda seconds: datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)),
    st.builds(datetime.fromtimestamp, _EPOCH_SECONDS, _OFFSETS),
).map(lambda moment: moment.isoformat())


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(stamps=st.lists(_STAMPS, min_size=1, max_size=5))
def test_journaled_runs_with_or_without_a_utc_offset_list_with_aware_times_in_order(
    tmp_path: Path, stamps: list[str]
) -> None:
    project = project_with_run(tmp_path / f"proj-{next(_CHECKOUTS)}", RUN_ID)
    for index, stamp in enumerate(stamps):
        _unrecorded(project, f"20261016-195926-83eaab12-proj-{index}", stamp)

    listed = _ask(project, "list")

    assert isinstance(listed, RunsListReply)
    times = [run.created_at for run in listed.runs.runs]
    assert all(moment.tzinfo is not None for moment in times)
    assert times == sorted(times)
