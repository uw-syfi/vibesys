"""A cancel stop cancels the jobs the run itself owns; a drain stop waits for them.

Attempt closure drains an attempt's jobs, but a measurement the run submits in its own
scope (the input baseline) has no attempt to close. Without a run-level cancel, a run
stopped with ``StopMode.CANCEL`` while that job is queued stays CLOSING until the job
ends on its own, which a held cluster job never does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_measurements import committed, observation, plan, requested

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_JOB = core.ResourceId(root="job")


def _stop(mode: core.StopMode, now_at: float) -> core.RunControlEvent:
    return core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="stop"), action="stop", mode=mode),
        result=core.RunResultProposal(outcome="cancelled", reason="operator stop"),
        now_at=now_at,
    )


def _answer(request: core.Request, now_at: float) -> core.Observation:
    """A well-behaved executor: a cancel ends the job, anything else simply succeeds."""
    cancelled = isinstance(request, core.CancelOwnedJob)
    request_id = request.request_id or core.RequestId(root="unnamed")
    return core.Observation(
        event_id=core.EventId(root=f"answer:{request_id.root}"),
        request_id=request_id,
        scope=request.scope,
        sequence=0,
        observed_at=now_at,
        status=core.ObservationStatus.CANCELLED if cancelled else core.ObservationStatus.SUCCEEDED,
        resource_id=request.resource_id if cancelled else None,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        admission_id=request.admission_id,
    )


class _Executor(NamedTuple):
    """The far side: the jobs it holds, and whether it claims to hold no record of them."""

    jobs: Mapping[core.ResourceId, core.SubmitMeasurement]
    no_record: bool = False


def _settle(
    state: core.CoreState,
    requests: Iterable[core.Request],
    now_at: float,
    sent: list[core.Request],
    executor: _Executor,
) -> core.CoreState:
    """Authorize and answer every request core sends, until it sends no more.

    A cancel's answer also reports the job itself ended, as the runtime's does. With
    ``executor.no_record`` the executor answers every poll and inspection of a job with "I hold no
    record of it" (unknown, unaccepted), as it does for a job it lost or never began.
    """
    pending = list(requests)
    for _ in range(50):
        if not pending:
            break
        request = pending.pop(0)
        sent.append(request)
        if request.request_id is None:
            continue
        state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
        if executor.no_record and isinstance(request, core.InspectOwnedJob | core.ObserveOwnedJob):
            result = core.step(state, core.RequestObserved(observation=_answer(request, now_at)))
            pending.extend(result.requests)
            unknown = observation(
                executor.jobs[request.resource_id],
                resource_id=request.resource_id,
                sequence=len(sent) + 100,
                status=core.ObservationStatus.UNKNOWN,
                accepted=False,
                diagnostic="executor holds no record of the job",
                observed_at=now_at,
            )
            result = core.step(
                result.state, core.JobObserved(resource_id=request.resource_id, observation=unknown)
            )
            pending.extend(result.requests)
            state = result.state
            continue
        result = core.step(state, core.RequestObserved(observation=_answer(request, now_at)))
        pending.extend(result.requests)
        if isinstance(request, core.CancelOwnedJob) and request.resource_id in executor.jobs:
            ended = observation(
                executor.jobs[request.resource_id],
                resource_id=request.resource_id,
                sequence=len(sent) + 1000,
                status=core.ObservationStatus.CANCELLED,
                terminal=True,
                released=True,
                children_complete=True,
                observed_at=now_at,
            )
            result = core.step(
                result.state, core.JobObserved(resource_id=request.resource_id, observation=ended)
            )
            pending.extend(result.requests)
        state = result.state
    return state


def _accept(
    state: core.CoreState, job: core.SubmitMeasurement
) -> tuple[core.CoreState, tuple[core.Request, ...]]:
    """The executor accepts the submission as job ``job``, still queued."""
    accepted = observation(job, children_complete=True)
    state = committed(state, accepted)
    submission = core.step(state, core.MeasurementSubmissionObserved(observation=accepted))
    observed = core.step(submission.state, core.JobObserved(resource_id=_JOB, observation=accepted))
    return observed.state, (*submission.requests, *observed.requests)


def _stopped(
    mode: core.StopMode, *, accepted_before_stop: bool, ticks: tuple[float, ...]
) -> tuple[core.CoreState, list[core.Request]]:
    """Stop a run whose own measurement job is queued, then drive it as the shell would.

    The job is accepted before the stop, or only after it: a submission already sent when
    the stop commits is answered later, and its job must still be cancelled.
    """
    asked = requested()
    job = asked.requests[0]
    assert isinstance(job, core.SubmitMeasurement)
    assert job.request_id is not None
    state = core.step(asked.state, core.DispatchAuthorized(request_id=job.request_id)).state
    sent: list[core.Request] = []
    if accepted_before_stop:
        state, _ = _accept(state, job)
    result = core.step(state, _stop(mode, 2.0))
    state = _settle(result.state, result.requests, 2.0, sent, _Executor({_JOB: job}))
    if not accepted_before_stop:
        state, requests = _accept(state, job)
        state = _settle(state, requests, 2.5, sent, _Executor({_JOB: job}))
    for tick in ticks:
        result = core.step(state, core.ClockAdvanced(now_at=tick))
        state = _settle(result.state, result.requests, tick, sent, _Executor({_JOB: job}))
    return state, sent


def _cancels(sent: Iterable[core.Request]) -> int:
    return sum(isinstance(r, core.CancelOwnedJob) and r.resource_id == _JOB for r in sent)


def test_a_cancel_stop_cancels_the_runs_queued_measurement_and_closes() -> None:
    state, sent = _stopped(core.StopMode.CANCEL, accepted_before_stop=True, ticks=(4.0,))
    assert _cancels(sent) == 1
    assert state.run.status == core.RunStatus.TERMINAL


@settings(max_examples=examples(pr=4, nightly=100))
@given(
    accepted_before_stop=st.booleans(),
    ticks=st.lists(st.floats(min_value=3.0, max_value=50.0), max_size=3).map(
        lambda xs: tuple(sorted(xs))
    ),
)
def test_a_cancel_stop_cancels_the_runs_own_jobs_once_and_closes(
    *, accepted_before_stop: bool, ticks: tuple[float, ...]
) -> None:
    state, sent = _stopped(
        core.StopMode.CANCEL, accepted_before_stop=accepted_before_stop, ticks=(*ticks, 60.0)
    )
    assert _cancels(sent) == 1
    assert state.run.status == core.RunStatus.TERMINAL


@given(accepted_before_stop=st.booleans())
def test_a_drain_stop_lets_the_runs_own_job_finish(*, accepted_before_stop: bool) -> None:
    state, sent = _stopped(
        core.StopMode.DRAIN, accepted_before_stop=accepted_before_stop, ticks=(4.0, 20.0)
    )
    assert _cancels(sent) == 0
    assert state.run.status == core.RunStatus.CLOSING


def _reload(state: core.CoreState) -> core.CoreState:
    """A host restart: the whole state through the envelope codec."""
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = core.OperationRegistry()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def test_a_late_observation_of_an_unconfirmed_job_cancels_it_after_the_stops_inspection() -> None:
    """A stop that finds the job unconfirmed inspects it; if its next observation still does
    not confirm it, the stop cancels it, once, instead of asking for the inspection again.

    The inspection's id names the job's step. The stop stamped it with its own decision and
    the observation then asked for the same id under the measurement's decision, which the
    request ledger rejects as an identity conflict and halts the shell. And a stop that only
    inspected, whatever the executor answered ("no record" included), left the run closing
    until the host gave up.
    """
    asked = requested()
    job = asked.requests[0]
    assert isinstance(job, core.SubmitMeasurement)
    assert job.request_id is not None
    state = core.step(asked.state, core.DispatchAuthorized(request_id=job.request_id)).state
    unconfirmed = observation(
        job, status=core.ObservationStatus.UNKNOWN, diagnostic="executor holds no record"
    )
    state = committed(state, unconfirmed)
    state = core.step(state, core.MeasurementSubmissionObserved(observation=unconfirmed)).state
    state = core.step(state, core.JobObserved(resource_id=_JOB, observation=unconfirmed)).state
    stopped = core.step(state, _stop(core.StopMode.CANCEL, 2.0))
    assert [type(r) for r in stopped.requests] == [core.InspectOwnedJob]
    late = core.step(stopped.state, core.JobObserved(resource_id=_JOB, observation=unconfirmed))
    assert [type(r) for r in late.requests] == [core.CancelOwnedJob]
    again = core.step(late.state, core.JobObserved(resource_id=_JOB, observation=unconfirmed))
    assert again.requests == ()


_QUEUE_WAIT_LIMIT = 8.0

_STATUSES = (
    ("unknown", core.ObservationStatus.UNKNOWN, False),
    ("unknown-accepted", core.ObservationStatus.UNKNOWN, True),
    ("pending", core.ObservationStatus.PENDING, True),
)

_OPS = st.one_of(
    st.tuples(st.just("tick"), st.sampled_from((0.0, 0.0, 1.0, 3.0))),
    st.tuples(st.just("observe"), st.integers(0, 2), st.sampled_from(_STATUSES)),
    st.tuples(st.just("stop"), st.sampled_from((core.StopMode.CANCEL, core.StopMode.DRAIN))),
    st.tuples(st.just("replay"), st.integers(0, 2)),
    st.tuples(st.just("restart")),
)


def _agent_call(
    state: core.CoreState, call_id: str, measurement: core.MeasurementPlan
) -> core.Transition:
    """An agent's in-turn measurement: core allocates its submission, which has no decision."""
    return core.step(
        state,
        core.AgentMeasurementRequested(
            scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
            plan=measurement,
            within=core.Within(limit=100.0),
            call_id=call_id,
        ),
    )


