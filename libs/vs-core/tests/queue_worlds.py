"""Runs with work waiting on jobs, for every source of a job, driven through ``core.step``.

Each world holds one kind of work tied to its jobs: an agent's wait on its own submission, an
agent's wait on a submission a strategy's ``Measure`` allocated (a repeat call), a strategy's
``Measure`` alone (no wait; its record is the work), a wait on a registered operation's job,
and one wait on two agent submissions. ``fact`` turns a job fact into the event the shell
delivers for it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import vs_core.api as core
from vs_core.api.testing import sample_limits

from .proof_digest import value_digest
from .test_continuations import fixture
from .test_measurements import observation, plan
from .test_registered_v3_contracts import (
    RegisteredMeasurement,
    expected_identity,
    measurement_codec,
)

LIMIT = 60.0
"""Every source's stated limit, counted from time 0."""


class Source(StrEnum):
    """Where the work tied to a job comes from."""

    AGENT = "agent"
    AGENT_REPEAT = "agent_repeat"
    MEASURE = "measure"
    REGISTERED = "registered"
    MULTI = "multi"


@dataclass(frozen=True)
class Job:
    """One job of a world: its identity and how the shell reports it."""

    resource_id: core.ResourceId
    submit: core.SubmitMeasurement | None = None
    operation: core.ExecuteRegisteredOperation | None = None
    sequence: int = 1


@dataclass(frozen=True)
class World:
    """A run whose work waits on ``jobs``, each already reported once (pending, at time 1)."""

    source: Source
    state: core.CoreState
    jobs: tuple[Job, ...]
    stated_at: float
    """The stated deadline of the work: the wait's, or the record's ``until``."""
    decision: core.DecisionId | None = None
    """The strategy decision the work answers, for a ``Measure``."""
    codec: core.OperationRegistry | None = None

    @property
    def waits(self) -> bool:
        """Whether an agent's wait is the work (else a strategy's record is)."""
        return self.source != Source.MEASURE


def limits(**overrides: object) -> core.Limits:
    """Run limits small enough for a test to reach every bound before the run deadline."""
    chosen: dict[str, object] = {
        "observe_interval": 10.0,
        "observe_backoff_cap": 30.0,
        "reconciliation_bound": 20.0,
        "queue_wait_budget": 400.0,
        "queue_notice_interval": 100.0,
    }
    return sample_limits(**{**chosen, **overrides})


def _with_limits(state: core.CoreState, run_limits: core.Limits) -> core.CoreState:
    return state.model_copy(update={"run": state.run.model_copy(update={"limits": run_limits})})


def fact(
    world: World,
    index: int,
    at: float,
    *,
    state: str,
    status: core.ObservationStatus = core.ObservationStatus.PENDING,
) -> tuple[World, tuple[core.CoreEvent, ...]]:
    """The events reporting job ``index`` at ``at``: pending, running, or ended with ``status``."""
    job = world.jobs[index]
    sequence = job.sequence + 1
    terminal = status != core.ObservationStatus.PENDING
    source = job.submit if job.submit is not None else job.operation
    assert source is not None
    assert source.request_id is not None
    observed = core.Observation(
        event_id=core.EventId(root=f"{job.resource_id.root}-{sequence}"),
        request_id=source.request_id,
        scope=source.scope,
        admission_id=source.admission_id,
        sequence=sequence,
        observed_at=at,
        status=status,
        resource_id=job.resource_id,
        accepted=True,
        terminal=terminal,
        released=terminal,
        children_complete=terminal,
    )
    progress = (
        None
        if terminal
        else core.JobProgress(
            observation_sequence=sequence,
            observed_at=at,
            state="pending" if state == "pending" else "running",
            pending_reason="Priority" if state == "pending" else None,
        )
    )
    jobs = tuple(
        replace(row, sequence=sequence) if position == index else row
        for position, row in enumerate(world.jobs)
    )
    moved = replace(world, jobs=jobs)
    if job.submit is not None:
        return moved, (
            core.JobObserved(resource_id=job.resource_id, observation=observed, progress=progress),
        )
    assert job.operation is not None
    # Records forward a registered operation's fact to its job as ``RegisteredJobObserved``.
    return moved, (core.RequestObserved(observation=observed, progress=progress),)


def first_fact(
    state: core.CoreState, submit: core.SubmitMeasurement, job: core.ResourceId
) -> core.CoreState:
    assert submit.request_id is not None
    state = core.step(state, core.DispatchAuthorized(request_id=submit.request_id)).state
    first = observation(submit, 1, resource_id=job)
    state = core.step(state, core.RequestObserved(observation=first)).state
    progress = core.JobProgress(
        observation_sequence=1, observed_at=first.observed_at, state="pending"
    )
    return core.step(
        state, core.JobObserved(resource_id=job, observation=first, progress=progress)
    ).state


def agent_call(
    state: core.CoreState, measurement: core.MeasurementPlan, call_id: str
) -> tuple[core.CoreState, core.SubmitMeasurement | None, core.AgentCall]:
    scope = core.Scope(owner=state.run.run_id, generation=state.run.generation)
    called = core.step(
        state,
        core.AgentMeasurementRequested(
            scope=scope, plan=measurement, within=core.Within(limit=LIMIT), call_id=call_id
        ),
    )
    submits = tuple(r for r in called.requests if isinstance(r, core.SubmitMeasurement))
    call = called.state.evaluation.agent_calls[-1]
    assert call.request_id is not None
    return called.state, (submits[0] if submits else None), call


