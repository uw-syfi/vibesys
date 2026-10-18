"""The runs core takes itself so a candidate is compared with its base, and the samples they give.

An agent's evaluation is compared with its base (`vs_core._comparison`). This module takes the
admission decisions that need core's state: asking for a base run when no fresh sample of it is
held or being taken, counting the strategy's own measurement of the input as samples of the
base, recording the sample a finished run gives with the comparison it changes, and taking the
runs a waiting comparison still needs whenever a run of either side ends (`after_run`).

That last decision is re-taken from the state at every end of a run, whatever the run gave: a
sample, a failure, a cancellation (its attempt closed, or it ran out of queue allowance) or a
rejection. A base run is shared by every candidate that waits on it, so a run that ends without
a sample leaves each of them waiting on nothing; the next end re-checks them and starts the
base run that is still missing, and a close call takes its next run as the previous one ends.
Nothing waits on an edge that may never come. Every decision is a pure function of state and
event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._comparison import (
    Measured,
    base_in_flight,
    base_missing,
    claimed_base,
    comparison_of,
    comparisons_of,
    evaluation_of,
    fresh,
    moment_of,
    remeasures_base,
    remeasures_candidate,
    running_baseline,
    sample_of,
    samples_of,
)
from ._proofs import (
    Proven,
)
from ._submissions import Origin, allocate, capped, current, identity_of, submission_released
from .types.common import (
    MAX_SUBMISSIONS_PER_MEASUREMENT,
    ObservationStatus,
    RequestId,
    Scope,
)
from .types.evaluation import (
    AgentMeasurementRequested,
    MeasurementResult,
    OwnedJob,
    PreparedSubmissionReceipt,
    RegisteredOwnedJob,
    SubmissionBudget,
    SubmitMeasurement,
)
from .types.job_observations import MeasurementFailure
from .types.kernel import AreaChange
from .types.measurement import BaseClaim, BaseRef, Comparison, CoreRun, Sample
from .types.until import Within

if TYPE_CHECKING:
    from .types.common import RequestId, Seconds
    from .types.evaluation import (
        EvaluationState,
        EvidenceRef,
        MeasurementIdentity,
        MeasurementPlan,
    )
    from .types.kernel import EvaluationContext
    from .types.records import Outbound

__all__ = ["after_run", "base_of", "base_plan", "record_samples", "with_base"]


def base_of(context: EvaluationContext, event: AgentMeasurementRequested) -> BaseRef | None:
    """The attempt's own base revision with its measured input, when the candidate is comparable.

    Core knows the base (the workspace the attempt was opened on); only the digest of what the
    evaluation reads there is the runtime's to give. A candidate without a measured input has
    no sample, so there is nothing to compare.
    """
    plan = event.plan
    owner = next(
        (
            row
            for row in context.attempts.attempts
            if row.attempt_id == event.scope.owner and row.generation == event.scope.generation
        ),
        None,
    )
    if owner is None or event.base_input is None or plan.measured_input is None:
        return None
    return BaseRef(commit=owner.workspace.base, measured_input=event.base_input)


def base_plan(plan: MeasurementPlan, base: BaseRef) -> MeasurementPlan:
    """The plan that measures ``base`` as ``plan`` measures its candidate: same gates, same host."""
    return plan.model_copy(
        update={
            "purpose": "baseline",
            "candidate": base.commit,
            "measured_input": base.measured_input,
        }
    )


def _compares(context: EvaluationContext, plan: MeasurementPlan) -> bool:
    """Whether ``plan`` runs the task's benchmark stage, the one a comparison reads."""
    task = context.run.facts.task
    return (
        task is not None
        and task.benchmark_stage is not None
        and any(stage.stage_id == task.benchmark_stage for stage in plan.stages)
    )


def with_base(
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    event: AgentMeasurementRequested,
    base: BaseRef | None,
    candidate: RequestId,
) -> AreaChange[EvaluationState]:
    """``change`` plus a run of the base, when no fresh sample of it is held or being taken.

    The base is measured once per (measured input, evaluation), first here. Whether its samples
    are held is read from the ledger of samples, so another attempt's run of the same base
    counts, and nothing is measured twice while a run of it is under way; one that ends without
    a sample is replaced (`after_run`).
    """
    if base is None or not _compares(context, event.plan):
        return change
    change = change.model_copy(update={"state": _anchored(change.state, base)})
    if base.measured_input == event.plan.measured_input:
        return change
    plan = base_plan(event.plan, base)
    identity = identity_of(plan)
    if not isinstance(identity, Proven):
        return change
    measured = Measured(evaluation_of(identity.value), base.measured_input)
    if not base_missing(change.state, measured, moment_of(context.run)):
        return change
    return _measure_base(change, context, event.scope, plan, base, candidate, capped(event))