class _Run:
    """The run's own measurement jobs, driven by hand: the events the shell would feed core.

    Core refuses a second measurement of a plan the scope already measures, so each job
    measures a plan of its own (a distinct evaluator). A job is a strategy's (a ``Measure``
    decision) or, where ``agents`` says so, an agent's call, whose submission has no decision.
    """

    def __init__(
        self, count: int, *, no_record: bool = False, agents: tuple[bool, ...] = ()
    ) -> None:
        self.no_record = no_record
        self.state = core.initial_state()
        self.jobs: dict[core.ResourceId, core.SubmitMeasurement] = {}
        for index in range(count):
            measurement = plan(evaluator_digest=f"evaluator-{index}")
            asked = (
                _agent_call(self.state, f"call-{index}", measurement)
                if index < len(agents) and agents[index]
                else requested(self.state, identity=f"measure-{index}", measurement=measurement)
            )
            job = asked.requests[0]
            assert isinstance(job, core.SubmitMeasurement)
            assert job.request_id is not None
            self.state = core.step(
                asked.state, core.DispatchAuthorized(request_id=job.request_id)
            ).state
            self.jobs[core.ResourceId(root=f"job-{index}")] = job
        self.resources = list(self.jobs)
        self.sent: list[core.Request] = []
        self.now = 1.0
        self.mode: core.StopMode | None = None
        self.sequences = dict.fromkeys(self.jobs, 0)
        self.submitted: set[core.ResourceId] = set()
        self.last: dict[core.ResourceId, tuple[core.Observation, bool]] = {}
        if no_record:
            # The executor took each job, and will later claim to hold no record of it.
            for index in range(count):
                self.observe(index, core.ObservationStatus.PENDING, accepted=True)

    @property
    def executor(self) -> _Executor:
        return _Executor(self.jobs, self.no_record)

    def _step(self, event: core.CoreEvent) -> None:
        result = core.step(self.state, event)
        self.state = result.state
        self.sent.extend(result.requests)

    def feed(self, event: core.CoreEvent) -> None:
        """Step core with an event the shell received, keeping what core asks for."""
        self._step(event)

    def tick(self, seconds: float) -> None:
        """Let time pass, but not past the queue allowance (10 s): an unconfirmed job that
        outlives it is cancelled by core itself, which is not the stop under test."""
        if self.now + seconds > _QUEUE_WAIT_LIMIT:
            return
        self.now += seconds
        self._step(core.ClockAdvanced(now_at=self.now))

    def observe(
        self,
        index: int,
        status: core.ObservationStatus,
        *,
        accepted: bool,
        **updates: object,
    ) -> None:
        resource = self.resources[index % len(self.resources)]
        self.sequences[resource] += 1
        seen = observation(
            self.jobs[resource],
            sequence=self.sequences[resource],
            resource_id=resource,
            status=status,
            accepted=accepted,
            observed_at=self.now,
            **updates,
        )
        self.deliver(resource, seen, accepted=accepted)

    def deliver(self, resource: core.ResourceId, seen: core.Observation, *, accepted: bool) -> None:
        self.last[resource] = (seen, accepted)
        self.state = committed(self.state, seen)
        if accepted and resource not in self.submitted:
            self.submitted.add(resource)
            self._step(core.MeasurementSubmissionObserved(observation=seen))
        self._step(core.JobObserved(resource_id=resource, observation=seen))

    def replay(self, index: int) -> None:
        """The last observation again: a poll answered twice, or replayed after a restart."""
        resource = self.resources[index % len(self.resources)]
        if resource in self.last:
            seen, accepted = self.last[resource]
            self.deliver(resource, seen, accepted=accepted)

    def stop(self, mode: core.StopMode) -> None:
        if self.mode is None:
            self.mode = mode
            self._step(_stop(mode, self.now))

    def apply(self, op: tuple[object, ...]) -> None:
        match op:
            case ("tick", float(seconds)):
                self.tick(seconds)
            case ("observe", int(index), (_, core.ObservationStatus() as status, bool(accepted))):
                self.observe(index, status, accepted=accepted)
            case ("stop", core.StopMode() as mode):
                self.stop(mode)
            case ("replay", int(index)):
                self.replay(index)
            case ("restart",):
                self.state = _reload(self.state)

    def settle_out(self) -> None:
        """Answer whatever core asked. The executor confirms every job at last, or, when it
        holds no record, never does and cancellation is all that can end a job.
        """
        if not self.no_record:
            for index in range(len(self.resources)):
                self.observe(index, core.ObservationStatus.PENDING, accepted=True)
        asked: list[core.Request] = []
        self.state = _settle(self.state, self._unanswered(), self.now, asked, self.executor)
        self.sent.extend(asked)
        self.settle_over((1.0,) * 6 if self.no_record else (5.0, 10.0, 70.0))

    def answer_now(self) -> None:
        """Answer, without the clock moving, whatever core asked and the shell has yet to."""
        asked: list[core.Request] = []
        self.state = _settle(self.state, self._unanswered(), self.now, asked, self.executor)
        self.sent.extend(asked)

    def end_jobs(self) -> None:
        """Every job finishes by itself."""
        for resource, job in self.jobs.items():
            self.sequences[resource] = max(self.sequences[resource], 2000) + 1
            ended = observation(
                job,
                sequence=self.sequences[resource],
                resource_id=resource,
                status=core.ObservationStatus.SUCCEEDED,
                terminal=True,
                released=True,
                children_complete=True,
                observed_at=self.now,
            )
            self._step(core.RequestObserved(observation=ended))
            self._step(core.JobObserved(resource_id=resource, observation=ended))
        self.state = _settle(self.state, self._unanswered(), self.now, [], _Executor(self.jobs))
        self.settle_over((5.0, 10.0))

    def settle_over(self, laters: tuple[float, ...]) -> None:
        """Let the clock run on, answering what core asks at each tick."""
        for later in laters:
            self.now += later
            result = core.step(self.state, core.ClockAdvanced(now_at=self.now))
            self.state = _settle(
                result.state,
                result.requests,
                self.now,
                self.sent,
                self.executor,
            )

    def _unanswered(self) -> list[core.Request]:
        """Each request core sent so far, once: the ones the shell has yet to answer."""
        once = {r.request_id: r for r in self.sent if r.request_id is not None}
        return [
            r
            for r in once.values()
            if any(
                row.request_id == r.request_id and row.phase == core.RequestPhase.PREPARED
                for row in self.state.records.records
            )
        ]

    def misattributed(self) -> list[core.Request]:
        """The steps on a job that name a decision other than its submission's.

        A poll, inspection or cancel is the job's own, whoever asked for it; an agent's job
        has no decision, so its steps have none either.
        """
        return [
            r
            for r in self.sent
            if isinstance(
                r,
                core.ObserveOwnedJob
                | core.InspectOwnedJob
                | core.CancelOwnedJob
                | core.CollectEvidence,
            )
            and r.resource_id in self.jobs
            and r.decision_id != self.jobs[r.resource_id].decision_id
        ]

    def cancels(self, resource: core.ResourceId) -> int:
        return len(
            {
                r.request_id
                for r in self.sent
                if isinstance(r, core.CancelOwnedJob) and r.resource_id == resource
            }
        )


