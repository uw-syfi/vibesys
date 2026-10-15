"""A wait's deadline freezes on the job facts it had then, whatever event carries time past it.

An agent that waits on its measurement suspends its turn until the job settles or the
wait's deadline passes. Core moves its clock only with the events it consumes, so the
event that carries time past the deadline may be a clock tick or the next poll of the
very job the agent waits on, still queued. Every ordering of those facts must freeze the
timeout on the progress the job had at the deadline and never halt the run.
"""

from dataclasses import dataclass

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_continuations import fixture
from .test_measurements import observation, plan

JOB = core.ResourceId(root="job")
LIMIT = 50.0
"""The agent's wait limit; its submission and its wait end this long after the call."""


@dataclass(frozen=True)
class Poll:
    """The executor reports the job ``after`` seconds after the previous fact."""

    after: float
    status: core.ObservationStatus
    released: bool = False


@dataclass(frozen=True)
class Tick:
    """The run loop's clock tick ``after`` seconds after the previous fact."""

    after: float


type Fact = Poll | Tick


def waiting() -> tuple[core.CoreState, core.Continuation, core.SubmitMeasurement]:
    """An agent's turn suspended on the one measurement it submitted, its job queued."""
    state, template = fixture(run_owned=True)
    state = state.model_copy(update={"evaluation": core.EvaluationState()})
    scope = core.Scope(owner=state.run.run_id, generation=state.run.generation)
    called = core.step(
        state,
        core.AgentMeasurementRequested(
            scope=scope, plan=plan(), within=core.Within(limit=LIMIT), call_id="call"
        ),
    )
    (submit,) = called.requests
    assert isinstance(submit, core.SubmitMeasurement)
    assert submit.request_id is not None
    (call,) = called.state.evaluation.agent_calls
    assert call.deadline_at is not None
    state = core.step(called.state, core.DispatchAuthorized(request_id=submit.request_id)).state
    first = observation(submit, 1, resource_id=JOB)
    state = core.step(state, core.RequestObserved(observation=first)).state
    state = core.step(state, core.JobObserved(resource_id=JOB, observation=first)).state
    wait = template.model_copy(update={"jobs": (JOB,), "deadline_at": call.deadline_at})
    state = core.step(state, core.TurnSuspended(continuation=wait)).state
    (recorded,) = state.evaluation.continuations
    assert recorded.timeout is None
    return state, recorded, submit


def _event(fact: Fact, submit: core.SubmitMeasurement, sequence: int, at: float) -> core.CoreEvent:
    if isinstance(fact, Tick):
        return core.ClockAdvanced(now_at=at)
    terminal = fact.status != core.ObservationStatus.PENDING
    observed = observation(
        submit,
        sequence,
        resource_id=JOB,
        observed_at=at,
        status=fact.status,
        terminal=terminal,
        released=terminal and fact.released,
        children_complete=terminal and fact.released,
    )
    progress = (
        None
        if terminal
        else core.JobProgress(
            observation_sequence=sequence,
            observed_at=at,
            state="pending",
            queued_s=at,
            pending_reason="Priority",
        )
    )
    return core.JobObserved(resource_id=JOB, observation=observed, progress=progress)


def _job(state: core.CoreState) -> core.OwnedJob:
    (job,) = state.evaluation.jobs
    return job


def _settled(job: core.OwnedJob) -> bool:
    return job.terminal and job.status != core.ObservationStatus.PENDING


facts = st.lists(
    st.one_of(
        st.builds(
            Poll,
            after=st.floats(min_value=0.5, max_value=40.0),
            status=st.sampled_from(
                (
                    core.ObservationStatus.PENDING,
                    core.ObservationStatus.PENDING,
                    core.ObservationStatus.SUCCEEDED,
                    core.ObservationStatus.FAILED,
                )
            ),
            released=st.booleans(),
        ),
        st.builds(Tick, after=st.floats(min_value=0.5, max_value=40.0)),
    ),
    min_size=1,
    max_size=8,
)


@settings(max_examples=examples(pr=60, nightly=1000))
# The live run: the job still queued at its next poll, past the wait's deadline.
@example(facts=[Poll(after=60.0, status=core.ObservationStatus.PENDING)])
# The clock passes the deadline first, then the queued job is polled.
@example(facts=[Tick(after=60.0), Poll(after=1.0, status=core.ObservationStatus.PENDING)])
# The job ends before the deadline and reports its release after it.
@example(
    facts=[
        Poll(after=5.0, status=core.ObservationStatus.SUCCEEDED),
        Poll(after=60.0, status=core.ObservationStatus.SUCCEEDED, released=True),
    ]
)
# The job's first fact past the deadline is its end.
@example(facts=[Poll(after=60.0, status=core.ObservationStatus.SUCCEEDED)])
@given(facts=facts)
def test_a_wait_freezes_at_its_deadline_on_the_facts_the_job_had_then(
    facts: list[Fact],
) -> None:
    state, wait, submit = waiting()
    sequence = 1
    at = state.run.now_at
    crossed: tuple[float, core.OwnedJob] | None = None
    resumes = 0
    for drawn in facts:
        at += drawn.after
        fact = drawn
        if isinstance(drawn, Poll):
            job = _job(state)
            if _settled(job):
                # An ended job reports only its release, once.
                if job.released or not drawn.released:
                    continue
                fact = Poll(after=drawn.after, status=job.status, released=True)
            sequence += 1
        before = state
        result = core.step(state, _event(fact, submit, sequence, at))
        state = result.state
        resumes += sum(isinstance(event, core.ResumeAuthorized) for event in result.events)
        if crossed is None and at >= wait.deadline_at:
            crossed = (at, _job(before))
        (row,) = state.evaluation.continuations
        if crossed is None or _settled(crossed[1]):
            assert row.timeout is None
            continue
        reached_at, job = crossed
        assert row.timeout == core.TimedOut(
            deadline_at=wait.deadline_at,
            reached_at=reached_at,
            unfinished=(core.JobTimeout(resource_id=JOB, progress=job.progress),),
        )
    assert resumes <= 1
