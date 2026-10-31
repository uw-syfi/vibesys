"""A registered read's git work does not grow with the project's runs.

The flaw this holds shut: a read that names a few runs scanned every run of the project first, so
a project's size was paid on every call, once per page or once per requested run. Every registered
read runs here over a project of N runs and of ten times N, and must ask git the same things. A
new read lands here by being registered. No time is measured: the calls the host makes on its
repositories are counted.

The one declared exception is the listing, which visits every run. It is a stream, not a read, and
it is held to one pass: one look at the branches, and a bounded number of calls per run. A new
stream lands here by being registered too: it must be named in ``_STREAMS`` with what it may ask git.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from tests.server.host_helper_support import fake_host, write_task

from server.host_api import RUN_EVENTS_FILE
from server.host_helper_os import FileProjects
from server.host_helper_requests import HelperHost, registry_read_source
from server.host_operation_reads import answer_registered_read, stream_saved_runs
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_project.api.testing import FakeGitRepositories, run_execution_record
from vs_proto.api import (
    Oneof,
    ReadRequest,
    ReadResponse,
    Timestamp,
    WatchRequest,
    WatchResponse,
    host_operations,
    validate,
)
from vs_proto.api import host_payloads as pb
from vs_sim.api.testing import ManualClock, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import GitFaultSink, GitRepository

_SMALL = 6
_LARGE = 10 * _SMALL
_NAMED = tuple(f"run-{index:04d}" for index in range(3))
_PER_RUN_CALLS = 3
"""What the listing may ask git per run: its manifest's branch and contents."""
_SHARED_CALLS = 8
"""What the listing may ask git once: the checkout, and the list of branches."""


class _CountingRepository:
    """A repository that counts each call to the one it wraps."""

    def __init__(self, repository: GitRepository, calls: Counter[str]) -> None:
        self._repository = repository
        self._calls = calls

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._repository, name)
        if not callable(attribute):
            return attribute

        def counted(*args: object, **kwargs: object) -> object:
            self._calls[name] += 1
            return attribute(*args, **kwargs)

        return counted


def _project(root: Path, runs: int, repositories: FakeGitRepositories) -> Path:
    """A project of ``runs`` runs, each committed on its own branch and with a journal."""
    root.mkdir()
    write_task(root, 'version = 1\n[agent]\ndomain = "generic"\n', objective="Faster.\n")
    repository = repositories.repository(root)
    repository.initialize(initial_branch="main")
    project = Project.open(root)
    project.state.create_project(root.name, now=datetime(2026, 1, 1, tzinfo=UTC))
    repository.stage_all(["."], force=True)
    repository.commit("baseline", allow_empty=True)
    for index in range(runs):
        run_id = f"run-{index:04d}"
        repository.switch_branch(f"vibesys-runs/{run_id}", create=True)
        project.state.create_run(
            project.state.new_run_manifest(
                root.name,
                run_id=run_id,
                branch=f"vibesys-runs/{run_id}",
                vibesys_version="0.2.0-test",
                run_environment=RunEnvironmentRecord(name="docker"),
                execution=run_execution_record(),
                orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
                trusted_input_baseline="0" * 40,
                now=datetime(2026, 10, 10, 12, tzinfo=UTC),
            ),
            make_current=False,
        )
        repository.stage_all(["."], force=True)
        repository.commit(f"vibesys: record run {run_id}")
        repository.switch_branch("main")
        journal = Project.log_directory_for(root.resolve(), run_id) / RUN_EVENTS_FILE
        journal.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "protocol_version": 1,
            "sequence": 1,
            "run_id": run_id,
            "timestamp": "2026-10-10T12:00:00+00:00",
            "type": "run_started",
            "text": "",
            "data": {"kind": "run_started", "overview": {"objective": {"direction": "max"}}},
        }
        journal.write_bytes(json.dumps(event).encode() + b"\n")
    return root


def _envelope(request: ReadRequest) -> ReadRequest:
    """``request`` as the gateway would send it: addressed to a checkout, and valid."""
    request.checkout = "/checkout"
    validate(request)
    return request