@settings(max_examples=examples(pr=3, nightly=500), deadline=None, database=None)
@example(count=1, ops=[("stop", core.StopMode.CANCEL)], no_record=True, agents=())
@example(
    count=2,
    ops=[("observe", 0, _STATUSES[0]), ("tick", 0.0), ("stop", core.StopMode.CANCEL)],
    no_record=True,
    agents=(),
)
@example(
    count=2,
    ops=[
        ("observe", 0, _STATUSES[2]),
        ("observe", 1, _STATUSES[2]),
        ("stop", core.StopMode.CANCEL),
    ],
    no_record=False,
    agents=(False, True),
)
@given(
    count=st.integers(1, 3),
    ops=st.lists(_OPS, max_size=16),
    no_record=st.booleans(),
    agents=st.tuples(st.booleans(), st.booleans(), st.booleans()),
)
def test_polls_stops_and_observations_at_shared_instants_never_conflict(
    count: int, ops: list[tuple[object, ...]], *, no_record: bool, agents: tuple[bool, ...]
) -> None:
    """Whatever order polls, stops, observations, replays and restarts come in, at the same
    instant or not, on one to three run-owned jobs, core never issues one request id with two
    payloads and cancels each job at most once. A cancel stop closes the run with every job
    ended; a drain stop cancels nothing, keeps the run CLOSING while a job lives, and closes
    it once every job finishes.

    Run r2 (2026-10-17): a cancel stop ended an agent's job (its submission has no decision)
    beside a strategy's. Each signal of the stop asked for the agent job's cancellation, and
    core stamped each copy with the decision of the signal that asked (the stop, then the
    strategy job's measure), so one cancel id carried two payloads and the run halted.
    """
    run = _Run(count, no_record=no_record, agents=agents)
    for op in ops:
        if run.state.run.status == core.RunStatus.TERMINAL:
            break
        run.apply(op)
    run.settle_out()
    assert all(run.cancels(resource) <= 1 for resource in run.resources)
    assert run.misattributed() == []
    match run.mode:
        case core.StopMode.CANCEL:
            assert run.state.run.status == core.RunStatus.TERMINAL
            assert all(run.cancels(resource) == 1 for resource in run.resources)
        case core.StopMode.DRAIN:
            assert all(run.cancels(resource) == 0 for resource in run.resources)
            assert run.state.run.status == core.RunStatus.CLOSING
            run.no_record = False
            run.end_jobs()
            assert run.state.run.status == core.RunStatus.TERMINAL


