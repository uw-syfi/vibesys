"""A job observed again after its end changes nothing core concluded from that end.

The ledger takes a job's first conclusive observation as its submission's own and keeps it,
while the job takes every later one: a poll that was in flight when the job ended answers
again, and a scheduler that ended a job reports its release in a later observation. Every
place that checks the job against its submission's record must read these as one conclusion
(#3124, #3125). The toy search runs on the real core while the driver answers again for every
ended job at any commit point, any number of times; the resumes, the releases and the wakes
must be the ones the same run reaches when no job is observed twice.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import Faults
from vs_sim.api.testing import examples

from .test_agent_turn_runs import SCENARIOS, Scenario, assert_ended_closed
from .test_attempt_retirement import retirement
from .test_continuations import fixture, job_change, persisted_step, synced, with_wait
from .test_job_release import JOB, started
from .test_measurements import observation
from .toy_agent_search import ROLES

if TYPE_CHECKING:
    from vs_core.api.testing import Trace


def outcome(trace: Trace) -> tuple[object, ...]:
    """What the run concluded from its jobs: decisions, resumes, waits and attempts."""
    return (
        [type(decision).__name__ for decision in trace.decisions],
        sorted(
            request.continuation_id.root
            for request in trace.requests
            if isinstance(request, core.ResumeSessionTurn)
        ),
        sorted(
            (row.continuation_id.root, row.phase, row.timeout is None)
            for row in trace.core.evaluation.continuations
        ),
        sorted((row.attempt_id.root, row.phase) for row in trace.core.attempts.attempts),
    )


WAITING = Scenario(
    workstreams=1,
    in_flight=1,
    waits=(True,),
    polls=(1,),
    roles=ROLES,
    ends=("wait",),
    shares=False,
    faults=Faults(),
)
"""Both roles of one attempt wait on their own job: the second resume reads the first job."""


@pytest.mark.regression
@settings(max_examples=examples(pr=6, nightly=200), deadline=None, database=None)
@example(scenario=WAITING, points=frozenset(range(1, 60)))
@example(scenario=WAITING, points=frozenset(range(1, 60, 3)))
@given(scenario=SCENARIOS, points=st.frozensets(st.integers(1, 200), max_size=12))
def test_a_job_observed_again_after_its_end_changes_no_resume_release_or_wake(
    scenario: Scenario, points: frozenset[int]
) -> None:
    """Re-observing every ended job, at any point and any number of times, changes no outcome."""
    once = scenario.run()
    again = replace(scenario, faults=replace(scenario.faults, reobserve_after=points)).run()

    assert_ended_closed(once)
    assert_ended_closed(again)
    assert outcome(again) == outcome(once)


def read_again(seen: core.Observation, later: int, delay: float) -> core.Observation:
    """The same end as ``seen``, read ``later`` sequences and ``delay`` seconds after it."""
    return seen.model_copy(
        update={
            "sequence": seen.sequence + later,
            "event_id": core.EventId(root=f"{seen.event_id.root}:again:{later}"),
            "observed_at": seen.observed_at + delay,
        }
    )


@pytest.mark.regression
@settings(max_examples=examples(pr=4, nightly=100), deadline=None)
@example(status=core.ObservationStatus.SUCCEEDED, later=1, delay=0.001)
@given(
    status=st.sampled_from(
        (
            core.ObservationStatus.SUCCEEDED,
            core.ObservationStatus.FAILED,
            core.ObservationStatus.CANCELLED,
        )
    ),
    later=st.integers(1, 5),
    delay=st.floats(0.001, 60.0),
)
def test_a_release_proof_read_before_the_job_was_polled_again_still_releases_the_attempt(
    status: core.ObservationStatus, later: int, delay: float
) -> None:
    """A closing attempt hears of its job's release after the job was polled again.

    The proof carries the end the job first reported; the job now holds the same end read
    later. The attempt must still drop the job dependency (`_attempt_retirement._job_proof`).
    """
    cluster = started(running_polls=10**6, ending=status)
    cluster.feed(retirement(cluster.ref, "cancel"))
    edge = core.ReleaseDependency(kind="job", identity=JOB)
    assert edge in cluster.attempt().release_dependencies
    closing = cluster.state
    end = observation(
        cluster.submission,
        2,
        status=status,
        terminal=True,
        released=True,
        children_complete=True,
        resource_id=JOB,
    )
    ended = core.step(closing, core.JobObserved(resource_id=JOB, observation=end)).state
    polled = core.step(
        ended, core.JobObserved(resource_id=JOB, observation=read_again(end, later, delay))
    ).state
    assert polled.evaluation.jobs[0].observation != end
    # The evaluation area has taken both readings; the attempt has not yet heard of the end.
    state = polled.model_copy(update={"attempts": closing.attempts})
    proof = core.ReleaseDependencyObserved(attempt=cluster.ref, dependency=edge, observation=end)

    released = core.step(state, proof).state

    assert edge not in released.attempts.attempts[0].release_dependencies


@pytest.mark.regression
@settings(max_examples=examples(pr=4, nightly=100), deadline=None)
@example(registered=False, later=1, delay=0.001)
@example(registered=True, later=1, delay=0.001)
@given(registered=st.booleans(), later=st.integers(1, 5), delay=st.floats(0.001, 60.0))
def test_a_wake_carrying_an_earlier_reading_of_a_jobs_end_still_resumes(
    *, registered: bool, later: int, delay: float
) -> None:
    """The wake for the last job to end names the end the job first reported.

    The job has since been polled again and holds the same end read later. The waiting
    continuation must still be authorized (`_continuations._changed`), not skip the wake and
    wait for one that never comes.
    """
    state, continuation = fixture(registered=registered)
    state = with_wait(state, continuation)
    field = "registered_jobs" if registered else "jobs"
    jobs = list(getattr(state.evaluation, field))
    ended = []
    for job in jobs:
        assert job.observation is not None
        end = job.observation.model_copy(
            update={"status": core.ObservationStatus.SUCCEEDED, "terminal": True, "sequence": 2}
        )
        ended.append(
            job.model_copy(
                update={
                    "status": end.status,
                    "terminal": True,
                    "wait": None,
                    "observation": end,
                }
            )
        )
    last = ended[-1]
    assert last.observation is not None
    now = (
        *ended[:-1],
        last.model_copy(update={"observation": read_again(last.observation, later, delay)}),
    )
    # The ledger keeps each submission's first conclusive reading.
    state = synced(
        state.model_copy(
            update={"evaluation": state.evaluation.model_copy(update={field: tuple(ended)})}
        )
    )
    state = state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={field: now})}
    )

    result = persisted_step(state, job_change(last, jobs[-1]))

    assert [type(event) for event in result.events] == [core.ResumeAuthorized]