def _measure_base(  # noqa: PLR0913  # lint-waiver: LW-310002 [PLR0913]; the change, the scope, the plan, the base, the candidate it serves and its limit are independent facts of one admission.
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    scope: Scope,
    plan: MeasurementPlan,
    base: BaseRef,
    candidate: RequestId,
    within: Within,
) -> AreaChange[EvaluationState]:
    """``change`` plus one more sample of ``base`` on the way: the strategy's run, else core's.

    A run of the base the strategy has under way is claimed instead of starting a second: its
    sample counts when it ends, and if it ends without one the claim's candidates are
    re-checked like any other (`after_run`).
    """
    awaited = running_baseline(change.state, base)
    if awaited is not None:
        return change.model_copy(update={"state": _claimed(change.state, awaited, base)})
    return _core_run(change, context, Origin(scope, None, within), plan, candidate)


def _claimed(state: EvaluationState, running: RequestId, base: BaseRef) -> EvaluationState:
    """``state`` counting the strategy's run in flight as samples of ``base`` when it ends."""
    if any(claim.request_id == running for claim in state.base_claims):
        return state
    claims = (*state.base_claims, BaseClaim(request_id=running, base=base))
    return state.model_copy(update={"base_claims": claims})


def _anchored(state: EvaluationState, base: BaseRef) -> EvaluationState:
    """``state`` with the input's own measurement of ``base`` counted as samples of it.

    The strategy measures the run's input before any agent works, from a plan that names no
    measured input (nothing was open to digest it from). The runtime has now digested that
    same commit, so what the evaluation read there is known: those runs are samples of this
    base, and it is not measured a second time.
    """
    held = {sample.run for sample in state.samples}
    made = tuple(
        sample
        for job in state.jobs
        if job.plan.purpose == "baseline"
        and job.plan.candidate == base.commit
        and job.plan.measured_input is None
        and job.submission_id not in held
        and job.observation is not None
        for evidence in job.evidence
        if (sample := sample_of(job, evidence, job.observation.observed_at, base.measured_input))
        is not None
    )
    one_each = {sample.run: sample for sample in reversed(made)}
    if not one_each:
        return state
    return state.model_copy(update={"samples": (*state.samples, *reversed(one_each.values()))})


def _core_run(
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    origin: Origin,
    plan: MeasurementPlan,
    candidate: RequestId,
) -> AreaChange[EvaluationState]:
    """``change`` plus one more run of ``plan`` in ``origin``'s scope, recorded as core's own.

    An extra sample of an identity the scope already measured is admitted when its latest run
    ended and released and may give one (`_sample_ready`), up to the identity's submission
    limit. A refusal adds nothing: a side that cannot be measured leaves the candidate without
    a comparison, never rejected.
    """
    scope = origin.scope
    identity = identity_of(plan)
    if not isinstance(identity, Proven) or not isinstance(current(context, scope), Proven):
        return change
    state = change.state
    matches = tuple(
        b for b in state.submission_budgets if b.scope == scope and b.identity == identity.value
    )
    budget = matches[0] if matches else None
    if len(matches) > 1 or not _sample_ready(state, context, budget):
        return change
    added = allocate(state, context, origin, plan, budget)
    outbound = next((r for r in added.requests if isinstance(r.request, SubmitMeasurement)), None)
    if outbound is None or outbound.request.request_id is None:
        return change
    request_id = outbound.request.request_id
    grown = added.state.model_copy(
        update={
            "core_runs": (
                *added.state.core_runs,
                CoreRun(
                    request_id=request_id,
                    scope=scope,
                    candidate=candidate,
                    fresh_when_asked=_fresh_count(state, context, plan, identity.value),
                ),
            )
        }
    )
    return AreaChange(
        state=grown,
        requests=(*change.requests, *added.requests),
        events=(*change.events, *(e for e in added.events if not isinstance(e, MeasurementResult))),
        signals=(*change.signals, *added.signals),
    )