def _authorize(state: core.CoreState, requests: Iterable[core.Request]) -> core.CoreState:
    for request in requests:
        assert request.request_id is not None
        state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
    return state


def _polls_blocked_by_late_restart(run: _Run) -> core.CoreState:
    """Both jobs' polls in flight when the host died, blocked once it came back too late.

    Every recovery inspection then lapses unanswered, so recovery stays BLOCKED and nothing
    addresses the blocked polls any more: a closing run ends them itself.
    """
    for index in range(len(run.resources)):
        run.observe(index, core.ObservationStatus.PENDING, accepted=True)
    timers = [job.wait.timer for job in run.state.evaluation.jobs if job.wait is not None]
    polled = core.step(run.state, core.ClockAdvanced(now_at=max(t for t in timers if t)))
    state = _authorize(polled.state, polled.requests)
    polls = [row for row in state.records.records if isinstance(row.request, core.ObserveOwnedJob)]
    restart = max(poll.until.at for poll in polls) + 1.0
    state = core.step(state, core.RecoveryStarted(epoch=2, now_at=restart)).state
    late = restart + state.run.limits.reconciliation_bound
    for poll in polls:
        result = core.step(
            state, core.ReconciliationDeadline(request_id=poll.request_id, now_at=late)
        )
        state = result.state
        for block in result.requests:
            if isinstance(block, core.BlockRequest):
                state = _authorize(state, (block,))
                answer = _answer(block, late).model_copy(
                    update={"admission_id": block.admission_id}
                )
                state = core.step(state, core.RequestObserved(observation=answer)).state
    inspections = [
        row.request
        for row in state.records.records
        if row.phase == core.RequestPhase.PREPARED
        and isinstance(row.request, core.InspectRequest)
        and any(row.request.target == poll.request_id for poll in polls)
    ]
    state = _authorize(state, inspections)
    lapsed = max(row.until.at for row in state.records.records) + 1.0
    return core.step(state, core.ClockAdvanced(now_at=lapsed)).state


