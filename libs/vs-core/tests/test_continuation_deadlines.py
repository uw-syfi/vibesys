"""A wait's deadline freezes on the job facts it had then, whatever event carries time past it.

An agent that waits on its measurement suspends its turn until the job settles or the
wait's deadline passes. Core moves its clock only with the events it consumes, so the
event that carries time past the deadline may be a clock tick or the next poll of the
very job the agent waits on, still running. Every ordering of those facts must freeze the
timeout on the progress the job had at the deadline and never halt the run.

The limit counts from the job's start: the queue time a pending fact earns is not charged,
up to ``observe_backoff_cap`` per interval, so a scheduler that stops answering cannot hold
a wait open.
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
    queued: bool = False
    """Whether a job that has not ended still waits in the scheduler's queue."""


@dataclass(frozen=True)
class Tick:
    """The run loop's clock tick ``after`` seconds after the previous fact."""

    after: float


type Fact = Poll | Tick


def waiting(
    *, queued: bool = False
) -> tuple[core.CoreState, core.Continuation, core.SubmitMeasurement]:
    """An agent's turn suspended on the one measurement it submitted, its job submitted.

    With ``queued`` the job's first fact says it waits in the scheduler's queue.
    """
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
    progress = (
        core.JobProgress(
            observation_sequence=1,
            observed_at=first.observed_at,
            state="pending",
            pending_reason="Resources",
        )
        if queued
        else None
    )
    state = core.step(
        state, core.JobObserved(resource_id=JOB, observation=first, progress=progress)
    ).state
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
            state="pending" if fact.queued else "running",
            pending_reason="Priority" if fact.queued else None,
        )
    )
    return core.JobObserved(resource_id=JOB, observation=observed, progress=progress)


def _job(state: core.CoreState) -> core.OwnedJob:
    (job,) = state.evaluation.jobs
    return job


def _submission_ended(state: core.CoreState, submit: core.SubmitMeasurement) -> bool:
    """Whether the submission's record was ended by its limit (blocked), not by its job."""
    assert submit.request_id is not None
    record = state.records.record(submit.request_id)
    assert record is not None
    return record.phase == core.RequestPhase.BLOCKED


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
# The job still running at its next poll, past the wait's deadline.
@example(facts=[Poll(after=60.0, status=core.ObservationStatus.PENDING)])
# The clock passes the deadline first, then the running job is polled.
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
            unfinished=(
                core.JobTimeout(
                    resource_id=JOB,
                    progress=job.progress,
                    by=core.WaitEnd.LIMIT,
                    at=wait.deadline_at,
                ),
            ),
        )
    assert resumes <= 1


# A job's run limit starts when it starts running: the time it waits in a queue (Slurm's, or
# an executor's admission queue) is not charged to the wait. On a contended cluster a job can
# stay queued for hours; a wait that counted that time would time the agent out and cancel
# its evaluation before it ever ran.

QUEUED_GAP = 150.0
"""The longest gap between two facts while the job is queued: three times the limit."""
RUNNING_GAP = 40.0


@dataclass(frozen=True)
class Started:
    """The executor reports the job running ``after`` seconds after the previous fact."""

    after: float


@dataclass(frozen=True)
class Ended:
    """The executor reports the job ended ``after`` seconds after the previous fact."""

    after: float
    status: core.ObservationStatus


type Lifecycle = Poll | Tick | Started | Ended


def _lifecycle_event(
    fact: Lifecycle, submit: core.SubmitMeasurement, sequence: int, at: float
) -> core.CoreEvent:
    match fact:
        case Started():
            return _event(Poll(fact.after, core.ObservationStatus.PENDING), submit, sequence, at)
        case Ended():
            return _event(Poll(fact.after, fact.status), submit, sequence, at)
        case _:
            return _event(fact, submit, sequence, at)


def seconds(most: float) -> st.SearchStrategy[float]:
    """A gap in whole seconds, so that sums of gaps are exact and a deadline is hit exactly."""
    return st.integers(min_value=1, max_value=int(most)).map(float)


ends = st.one_of(
    st.none(),
    st.builds(
        Ended,
        after=seconds(RUNNING_GAP),
        status=st.sampled_from((core.ObservationStatus.SUCCEEDED, core.ObservationStatus.FAILED)),
    ),
)


interleaved = st.lists(
    st.one_of(
        seconds(QUEUED_GAP).map(
            lambda after: Poll(after=after, status=core.ObservationStatus.PENDING, queued=True)
        ),
        st.builds(Started, after=seconds(RUNNING_GAP)),
        st.builds(Tick, after=seconds(QUEUED_GAP)),
    ),
    max_size=8,
)


@settings(max_examples=examples(pr=60, nightly=1000))
# Requeued: it runs, goes back to the queue past its limit, then runs again.
@example(
    first_queued=False,
    facts=[
        Started(after=20.0),
        Poll(after=20.0, status=core.ObservationStatus.PENDING, queued=True),
        Tick(after=150.0),
        Started(after=1.0),
        Tick(after=10.0),
    ],
    end=None,
)
@given(first_queued=st.booleans(), facts=interleaved, end=ends)
def test_a_wait_times_out_exactly_when_its_job_has_run_its_limit(
    *, first_queued: bool, facts: list[Poll | Started | Tick], end: Ended | None
) -> None:
    """Any interleaving of queued and running facts, clock ticks, requeues and an end.

    Time is charged from the call, except the queue time a pending fact earns: the time from
    it to the job's next fact (or to now), at most ``observe_backoff_cap``. The wait times
    out at the first event at which the charged time reaches the limit (or the run's deadline
    passes) while the job has not ended, and never before.
    """
    state, _, submit = waiting(queued=first_queued)
    cap = state.run.limits.observe_backoff_cap
    sequence = 1
    at = state.run.now_at
    charged = at  # The call was at 0; the job's first fact at ``at``.
    last_fact_at = at
    job_queued = first_queued
    frozen: core.TimedOut | None = None
    for fact in [*facts, *(() if end is None else (end,))]:
        at += fact.after
        since = at - last_fact_at
        now_charged = charged + (max(since - cap, 0.0) if job_queued else since)
        before = _job(state)
        if not isinstance(fact, Tick):
            sequence += 1
            job_queued = isinstance(fact, Poll)
            charged, last_fact_at = now_charged, at
        result = core.step(state, _lifecycle_event(fact, submit, sequence, at))
        state = result.state
        (row,) = state.evaluation.continuations
        if _submission_ended(state, submit):
            assert now_charged >= LIMIT or at >= state.run.deadline_at, (fact, at, now_charged)
        due = now_charged >= LIMIT or at >= state.run.deadline_at
        if frozen is None and due and not _settled(before):
            assert row.timeout is not None, (fact, at, now_charged)
            assert row.timeout.unfinished == (
                core.JobTimeout(
                    resource_id=JOB,
                    progress=before.progress,
                    by=row.timeout.by,
                    at=row.timeout.deadline_at,
                ),
            )
            assert row.timeout.reached_at == at
            frozen = row.timeout
        assert row.timeout == frozen, (fact, at, now_charged)
        if _settled(_job(state)):
            break