def _sample_ready(
    state: EvaluationState, context: EvaluationContext, budget: SubmissionBudget | None
) -> bool:
    """Whether the identity may take one more run: its last ended, released, and may give a sample.

    A run that gave a sample may be followed by another, and so may one that ended without a
    sample for a reason that says nothing of the next run: it was cancelled (drained with its
    attempt, or out of queue allowance), failed in the machinery, or was refused at submission.
    Two ends refuse another, since the next run would end the same way: a run that succeeded
    without a reading (the benchmark reports no metric), and a workload failure (the revision
    fails the evaluation, which is durable, as it is for any submission). Every run counts
    toward the identity's submission limit, so a run that keeps dying is retried a bounded
    number of times.
    """
    if budget is None or not budget.receipts:
        return True
    latest = budget.receipts[-1]
    if (
        not isinstance(latest, PreparedSubmissionReceipt)
        or latest.observation is None
        or not latest.observation.terminal
        or latest.failure is MeasurementFailure.WORKLOAD
    ):
        return False
    observation = latest.observation
    sampled = any(sample.run == latest.request_id for sample in state.samples)
    if observation.status == ObservationStatus.SUCCEEDED and not sampled:
        return False
    released = not observation.accepted or isinstance(
        submission_released(state, context, latest), Proven
    )
    return released and len(budget.receipts) < min(budget.limit, MAX_SUBMISSIONS_PER_MEASUREMENT)


def _fresh_count(
    state: EvaluationState,
    context: EvaluationContext,
    plan: MeasurementPlan,
    identity: MeasurementIdentity,
) -> int:
    """How many fresh samples of the input ``plan`` measures are held now, in any scope."""
    if plan.measured_input is None:
        return 0
    measured = Measured(evaluation_of(identity), plan.measured_input)
    return len(fresh(samples_of(state, measured), moment_of(context.run)))


def record_samples(
    state: EvaluationState,
    context: EvaluationContext,
    job: OwnedJob | RegisteredOwnedJob,
    newly: tuple[EvidenceRef, ...],
    observed_at: Seconds,
) -> tuple[EvaluationState, tuple[Outbound, ...]]:
    """Record the sample a finished framework run gives, and what its end changes.

    One run is one sample, however many records it carries, and a sample cannot be built
    without the commit and the measured input of its plan (`sample_of`). Every new or changed
    comparison is logged. Once the run has ended, with a sample or without, the comparisons
    waiting on its input take the runs they still need (`after_run`).
    """
    if not isinstance(job, OwnedJob):
        return state, ()
    sample = next(
        (
            made
            for evidence in newly
            if (made := sample_of(job, evidence, observed_at, _input_of(state, job))) is not None
            and all(held.run != made.run for held in state.samples)
        ),
        None,
    )
    if sample is not None:
        state = state.model_copy(
            update={"samples": (*state.samples, _with_known_base(state, sample))}
        )
        state = state.model_copy(update={"comparisons": _logged(state, context)})
    if not job.terminal:
        return state, ()
    change = after_run(AreaChange(state=state), context, job.submission_id)
    return change.state, change.requests


def _logged(state: EvaluationState, context: EvaluationContext) -> tuple[Comparison, ...]:
    """The comparison log with every comparison the samples now make that it does not hold."""
    log = state.comparisons
    for comparison in comparisons_of(state, moment_of(context.run)):
        last = next((c for c in reversed(log) if _same_subject(c, comparison)), None)
        if last != comparison:
            log = (*log, comparison)
    return log


def _input_of(state: EvaluationState, job: OwnedJob) -> str | None:
    """The measured input of a claimed run of the strategy's, whose plan names none."""
    claimed = claimed_base(state, job.submission_id)
    return None if claimed is None else claimed.measured_input


def _with_known_base(state: EvaluationState, sample: Sample) -> Sample:
    """``sample`` naming the base its input is compared with, when an earlier run of it did.

    The strategy's final check of a candidate carries no base (its decision names none), yet it
    is another run of the input the agent measured against its base: the record says so.
    """
    if sample.base is not None:
        return sample
    known = next(
        (
            held.base
            for held in reversed(state.samples)
            if held.base is not None
            and (held.scope, held.evaluation, held.measured_input)
            == (sample.scope, sample.evaluation, sample.measured_input)
        ),
        None,
    )
    return sample if known is None else sample.model_copy(update={"base": known})


def _same_subject(held: Comparison, other: Comparison) -> bool:
    """Whether two comparisons are of one candidate input in one scope against one base input."""
    return (held.scope, held.candidate_input, held.base_input) == (
        other.scope,
        other.candidate_input,
        other.base_input,
    )


