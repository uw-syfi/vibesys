"""A strategy's measurement charges its limit from its job's start, never its queue time.

A strategy that measures (a ``Measure`` decision, such as the dynamic strategy's baseline
input measurement) gives the work a limit counted from the submission's dispatch. The
submission's record stays open until the job ends, and a record whose limit passes is
ended: the strategy gets ``Failed(TIMED_OUT)`` and the job is cancelled. On a contended
cluster an 8-GPU job waits hours in the scheduler's queue, so a limit that counted that
time expired before the job ever ran. The rule is the one agent waits follow: the queue
time a job's pending facts earn (at most ``observe_backoff_cap`` per interval) is not
charged, so the limit counts from the job's start.
"""

from dataclasses import dataclass

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_continuation_deadlines import Ended, Poll, Started, Tick, seconds
from .test_measurements import observation, requested

JOB = core.ResourceId(root="job")
LIMIT = 100.0
"""The measurement's limit, counted from the submission's dispatch at time 0."""
QUEUED_GAP = 100.0
"""The longest gap after a queued fact: the limit, so a few queued facts pass it."""
RUNNING_GAP = 40.0


@dataclass(frozen=True)
class Measuring:
    state: core.CoreState
    submit: core.SubmitMeasurement


def measuring(*, queued: bool) -> Measuring:
    """A strategy's ``Measure`` dispatched at time 0, its job's first fact at time 1.

    With ``queued`` that fact says the job waits in the scheduler's queue. The test
    delivers the job's facts itself and never answers core's polls, so a poll's own bound
    is set past the run deadline: only the submission's limit can end the measurement.
    """
    initial = core.initial_state()
    limits = initial.run.limits.model_copy(update={"reconciliation_bound": 20 * LIMIT})
    initial = initial.model_copy(update={"run": initial.run.model_copy(update={"limits": limits})})
    result = requested(initial)
    (submit,) = result.requests
    assert isinstance(submit, core.SubmitMeasurement)
    assert submit.request_id is not None
    assert result.state.run.now_at == 0.0
    state = core.step(result.state, core.DispatchAuthorized(request_id=submit.request_id)).state
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
    return Measuring(state=state, submit=submit)


def _event(
    fact: Poll | Started | Ended | Tick, submit: core.SubmitMeasurement, sequence: int, at: float
) -> core.CoreEvent:
    if isinstance(fact, Tick):
        return core.ClockAdvanced(now_at=at)
    status = fact.status if isinstance(fact, Poll | Ended) else core.ObservationStatus.PENDING
    terminal = status != core.ObservationStatus.PENDING
    queued = isinstance(fact, Poll) and fact.queued
    observed = observation(
        submit,
        sequence,
        resource_id=JOB,
        observed_at=at,
        status=status,
        terminal=terminal,
    )
    progress = (
        None
        if terminal
        else core.JobProgress(
            observation_sequence=sequence,
            observed_at=at,
            state="pending" if queued else "running",
            pending_reason="Priority" if queued else None,
        )
    )
    return core.JobObserved(resource_id=JOB, observation=observed, progress=progress)


def _submission(state: core.CoreState, submit: core.SubmitMeasurement) -> core.RequestRecord:
    assert submit.request_id is not None
    record = state.records.record(submit.request_id)
    assert record is not None
    return record


def _timed_out(events: tuple[core.StrategyEvent, ...]) -> int:
    return sum(
        isinstance(event, core.Failed) and event.reason == core.FailureReason.TIMED_OUT
        for event in events
    )


def _cancels(requests: tuple[core.Request, ...]) -> int:
    return sum(
        isinstance(request, core.CancelOwnedJob | core.CancelOwnedResource) for request in requests
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
ends = st.one_of(
    st.none(),
    st.builds(
        Ended,
        after=seconds(RUNNING_GAP),
        status=st.sampled_from((core.ObservationStatus.SUCCEEDED, core.ObservationStatus.FAILED)),
    ),
)


@settings(max_examples=examples(pr=80, nightly=1000))
# The live run: queued past the limit, then it runs and finishes in time.
@example(
    first_queued=True,
    facts=[
        Poll(after=100.0, status=core.ObservationStatus.PENDING, queued=True),
        Tick(after=100.0),
        Started(after=50.0),
        Tick(after=40.0),
    ],
    end=Ended(after=10.0, status=core.ObservationStatus.SUCCEEDED),
)
# Queued past the limit, then it runs past its own limit and times out on its run time.
@example(
    first_queued=True,
    facts=[Tick(after=100.0), Started(after=10.0), Tick(after=40.0), Tick(after=40.0)],
    end=None,
)
# Requeued: it runs, goes back to the queue past its limit, then runs again.
@example(
    first_queued=False,
    facts=[
        Started(after=20.0),
        Poll(after=20.0, status=core.ObservationStatus.PENDING, queued=True),
        Tick(after=100.0),
        Started(after=1.0),
        Tick(after=40.0),
        Tick(after=40.0),
    ],
    end=None,
)
@given(first_queued=st.booleans(), facts=interleaved, end=ends)
def test_a_measurement_times_out_exactly_when_its_job_has_run_its_limit(
    *, first_queued: bool, facts: list[Poll | Started | Tick], end: Ended | None
) -> None:
    """Any interleaving of queued and running facts, clock ticks, requeues and an end.

    Time is charged from the dispatch, except the queue time a pending fact earns: the time
    from it to the job's next fact (or to now), at most ``observe_backoff_cap``. The record's
    limit is checked at each clock tick: the measurement times out at the first tick at which
    the charged time reaches the limit (or the run's deadline passes) while the job has not
    ended, once, and is cancelled at most once; never before.
    """
    measured = measuring(queued=first_queued)
    state, submit = measured.state, measured.submit
    cap = state.run.limits.observe_backoff_cap
    sequence = 1
    at = state.run.now_at
    charged = at  # Dispatched at 0; the job's first fact at ``at``.
    last_fact_at = at
    job_queued = first_queued
    ended = False
    timeouts = 0
    cancels = 0
    for fact in [*facts, *(() if end is None else (end, Tick(after=LIMIT)))]:
        at += fact.after
        since = at - last_fact_at
        now_charged = charged + (max(since - cap, 0.0) if job_queued else since)
        if not isinstance(fact, Tick):
            sequence += 1
            job_queued = isinstance(fact, Poll)
            charged, last_fact_at = now_charged, at
        result = core.step(state, _event(fact, submit, sequence, at))
        state = result.state
        timed = _timed_out(result.events)
        cancels += _cancels(result.requests)
        due = now_charged >= LIMIT or at >= state.run.deadline_at
        if isinstance(fact, Tick) and not ended and due:
            # The first tick past the run limit ends it; later ones find it already ended.
            assert timeouts + timed == 1, (fact, at, now_charged)
        elif timeouts == 0:
            assert timed == 0, (fact, at, now_charged)
            assert _submission(state, submit).phase != core.RequestPhase.BLOCKED, (fact, at)
        timeouts += timed
        assert timeouts <= 1
        assert cancels <= 1
        ended = ended or isinstance(fact, Ended)