@pytest.mark.regression
def test_a_cancel_stop_that_ends_a_strategy_jobs_blocked_poll_cancels_an_agents_job_once() -> None:
    """One cancel stop and the strategy job it ends both ask to cancel an agent's job.

    Run r2 (2026-10-17): a recovery-blocked cancel stop asked to cancel an agent's job (its
    submission has no decision), stamped with the stop's decision. In the same step the
    closing run ended the strategy job's blocked poll, whose forwarded reading (caused by the
    strategy's measure) asked for the same cancel again. Core stamped that copy with the
    measure, so one cancel id carried two payloads and the run halted with "request identity
    conflict". A job's steps are its own: both copies carry the agent job's (absent) decision.
    """
    run = _Run(2, agents=(True, False))
    [agent_job] = [resource for resource, job in run.jobs.items() if job.decision_id is None]
    state = _polls_blocked_by_late_restart(run)
    assert state.records.recovery.phase == core.RecoveryPhase.BLOCKED
    stopped = core.step(state, _stop(core.StopMode.CANCEL, state.run.now_at))
    cancels = [
        request
        for request in stopped.requests
        if isinstance(request, core.CancelOwnedJob) and request.resource_id == agent_job
    ]
    assert len(cancels) == 1
    assert cancels[0].decision_id is None