def after_run(
    change: AreaChange[EvaluationState], context: EvaluationContext, run: RequestId
) -> AreaChange[EvaluationState]:
    """``change`` plus the runs the end of ``run`` leaves a waiting comparison needing.

    ``run`` measured one input (its plan's, or for a claimed run of the strategy's the base it
    was claimed for). Every candidate compared with that input, or whose own input it is, is
    re-checked from the state (`_advance`), whether ``run`` gave a sample or not; each check is
    idempotent, so the job's end and its submission's end may both ask. A submission whose job
    has not been seen to end yet is left to that end, which carries its sample: deciding before
    it would take its input for unmeasured.
    """
    state = change.state
    if any(job.submission_id == run and not job.terminal for job in state.jobs):
        return change
    measured_input = _measured_input(state, context, run)
    if measured_input is None:
        return change
    for candidate in _candidates_of(state, context, measured_input):
        change = _advance(change, context, candidate)
    return change


def _measured_input(
    state: EvaluationState, context: EvaluationContext, run: RequestId
) -> str | None:
    """The input ``run`` measured: its plan's, else the base a claim counts it toward."""
    job = next((j for j in state.jobs if j.submission_id == run), None)
    row = context.records.record(run)
    plan = (
        job.plan
        if job is not None
        else row.request.plan
        if row is not None and isinstance(row.request, SubmitMeasurement)
        else None
    )
    if plan is None:
        return None
    claimed = claimed_base(state, run)
    return plan.measured_input or (None if claimed is None else claimed.measured_input)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A run of a candidate that names its base: an agent's, or core's own run of it again."""

    request_id: RequestId
    scope: Scope
    plan: MeasurementPlan
    base: BaseRef


def _candidates_of(
    state: EvaluationState, context: EvaluationContext, measured_input: str
) -> tuple[_Candidate, ...]:
    """The latest run of each (scope, candidate input, base) that involves ``measured_input``.

    Read from the submissions, so a candidate whose run is admitted but not yet started counts:
    it waits on its base as much as one that runs.
    """
    made = (
        *(
            _Candidate(row.request_id, row.request.scope, row.request.plan, row.request.base)
            for row in context.records.records
            if isinstance(row.request, SubmitMeasurement) and row.request.base is not None
        ),
        *(
            _Candidate(job.submission_id, job.scope, job.plan, job.base)
            for job in state.jobs
            if job.base is not None
        ),
    )
    latest: dict[tuple[str, str, str], _Candidate] = {}
    for candidate in made:
        own = candidate.plan.measured_input
        if own is None or measured_input not in (own, candidate.base.measured_input):
            continue
        key = (candidate.scope.model_dump_json(), own, candidate.base.model_dump_json())
        latest[key] = candidate
    return tuple(latest.values())


def _advance(
    change: AreaChange[EvaluationState], context: EvaluationContext, candidate: _Candidate
) -> AreaChange[EvaluationState]:
    """``change`` plus the next run ``candidate``'s comparison needs, if any.

    - No comparison yet, while the candidate has a sample or a run under way: its base has no
      fresh sample, and when none is being taken either (the run it waited on ended without
      one, in any scope) the base is measured again.
    - A close call: one more run of the base (`remeasures_base`) and, under no declared floor,
      of the candidate's input (`remeasures_candidate`), each while its side is short of the
      bound and has no run under way.

    Only for a candidate whose scope may still start work (`_core_run`).
    """
    base = candidate.base
    own = candidate.plan.measured_input
    identity = identity_of(candidate.plan)
    if (
        own is None
        or own == base.measured_input
        or not _compares(context, candidate.plan)
        or not isinstance(identity, Proven)
        or not isinstance(current(context, candidate.scope), Proven)
    ):
        return change
    state = change.state
    evaluation = evaluation_of(identity.value)
    moment = moment_of(context.run)
    scope = candidate.scope
    within = Within(limit=candidate.plan.time_bound)
    plan = base_plan(candidate.plan, base)
    mine = Measured(evaluation, own)
    comparison = comparison_of(state, scope, mine, moment)
    if comparison is None:
        sampled = any(s.scope == scope for s in samples_of(state, mine))
        if (sampled or base_in_flight(state, mine, scope)) and base_missing(
            state, Measured(evaluation, base.measured_input), moment
        ):
            return _measure_base(change, context, scope, plan, base, candidate.request_id, within)
        return change
    if remeasures_base(state, comparison, evaluation, moment):
        change = _measure_base(change, context, scope, plan, base, candidate.request_id, within)
    if remeasures_candidate(change.state, comparison, evaluation, moment):
        again = Origin(scope, None, within, base=base)
        change = _core_run(change, context, again, candidate.plan, candidate.request_id)
    return change
