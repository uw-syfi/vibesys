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
    from collections.abc import Callable
    from types import CodeType, FrameType


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


_CORE_SOURCE = core.__file__.rsplit("/api/", 1)[0]


def _iterations(state: core.CoreState, event: core.CoreEvent) -> int:
    """How many loop iterations in core's own code ``core.step(state, event)`` runs.

    Calls miss a scan written as a generator, which runs a whole ledger in one call; each
    iteration of any loop is a backward jump, which ``sys.monitoring`` reports. The tool id is
    one no debugger, coverage tool or profiler claims, so measuring never disturbs them.
    """
    monitoring = sys.monitoring
    tool = next(t for t in (3, 4) if monitoring.get_tool(t) is None)
    counted = [0]

    def jumped(code: CodeType, _source: int, _destination: int) -> None:
        if code.co_filename.startswith(_CORE_SOURCE):
            counted[0] += 1

    monitoring.use_tool_id(tool, "step-iterations")
    try:
        monitoring.register_callback(tool, monitoring.events.JUMP, jumped)
        monitoring.set_events(tool, monitoring.events.JUMP)
        core.step(state, event)
    finally:
        monitoring.set_events(tool, 0)
        monitoring.register_callback(tool, monitoring.events.JUMP, None)
        monitoring.free_tool_id(tool)
    return counted[0]


def _answered(row: core.RequestRecord, rows: int) -> tuple[core.RequestRecord, ...]:
    """``rows`` finished copies of ``row``, each answered under its own identity, as in a real ledger."""
    answer = row.observation
    assert answer is not None
    copies = []
    for index in range(rows):
        identity = core.RequestId(root=f"earlier-{index}")
        copies.append(
            row.model_copy(
                update={
                    "request_id": identity,
                    "request": row.request.model_copy(update={"request_id": identity}),
                    "observation": answer.model_copy(
                        update={
                            "request_id": identity,
                            "event_id": core.EventId(root=f"earlier-{index}:0"),
                        }
                    ),
                }
            )
        )
    return tuple(copies)


def _finished(state: core.CoreState) -> core.RequestRecord:
    return next(
        row
        for row in state.records.records
        if row.phase == core.RequestPhase.COMPLETED and row.observation is not None
    )


def _behind(state: core.CoreState, finished: core.RequestRecord, rows: int) -> core.CoreState:
    """``state`` with ``rows`` more finished, answered copies of ``finished`` ahead of its own."""
    records = state.records.model_copy(
        update={"records": (*_answered(finished, rows), *state.records.records)}
    )
    return state.model_copy(update={"records": records})


# A step's cost on ``n``, ``4 * n`` and ``16 * n`` finished rows. Each fourfold step in rows adds
# four times the cost of the step before it when the work is linear in the ledger, and sixteen
# times when it is quadratic. Comparing the added costs, not the totals, leaves out the step's
# fixed cost, which hid a quadratic part: with one linear-time fix undone, a restart's total
# grew only 4.9x to 5.8x from ``n`` to ``4 * n``, while its added cost grew 7.8x to 9.8x.
_LINEAR = 6
_SIZES = (_ROWS, 4 * _ROWS, 16 * _ROWS)


def _quadratic(cost: Callable[[int], int]) -> tuple[int, ...] | None:
    """The costs ``cost(rows)`` on each of ``_SIZES`` when they grow faster than linearly, else None."""
    short, long, longest = (cost(rows) for rows in _SIZES)
    return None if longest - long <= _LINEAR * (long - short) else (short, long, longest)


def _grows_linearly(
    state: core.CoreState, event: core.CoreEvent, finished: core.RequestRecord
) -> tuple[int, ...] | None:
    """None when ``event`` costs linearly more behind more ``finished`` rows, else its costs."""
    return _quadratic(lambda rows: _iterations(_behind(state, finished, rows), event))


def test_a_restart_costs_in_proportion_to_the_ledger() -> None:
    """Opening a new host epoch over ``n``, ``4 * n`` and ``16 * n`` finished requests.

    A restart re-reads the answer every finished record holds. Each re-read copied the ledger
    state, and the copy rebuilt the ledger's index on its next lookup, so a restart cost the
    square of the run's length: on a 10-hour run (7,700 records) it held the run loop for
    20 to 40 s while the run held its lease.
    """
    state = started(running_polls=5, ending=core.ObservationStatus.SUCCEEDED).state
    restart = core.RecoveryStarted(epoch=state.records.recovery.epoch + 1, now_at=state.run.now_at)
    finished = _finished(run_toy(ToyHost(waits=(True,), polls=(2,)), Config()).core)
    assert _grows_linearly(state, restart, finished) is None
