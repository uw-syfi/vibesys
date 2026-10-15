"""Every wait on a job ends by a bound derived from the job's own queue time, for every source.

A job's queue time is data: each fact after a pending one credits the job with the gap, capped
at ``observe_backoff_cap`` so that an outage of the scheduler (stale pending facts, polls that
never answer) is not counted as queue time. Work tied to the job ends at its stated limit plus
that credit, when the job's queue budget runs out, or when the run ends; whichever is first.

The worlds (``queue_worlds``) cover each source of a job: an agent's own submission, an agent's
repeat of a strategy's submission, a strategy's ``Measure``, a registered operation's job, and
one wait on two jobs. Histories interleave fresh and stale pending facts, polls dispatched and
never answered, requeues, running facts, clock ticks and job ends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .queue_worlds import BUILDERS, Source, World, fact, limits

LIMITS = limits()
CAP = LIMITS.observe_backoff_cap
NOTICE = LIMITS.queue_notice_interval
ENDS_OF_A_DECISION_WAIT = frozenset(
    {core.FailureReason.TIMED_OUT, core.FailureReason.QUEUE_BUDGET_SPENT}
)


@dataclass(frozen=True)
class Fact:
    """Job ``job`` reports ``state`` (pending, running or ended) ``gap`` seconds after the last step."""

    job: int
    state: str
    gap: float


@dataclass(frozen=True)
class Tick:
    """The clock moves ``gap`` seconds with no fact; polls in flight stay unanswered."""

    gap: float


Step = Fact | Tick

# Fresh gaps stay within the cap; stale ones (an outage) exceed it.
gaps = st.one_of(
    st.floats(1.0, CAP, allow_nan=False), st.floats(CAP + 1.0, 4 * CAP, allow_nan=False)
)
facts = st.builds(
    Fact,
    job=st.integers(0, 1),
    state=st.sampled_from(["pending", "pending", "pending", "running", "ended"]),
    gap=gaps,
)
histories = st.lists(st.one_of(facts, st.builds(Tick, gap=gaps)), max_size=25)


@dataclass
class Run:
    """A world driven through ``core.step``, with everything the properties read."""

    world: World
    now: float = 1.0
    events: list[core.StrategyEvent] = field(default_factory=list)
    ended: bool = False

    def feed(self, event: core.CoreEvent, *, dispatch_polls: bool) -> core.Transition:
        result = core.step(self.world.state, event)
        self.world = replace(self.world, state=result.state)
        self.events.extend(result.events)
        if dispatch_polls:
            for request in result.requests:
                if isinstance(request, core.ObserveOwnedJob) and request.request_id is not None:
                    self.feed(
                        core.DispatchAuthorized(request_id=request.request_id),
                        dispatch_polls=False,
                    )
        return result


def _jobs(state: core.CoreState) -> tuple[core.OwnedJob | core.RegisteredOwnedJob, ...]:
    return (*state.evaluation.jobs, *state.evaluation.registered_jobs)


def _job(
    state: core.CoreState, resource: core.ResourceId
) -> core.OwnedJob | core.RegisteredOwnedJob:
    return next(job for job in _jobs(state) if job.resource_id == resource)


def _pending(job: core.OwnedJob | core.RegisteredOwnedJob) -> bool:
    progress, observation = job.progress, job.observation
    return (
        not job.terminal
        and progress is not None
        and observation is not None
        and progress.state == "pending"
        and progress.observation_sequence == observation.sequence
    )


def _apply(run: Run, step: Step, *, dispatch_polls: bool) -> None:
    """Deliver ``step``, checking the queue credit and notices of the job it reports."""
    run.now += step.gap
    if isinstance(step, Tick):
        run.feed(core.ClockAdvanced(now_at=run.now), dispatch_polls=dispatch_polls)
        return
    index = step.job % len(run.world.jobs)
    resource = run.world.jobs[index].resource_id
    before = _job(run.world.state, resource)
    if before.terminal:
        # An executor reports nothing after a job's end.
        return
    status = (
        core.ObservationStatus.FAILED if step.state == "ended" else core.ObservationStatus.PENDING
    )
    run.ended |= step.state == "ended"
    run.world, delivered = fact(run.world, index, run.now, state=step.state, status=status)
    heard: list[core.StrategyEvent] = []
    for event in delivered:
        heard.extend(run.feed(event, dispatch_polls=dispatch_polls).events)
    after = _job(run.world.state, resource)
    earned = after.queue_credit - before.queue_credit
    # Only the interval after a pending fact is queue time, and never more than the cap of it.
    assert 0.0 <= earned <= CAP + 1e-9
    if earned > 0.0:
        assert _pending(before)
        assert before.observation is not None
        assert math.isclose(earned, min(run.now - before.observation.observed_at, CAP))
    noticed = [e for e in heard if isinstance(e, core.JobQueued) and e.resource_id == resource]
    crossed = _pending(after) and after.queue_credit // NOTICE > before.queue_credit // NOTICE
    assert len(noticed) == (1 if crossed else 0)
    for notice in noticed:
        assert notice.pending_reason == "Priority"
        assert notice.queued_s == after.queue_credit


type Job = core.OwnedJob | core.RegisteredOwnedJob
BUDGET = LIMITS.queue_wait_budget


def _settled(job: Job) -> bool:
    return job.terminal and job.status is not core.ObservationStatus.PENDING


def _bound(job: Job, stated: float, run_deadline: float) -> tuple[float, core.WaitEnd]:
    """The oracle: when work on ``job`` with the stated limit ``stated`` ends, and why.

    The limit plus the job's credit. A job whose latest fact is pending (and came before
    that) may still earn the cap, or runs out of queue budget first, whichever comes first
    (the limit on a tie). A limit carried past the run deadline ends with the run.
    """
    at, by = stated + job.queue_credit, core.WaitEnd.LIMIT
    if _pending(job) and job.observation is not None and at > job.observation.observed_at:
        budget_at = job.observation.observed_at + BUDGET - job.queue_credit
        at, by = (
            (at + CAP, core.WaitEnd.LIMIT)
            if at + CAP <= budget_at
            else (budget_at, core.WaitEnd.QUEUE_BUDGET)
        )
    ceiling = max(run_deadline, stated)
    return (at, by) if at <= ceiling else (ceiling, core.WaitEnd.RUN_END)


def _wait_bounds(
    state: core.CoreState, wait: core.Continuation, stated: float
) -> dict[core.ResourceId, tuple[float, core.WaitEnd]]:
    """The oracle bound of each job ``wait`` still waits on: unsettled, budget not spent."""
    spent = {item.resource_id for item in wait.spent}
    return {
        resource: _bound(_job(state, resource), stated, state.run.deadline_at)
        for resource in wait.jobs
        if not _settled(_job(state, resource)) and resource not in spent
    }


def _check_waits(run: Run, before: core.CoreState) -> None:
    """Every wait ended, and every job it ended alone, exactly at its derived bound.

    A job's bound is derived from its facts before the step or after it (a new fact can
    move it into the past); a wait still open after a step has no job whose bound passed.
    """
    # Core's clock: a step the executor never reports (a fact after a job's end) moves nothing.
    state = run.world.state
    now = state.run.now_at
    earlier = {row.continuation_id: row for row in before.evaluation.continuations}
    for wait in state.evaluation.continuations:
        previous = earlier[wait.continuation_id]
        if previous.timeout is not None:
            assert wait.timeout == previous.timeout
            continue
        # The oracle's bounds, from the job facts before and after the step.
        derived = (
            _wait_bounds(before, previous, run.world.stated_at),
            _wait_bounds(state, previous, run.world.stated_at),
        )
        for item in wait.spent[len(previous.spent) :]:
            # A job ended alone exactly when its own queue budget ran out.
            assert item.by is core.WaitEnd.QUEUE_BUDGET
            assert any(
                bounds.get(item.resource_id) == (item.at, core.WaitEnd.QUEUE_BUDGET)
                for bounds in derived
            )
            assert item.at <= now
        timeout = wait.timeout
        if timeout is None:
            assert all(
                at > now for at, _ in _wait_bounds(state, wait, run.world.stated_at).values()
            )
            continue
        assert timeout.reached_at == now
        spent = {item.resource_id: item for item in wait.spent}
        ended = (timeout.deadline_at, timeout.by)
        if ended[1] is core.WaitEnd.QUEUE_BUDGET:
            # Every other job settled after the last budget ran out: the wait ended then.
            assert spent
            assert timeout.deadline_at == max(item.at for item in spent.values())
            assert all(_settled(_job(state, r)) or r in spent for r in wait.jobs)
        else:
            # The earliest bound of a job it still waited on ended the whole wait.
            assert any(
                bounds and min(bounds.values()) == ended
                for bounds in (
                    {r: b for r, b in d.items() if b[1] is not core.WaitEnd.QUEUE_BUDGET}
                    for d in derived
                )
            )
            assert timeout.deadline_at >= run.world.stated_at
        assert timeout.deadline_at <= now
        # The wait froze on the facts before the step's event or after it.
        assert tuple(item.resource_id for item in timeout.unfinished) in {
            tuple(r for r in wait.jobs if r in spent or not _settled(_job(facts, r)))
            for facts in (before, state)
        }
        for item in timeout.unfinished:
            assert item == spent.get(item.resource_id, item)
            if item.resource_id not in spent:
                assert (item.at, item.by) == ended


def _check_record(run: Run, before: core.CoreState, heard: list[core.StrategyEvent]) -> None:
    """A decision's record ends exactly at its derived bound, and only by a limit."""
    if run.world.decision is None:
        return
    (job,) = run.world.jobs
    assert job.submit is not None
    assert job.submit.request_id is not None
    for failure in heard:
        if not isinstance(failure, core.Failed) or failure.decision != run.world.decision:
            continue
        # No fact of a poll or a job's end ever reaches the decision as a failure.
        assert failure.reason in ENDS_OF_A_DECISION_WAIT
        assert failure.limit_at is not None
        record = run.world.state.records.record(job.submit.request_id)
        assert record is not None
        derived = {
            _bound(_job(state, job.resource_id), record.until.at, state.run.deadline_at)
            for state in (before, run.world.state)
        }
        by = (
            core.WaitEnd.QUEUE_BUDGET
            if failure.reason is core.FailureReason.QUEUE_BUDGET_SPENT
            else core.WaitEnd.LIMIT
        )
        assert (failure.limit_at, by) in derived or (
            by is core.WaitEnd.LIMIT and (failure.limit_at, core.WaitEnd.RUN_END) in derived
        )
        assert failure.limit_at <= run.world.state.run.now_at