@settings(max_examples=examples(pr=6, nightly=200), deadline=None, database=None)
@example(count=1, mode=core.StopMode.DRAIN, no_record=True)
@example(count=1, mode=core.StopMode.DRAIN, no_record=False)
@given(
    count=st.integers(1, 3),
    mode=st.sampled_from((core.StopMode.CANCEL, core.StopMode.DRAIN)),
    no_record=st.booleans(),
)
def test_a_run_past_its_deadline_cancels_its_own_jobs_whatever_the_stop(
    count: int, mode: core.StopMode, *, no_record: bool
) -> None:
    """Chaos seed 2578: a drain waits for the run's own jobs only while the run has time left.

    Each job stays queued for good, or the executor holds no record of it (its sbatch was
    lost). A drain stop lets such a job be until the run deadline; from then on core
    inspects it and cancels it, once, as a cancel stop does, and the run closes. Before,
    a drained run past its deadline polled the job forever.
    """
    run = _Run(count, no_record=no_record)
    for index in range(count):
        run.observe(index, core.ObservationStatus.PENDING, accepted=True)
    run.stop(mode)
    run.answer_now()
    deadline = run.state.run.deadline_at
    run.settle_over((5.0, 10.0))
    if mode == core.StopMode.DRAIN:
        assert run.state.run.status == core.RunStatus.CLOSING
        assert all(run.cancels(resource) == 0 for resource in run.resources)
    run.settle_over((deadline - run.now, *(10.0,) * 4))
    assert run.state.run.status == core.RunStatus.TERMINAL
    assert all(run.cancels(resource) == 1 for resource in run.resources)