_REQUESTS: dict[str, Callable[[str], ReadRequest]] = {
    "list_tasks": lambda project: ReadRequest(
        method=Oneof("list_tasks", pb.ListTasksRequest(request_id="r", project=project))
    ),
    "list_harnesses": lambda _project: ReadRequest(
        method=Oneof("list_harnesses", pb.ListHarnessesRequest(request_id="r"))
    ),
    "list_examples": lambda _project: ReadRequest(
        method=Oneof("list_examples", pb.ListExamplesRequest(request_id="r"))
    ),
    "get_task_goal": lambda project: ReadRequest(
        method=Oneof(
            "get_task_goal",
            pb.GetTaskGoalRequest(request_id="r", project=project, task="default"),
        )
    ),
    "describe_machine": lambda _project: ReadRequest(
        method=Oneof("describe_machine", pb.DescribeMachineRequest(request_id="r"))
    ),
    "list_compute": lambda _project: ReadRequest(
        method=Oneof("list_compute", pb.ListComputeRequest(request_id="r"))
    ),
    "preflight_run": lambda project: ReadRequest(
        method=Oneof("preflight_run", pb.PreflightRunRequest(request_id="r", project=project))
    ),
    "inspect_destination": lambda project: ReadRequest(
        method=Oneof(
            "inspect_destination",
            pb.InspectDestinationRequest(request_id="r", example="absent", destination=project),
        )
    ),
    "list_agent_logins": lambda _project: ReadRequest(
        method=Oneof("list_agent_logins", pb.ListAgentLoginsRequest(request_id="r"))
    ),
    "list_run_digests": lambda project: ReadRequest(
        method=Oneof(
            "list_run_digests",
            pb.ListRunDigestsRequest(request_id="r", project=project, run_ids=list(_NAMED)),
        )
    ),
    "usage_window": lambda project: ReadRequest(
        method=Oneof(
            "usage_window",
            pb.UsageWindowRequest(
                request_id="r",
                project=project,
                run_ids=list(_NAMED),
                since=Timestamp.from_seconds(1_790_000_000),
                until=Timestamp.from_seconds(1_790_086_400),
            ),
        )
    ),
}


_STREAMS = {"login", "stream_saved_runs"}
"""The registered streams: login asks git nothing, and the listing is held to one pass below."""


def test_every_registered_stream_is_accounted_for_here() -> None:
    assert set(host_operations.WATCHES) == _STREAMS, (
        "a new stream scans a project or it does not: say which, and test it beside the listing"
    )


def _host(repositories: FakeGitRepositories, calls: Counter[str]) -> HelperHost:
    def counting(root: Path, faults: GitFaultSink) -> GitRepository:
        calls["repositories opened"] += 1
        repository = repositories.repository(root, faults)
        return cast("GitRepository", _CountingRepository(repository, calls))

    threads = SimThreads()
    return replace(
        fake_host(threads, SimNetwork(threads)),
        projects=FileProjects(counting, threads, clock=ManualClock()),
    )


def _git_work(tmp_path: Path, slot: str, runs: int) -> tuple[Counter[str], ReadResponse]:
    """What git was asked, and the reply, for ``slot`` over a project of ``runs`` runs."""
    repositories = FakeGitRepositories()
    project = _project(tmp_path / f"project-{runs}", runs, repositories)
    calls: Counter[str] = Counter()
    request = _envelope(_REQUESTS[slot](str(project)))
    reply = answer_registered_read(
        slot.replace("_", "-"),
        request.to_binary(),
        registry_read_source(_host(repositories, calls)),
    )
    return calls, ReadResponse.from_binary(reply)


@pytest.mark.parametrize("slot", sorted(host_operations.READS))
def test_no_registered_read_asks_git_more_of_a_project_with_more_runs(
    tmp_path: Path, slot: str
) -> None:
    assert slot in _REQUESTS, f"name a request for the registered read {slot!r} in _REQUESTS"

    small, _reply = _git_work(tmp_path, slot, _SMALL)
    large, reply = _git_work(tmp_path, slot, _LARGE)

    assert large == small, f"{slot}: git work grew from {_SMALL} runs to {_LARGE} runs"
    assert reply.result is not None


@pytest.mark.parametrize("slot", ["list_run_digests", "usage_window"])
def test_the_reads_that_name_runs_answer_for_those_runs(tmp_path: Path, slot: str) -> None:
    calls, reply = _git_work(tmp_path, slot, _SMALL)

    assert reply.result is not None
    assert reply.result.field == slot
    assert calls["repositories opened"] > 0
    answer = reply.result.value
    if isinstance(answer, pb.ListRunDigestsResponse):
        assert [entry.run_id for entry in answer.digests] == list(_NAMED)
    else:
        assert isinstance(answer, pb.UsageWindowResponse)
        assert answer.missing_runs == []


def _listing_work(tmp_path: Path, runs: int) -> tuple[Counter[str], int]:
    repositories = FakeGitRepositories()
    project = _project(tmp_path / f"listing-{runs}", runs, repositories)
    calls: Counter[str] = Counter()
    request = WatchRequest(
        checkout="/checkout",
        method=Oneof(
            "stream_saved_runs", pb.StreamSavedRunsRequest(request_id="r", project=str(project))
        ),
    )
    written = bytearray()
    source = registry_read_source(_host(repositories, calls))
    stream_saved_runs(request.to_binary(), source, written.extend)
    length = int.from_bytes(written[:4], "big")
    response = WatchResponse.from_binary(bytes(written[4 : 4 + length]))
    assert response.result is not None
    frame = response.result.value
    assert isinstance(frame, pb.StreamSavedRunsResponse)
    assert frame.complete
    return calls, len(frame.runs)


@pytest.mark.parametrize("runs", [_SMALL, _LARGE])
def test_the_listing_is_the_declared_exception_and_makes_one_pass(
    tmp_path: Path, runs: int
) -> None:
    calls, listed = _listing_work(tmp_path, runs)

    assert listed == runs
    assert calls["branches"] == 1
    assert calls["repositories opened"] == 1
    assert calls.total() <= _SHARED_CALLS + _PER_RUN_CALLS * runs, calls