def _decision_failures(run: Run) -> list[core.Failed]:
    return [
        event
        for event in run.events
        if isinstance(event, core.Failed) and event.decision == run.world.decision
    ]


def _work_ended(run: Run) -> bool:
    state = run.world.state
    if run.world.waits:
        return all(
            wait.timeout is not None
            or all(_job(state, job).terminal for job in wait.jobs)
            or wait.phase is not core.ContinuationPhase.WAITING
            for wait in state.evaluation.continuations
        )
    (job,) = run.world.jobs
    assert job.submit is not None
    assert job.submit.request_id is not None
    record = state.records.record(job.submit.request_id)
    return record is not None and (
        record.failure is not None or _job(state, job.resource_id).terminal
    )


@settings(max_examples=examples(pr=60, nightly=1000), deadline=None)
@given(source=st.sampled_from(Source), history=histories, dispatch_polls=st.booleans())
def test_every_wait_on_a_job_ends_once_by_a_bound_derived_from_its_queue_time(
    source: Source, history: list[Step], *, dispatch_polls: bool
) -> None:
    run = Run(BUILDERS[source](LIMITS))
    for step in history:
        before, heard = run.world.state, len(run.events)
        _apply(run, step, dispatch_polls=dispatch_polls)
        if run.world.waits:
            _check_waits(run, before)
        _check_record(run, before, run.events[heard:])
    # The run's end bounds everything: past it, every wait and record has ended.
    before, heard = run.world.state, len(run.events)
    run.now = max(run.now, run.world.state.run.deadline_at) + 1.0
    run.feed(core.ClockAdvanced(now_at=run.now), dispatch_polls=False)
    if run.world.waits:
        _check_waits(run, before)
    _check_record(run, before, run.events[heard:])
    assert _work_ended(run)
    resumed = [e for e in run.events if isinstance(e, core.ResumeAuthorized)]
    assert len({e.continuation_id for e in resumed}) == len(resumed)
    if run.world.decision is not None:
        # A strategy decision hears at most one failure.
        assert len(_decision_failures(run)) <= 1