def _operator_stop(index: int, mode: core.StopMode, now_at: float) -> core.RunControlEvent:
    """An operator's stop as the run's control bridge records it: a control, with no result."""
    control = core.ControlInput(
        control_id=core.ControlId(root=f"op-stop-{index}"), action="stop", mode=mode
    )
    return core.RunControlEvent(control=control, now_at=now_at)


def _strategy_finish(state: core.CoreState, mode: core.StopMode) -> core.DecisionSubmitted:
    """The search's own end of the run, with the mode it read from the first stop it saw."""
    finish = core.Finish(
        decision_id=core.DecisionId(root="finish"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        mode=mode.value,
        result=core.RunResultProposal(outcome="cancelled", reason="operator stop"),
    )
    return core.DecisionSubmitted(decision=finish, expected_revision=state.revision)


@pytest.mark.regression
@settings(max_examples=examples(pr=6, nightly=300), deadline=None, database=None)
@example(count=1, stops=[core.StopMode.DRAIN, core.StopMode.CANCEL], finish_at=1, restart=False)
@example(count=1, stops=[core.StopMode.CANCEL, core.StopMode.DRAIN], finish_at=0, restart=False)
@example(count=2, stops=[core.StopMode.DRAIN, core.StopMode.CANCEL], finish_at=2, restart=True)
@given(
    count=st.integers(1, 2),
    stops=st.lists(st.sampled_from(core.StopMode), min_size=1, max_size=4),
    finish_at=st.integers(0, 4),
    restart=st.booleans(),
)
def test_a_stop_only_escalates_whatever_order_its_requests_come_in(
    count: int, stops: list[core.StopMode], finish_at: int, *, restart: bool
) -> None:
    """periodic-1330 MED-2: a cancel after a drain was recorded, and changed nothing.

    Operator stops arrive in any order, before or after the search commits its end of the
    run (which carries the first stop's mode, or a drain when the run ended by itself).
    The run's stop is the strongest asked for: one cancel anywhere cancels every job the
    run owns, once, and closes the run; only drains let every job finish. ``restart``
    reloads core after every step, so an escalation is durable.
    """
    run = _Run(count)
    for index in range(count):
        run.observe(index, core.ObservationStatus.PENDING, accepted=True)
    finished = False
    for index, mode in enumerate([*stops, None]):
        if run.state.run.status == core.RunStatus.TERMINAL:
            break
        if not finished and (index >= finish_at or mode is None):
            finished = True
            run.feed(_strategy_finish(run.state, stops[0] if index else core.StopMode.DRAIN))
        if mode is not None and run.state.run.status != core.RunStatus.TERMINAL:
            run.feed(_operator_stop(index, mode, run.now))
        if restart:
            run.state = _reload(run.state)
    run.mode = core.StopMode.CANCEL if core.StopMode.CANCEL in stops else core.StopMode.DRAIN
    run.settle_out()
    assert all(run.cancels(resource) <= 1 for resource in run.resources)
    if run.mode is core.StopMode.CANCEL:
        assert run.state.run.status == core.RunStatus.TERMINAL
        assert all(run.cancels(resource) == 1 for resource in run.resources)
    else:
        assert all(run.cancels(resource) == 0 for resource in run.resources)
        assert run.state.run.status == core.RunStatus.CLOSING
        run.end_jobs()
        assert run.state.run.status == core.RunStatus.TERMINAL