def suspend(
    state: core.CoreState, template: core.Continuation, jobs: tuple[core.ResourceId, ...], at: float
) -> core.CoreState:
    wait = template.model_copy(update={"jobs": jobs, "deadline_at": at})
    state = core.step(state, core.TurnSuspended(continuation=wait)).state
    (recorded,) = state.evaluation.continuations
    assert recorded.timeout is None
    return state


def base(run_limits: core.Limits) -> tuple[core.CoreState, core.Continuation]:
    state, template = fixture(run_owned=True)
    state = state.model_copy(update={"evaluation": core.EvaluationState()})
    return _with_limits(state, run_limits), template


def agent(run_limits: core.Limits) -> World:
    """An agent's wait on the one submission its call allocated."""
    state, template = base(run_limits)
    job = core.ResourceId(root="job")
    state, submit, call = agent_call(state, plan(), "call")
    assert submit is not None
    assert call.deadline_at is not None
    state = first_fact(state, submit, job)
    state = suspend(state, template, (job,), call.deadline_at)
    return World(Source.AGENT, state, (Job(job, submit=submit),), call.deadline_at)


def multi(run_limits: core.Limits) -> World:
    """One agent wait on two submissions of two different candidates."""
    state, template = base(run_limits)
    jobs: list[Job] = []
    deadline = 0.0
    for index, purpose in enumerate(("baseline", "official")):
        resource = core.ResourceId(root=f"job-{index}")
        state, submit, call = agent_call(state, plan(purpose=purpose), f"call-{index}")
        assert submit is not None
        assert call.deadline_at is not None
        state = first_fact(state, submit, resource)
        jobs.append(Job(resource, submit=submit))
        deadline = call.deadline_at
    state = suspend(state, template, tuple(job.resource_id for job in jobs), deadline)
    return World(Source.MULTI, state, tuple(jobs), deadline)


def _measured(state: core.CoreState) -> tuple[core.CoreState, core.SubmitMeasurement]:
    decision = core.Measure(
        decision_id=core.DecisionId(root="measure"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        plan=plan(),
    )
    result = core.step(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=LIMIT)),
            expected_revision=state.revision,
        ),
    )
    (submit,) = (r for r in result.requests if isinstance(r, core.SubmitMeasurement))
    return result.state, submit


def measure(run_limits: core.Limits) -> World:
    """A strategy's ``Measure``: its submission's record is the work, no wait."""
    state = _with_limits(core.initial_state(), run_limits)
    state, submit = _measured(state)
    job = core.ResourceId(root="job")
    state = first_fact(state, submit, job)
    assert submit.request_id is not None
    record = state.records.record(submit.request_id)
    assert record is not None
    return World(
        Source.MEASURE,
        state,
        (Job(job, submit=submit),),
        record.until.at,
        decision=core.DecisionId(root="measure"),
    )


def agent_repeat(run_limits: core.Limits) -> World:
    """An agent's wait on a submission a strategy's ``Measure`` allocated: a repeat call."""
    state, template = base(run_limits)
    state, submit = _measured(state)
    job = core.ResourceId(root="job")
    state = first_fact(state, submit, job)
    state, allocated, call = agent_call(state, plan(), "repeat")
    assert allocated is None
    assert call.repeat
    assert call.request_id == submit.request_id
    assert call.deadline_at is not None
    state = suspend(state, template, (job,), call.deadline_at)
    return World(
        Source.AGENT_REPEAT,
        state,
        (Job(job, submit=submit),),
        call.deadline_at,
        decision=core.DecisionId(root="measure"),
    )


def registered(run_limits: core.Limits) -> World:
    """An agent's wait on the job of a registered operation."""
    state, template = base(run_limits)
    codec = measurement_codec(normalized=True)
    scope = core.Scope(owner=state.run.run_id, generation=state.run.generation)
    identity = expected_identity("evaluator")
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="registered"),
            scope=scope,
            request=RegisteredMeasurement(identity=identity),
        )
    )
    wire = decision.registered_wire
    assert wire is not None
    request_id = core.RequestId(root="operation:registered")
    request = core.ExecuteRegisteredOperation(
        request_id=request_id,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="operation:registered"),
        scope=scope,
        operation=wire,
        retry_limit=0,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
        request_ids=(request_id,),
        until=core.Until(at=LIMIT),
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                    "receipts": (*state.run.receipts, receipt),
                }
            ),
        }
    )
    state = core.step(
        state,
        core.RegisteredJobRequested(
            request=request, resource_pool=core.PoolId(root="jobs"), expected_measurement=identity
        ),
    ).state
    state = core.step(state, core.DispatchAuthorized(request_id=request_id)).state
    job = core.ResourceId(root="job")
    world = World(
        Source.REGISTERED,
        state,
        (Job(job, operation=request, sequence=0),),
        LIMIT,
        codec=codec,
    )
    world, events = fact(world, 0, 1.0, state="pending")
    for event in events:
        world = replace(world, state=core.step(world.state, event).state)
    state = suspend(world.state, template, (job,), LIMIT)
    return replace(world, state=state)


BUILDERS = {
    Source.AGENT: agent,
    Source.AGENT_REPEAT: agent_repeat,
    Source.MEASURE: measure,
    Source.REGISTERED: registered,
    Source.MULTI: multi,
}