def _queue(source: Source, until: float, gap: float) -> Run:
    """Every job of ``source`` reports pending every ``gap`` seconds until ``until``."""
    run = Run(BUILDERS[source](LIMITS))
    while run.now + gap <= until:
        for index in range(len(run.world.jobs)):
            _apply(run, Fact(job=index, state="pending", gap=0.0), dispatch_polls=False)
        run.now += gap
    run.feed(core.ClockAdvanced(now_at=run.now), dispatch_polls=False)
    return run


def _ended_by(run: Run) -> tuple[core.WaitEnd | core.FailureReason, float]:
    if run.world.waits:
        (wait,) = run.world.state.evaluation.continuations
        assert wait.timeout is not None
        return wait.timeout.by, wait.timeout.deadline_at
    (failure,) = _decision_failures(run)
    assert failure.limit_at is not None
    return failure.reason, failure.limit_at


@given(source=st.sampled_from(Source))
def test_a_job_that_never_leaves_the_queue_ends_its_work_when_its_queue_budget_is_spent(
    source: Source,
) -> None:
    # Fresh polls credit the whole queue: 400 s of it spends the budget at 401.
    run = _queue(source, until=LIMITS.queue_wait_budget + 40.0, gap=10.0)
    reason, at = _ended_by(run)
    expected = (
        core.WaitEnd.QUEUE_BUDGET if run.world.waits else core.FailureReason.QUEUE_BUDGET_SPENT
    )
    assert reason == expected
    assert at == 1.0 + LIMITS.queue_wait_budget
    assert any(isinstance(event, core.JobQueued) for event in run.events)


@given(source=st.sampled_from(Source))
def test_an_executor_outage_while_a_job_queues_is_credited_no_more_than_the_cap(
    source: Source,
) -> None:
    # One pending fact at 1 and nothing until 500: only the cap after it is queue time, so
    # the stated limit ends 30 s late, not at the next answer.
    run = Run(BUILDERS[source](LIMITS))
    run.now = 500.0
    run.feed(core.ClockAdvanced(now_at=run.now), dispatch_polls=False)
    reason, at = _ended_by(run)
    assert reason in {core.WaitEnd.LIMIT, core.FailureReason.TIMED_OUT}
    assert at == run.world.stated_at + CAP
