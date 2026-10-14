"""A step costs the same however long the run behind it is.

Core keeps every request of a run in its ledger, so a step that found a record by comparing its
identity with every row cost more with every request the run had made: a long run's steps grew
with its length and the run with its square. A lookup is now a read of the ledger's index. The
cost is counted, not timed: the model comparisons (`BaseModel.__eq__`) one step makes, on a
ledger of ``n`` and of ``4 * n`` finished requests, so the check is exact and never flaky. A job
event's questions about each running job are counted the same way, as the calls one step makes.
"""

from __future__ import annotations

import sys
from functools import cache
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core

from .test_job_release import JOB, started
from .test_measurements import observation
from .toy_agent_search import Config, ToyHost, run_toy

if TYPE_CHECKING:
    from types import FrameType

_EQUALITY = BaseModel.__eq__.__code__


def _comparisons(state: core.CoreState, event: core.CoreEvent) -> int:
    """How many model comparisons ``core.step(state, event)`` makes."""
    counted = [0]

    def count(frame: FrameType, kind: str, _arg: object) -> None:
        if kind == "call" and frame.f_code is _EQUALITY:
            counted[0] += 1

    sys.setprofile(count)
    try:
        core.step(state, event)
    finally:
        sys.setprofile(None)
    return counted[0]


@cache
def _toy_run() -> core.CoreState:
    """The core state at the end of the smallest toy run (core states never change)."""
    return run_toy(ToyHost(), Config()).core


def _padded(state: core.CoreState, filler: core.RequestRecord, rows: int) -> core.CoreState:
    """``state`` with ``rows`` more finished requests ahead of its own, as a long run has."""
    padding = tuple(
        filler.model_copy(update={"request_id": core.RequestId(root=f"earlier-{row}")})
        for row in range(rows)
    )
    records = state.records.model_copy(update={"records": (*padding, *state.records.records)})
    return state.model_copy(update={"records": records})


_ROWS = 50


def test_a_step_compares_as_much_on_a_long_ledger_as_on_a_short_one() -> None:
    """A redelivered answer, which looks its record up in the ledger, on ``n`` and ``4 * n`` rows."""
    state = _toy_run()
    finished = [
        row
        for row in state.records.records
        if row.phase == core.RequestPhase.COMPLETED and row.observation is not None
    ]
    answer = finished[-1].observation
    assert answer is not None
    redelivered = core.RequestObserved(observation=answer)
    short = _comparisons(_padded(state, finished[0], _ROWS), redelivered)
    long = _comparisons(_padded(state, finished[0], 4 * _ROWS), redelivered)
    assert long <= short, (short, long)


def _calls(state: core.CoreState, event: core.CoreEvent) -> int:
    """How many function calls, Python and builtin, ``core.step(state, event)`` makes."""
    counted = [0]

    def count(_frame: FrameType, kind: str, _arg: object) -> None:
        if kind in {"call", "c_call"}:
            counted[0] += 1

    sys.setprofile(count)
    try:
        core.step(state, event)
    finally:
        sys.setprofile(None)
    return counted[0]


def _shaped(state: core.CoreState, jobs: int, rows: int) -> core.CoreState:
    """``state`` with ``jobs`` more running jobs and ``rows`` inspections of earlier jobs."""
    (job,) = state.evaluation.jobs
    running = tuple(
        job.model_copy(update={"resource_id": core.ResourceId(root=f"running-{index}")})
        for index in range(jobs)
    )
    evaluation = state.evaluation.model_copy(update={"jobs": (job, *running)})
    (submitted,) = (
        row for row in state.records.records if isinstance(row.request, core.SubmitMeasurement)
    )
    padding = tuple(
        submitted.model_copy(
            update={
                "request_id": core.RequestId(root=f"earlier-{row}"),
                "request": core.InspectOwnedJob.model_validate(
                    {
                        **submitted.request.model_dump(exclude={"kind", "plan", "decision_id"}),
                        "request_id": core.RequestId(root=f"earlier-{row}"),
                        "resource_id": core.ResourceId(root=f"earlier-job-{row}"),
                    }
                ),
            }
        )
        for row in range(rows)
    )
    records = state.records.model_copy(update={"records": (*padding, *state.records.records)})
    return state.model_copy(update={"evaluation": evaluation, "records": records})


_JOBS = 8


def test_a_job_event_reads_the_ledger_once_however_many_jobs_are_running() -> None:
    """A running job's poll answer, on ``n`` and ``4 * n`` rows, beside 0 and 8 more jobs.

    Every job event asks, for each running job, whether core already cancelled or inspected
    it. Asked as a scan of the ledger per job, the step's cost grew with jobs times rows; read
    from the ledger's index by resource, the extra jobs add less than one call per added row
    each (the ledger itself is still read once per step).
    """
    cluster = started(running_polls=5, ending=core.ObservationStatus.SUCCEEDED)
    poll = core.JobObserved(
        resource_id=JOB,
        observation=observation(
            cluster.submission, 2, resource_id=JOB, observed_at=cluster.state.run.now_at
        ),
    )

    def growth(jobs: int) -> int:
        short = _calls(_shaped(cluster.state, jobs, _ROWS), poll)
        return _calls(_shaped(cluster.state, jobs, 4 * _ROWS), poll) - short

    alone, beside = growth(0), growth(_JOBS)
    assert beside - alone < _JOBS * 3 * _ROWS, (alone, beside)


@given(
    names=st.lists(st.sampled_from(["a", "b", "c", "d"]), max_size=8),
    wanted=st.sampled_from("abcde"),
)
def test_the_ledger_index_finds_what_a_scan_finds(names: list[str], wanted: str) -> None:
    """Every lookup answers as the scan it replaced, duplicates (a corrupt ledger) included."""
    state = _toy_run()
    filler = state.records.records[0]
    rows = tuple(
        filler.model_copy(update={"request_id": core.RequestId(root=name)}) for name in names
    )
    ledger = state.records.model_copy(update={"records": rows})
    identity = core.RequestId(root=wanted)
    scanned = tuple(row for row in rows if row.request_id == identity)
    assert ledger.find(identity) == scanned
    assert ledger.record(identity) == (scanned[0] if scanned else None)
    assert ledger.find(None) == ()


@given(
    named=st.lists(st.sampled_from([None, "a", "b", "c"]), max_size=8),
    wanted=st.sampled_from("abcd"),
)
def test_the_resource_index_finds_what_a_scan_finds(named: list[str | None], wanted: str) -> None:
    """The records about a resource, by index, are the ones a scan finds, in ledger order."""
    state = started(running_polls=0, ending=core.ObservationStatus.SUCCEEDED).state
    (submitted,) = state.records.records
    rows = tuple(
        submitted
        if name is None
        else submitted.model_copy(
            update={
                "request": core.InspectOwnedJob.model_validate(
                    {
                        **submitted.request.model_dump(exclude={"kind", "plan", "decision_id"}),
                        "resource_id": core.ResourceId(root=name),
                    }
                )
            }
        )
        for name in named
    )
    ledger = state.records.model_copy(update={"records": rows})
    resource = core.ResourceId(root=wanted)
    scanned = tuple(row for row in rows if getattr(row.request, "resource_id", None) == resource)
    assert ledger.about_resource(resource) == scanned
