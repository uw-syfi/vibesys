"""Runs of a candidate's base, the samples they give, and the comparisons they make.

An agent's evaluation is compared with its base (`vs_core._comparison`). This module takes the
admission decisions that need core's state: asking for a base run when no fresh sample of it is
held or being taken, counting the strategy's own measurement of the input as samples of the
base, recording the sample a finished run gives with the comparison it changes, and earning a
close call one more base run. Every decision is a pure function of state and event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._comparison import (
    Measured,
    base_missing,
    comparisons_of,
    evaluation_of,
    moment_of,
    remeasures_base,
    sample_of,
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
from .types.kernel import AreaChange
from .types.measurement import BaseRef, BaseRun, Comparison, Sample
from .types.until import Within

if TYPE_CHECKING:
    from .types.common import Seconds
    from .types.evaluation import EvaluationState, EvidenceRef, MeasurementPlan
    from .types.kernel import EvaluationContext
    from .types.records import Outbound

__all__ = ["base_of", "base_plan", "record_samples", "with_base"]


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


def with_base(
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    event: AgentMeasurementRequested,
    base: BaseRef | None,
    candidate: RequestId,
) -> AreaChange[EvaluationState]:
    """``change`` plus a run of the base, when no fresh sample of it is held or being taken.

    The base is measured once per (measured input, evaluation); this is the only place it
    first is. Whether its samples are held is read from the ledger of samples, so another
    attempt's run of the same base counts, and nothing is measured twice.
    """
    task = context.run.facts.task
    compares = (
        task is not None
        and task.benchmark_stage is not None
        and any(stage.stage_id == task.benchmark_stage for stage in event.plan.stages)
    )
    if base is None or not compares:
        return change
    change = change.model_copy(update={"state": _anchored(change.state, base)})
    if base.measured_input == event.plan.measured_input:
        return change
    plan = base_plan(event.plan, base)
    identity = identity_of(plan)
    if not isinstance(identity, Proven):
        return change
    state = change.state
    measured = Measured(evaluation_of(identity.value), base.measured_input)
    if not base_missing(state, measured, moment_of(context.run)):
        return change
    return _base_run(change, context, event.scope, plan, candidate, capped(event))


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


def _base_run(  # noqa: PLR0913  # lint-waiver: LW-310002 [PLR0913]; the change, the scope, the plan, the candidate it serves and its limit are independent facts of one admission.
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    scope: Scope,
    plan: MeasurementPlan,
    candidate: RequestId,
    within: Within,
) -> AreaChange[EvaluationState]:
    """``change`` plus one more run of the base, recorded as core's own (``BaseRun``).

    An extra sample of an identity the scope already measured is admitted when its latest run
    ended cleanly and released, up to the identity's submission limit. A refusal adds nothing:
    a base that cannot be measured leaves the candidate without a comparison, never rejected.
    """
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
    added = allocate(state, context, Origin(scope, None, within), plan, budget)
    outbound = next((r for r in added.requests if isinstance(r.request, SubmitMeasurement)), None)
    if outbound is None or outbound.request.request_id is None:
        return change
    request_id = outbound.request.request_id
    grown = added.state.model_copy(
        update={
            "base_runs": (
                *added.state.base_runs,
                BaseRun(request_id=request_id, scope=scope, candidate=candidate),
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
    """Whether the identity may take one more sample: its last run ended cleanly and released."""
    if budget is None or not budget.receipts:
        return True
    latest = budget.receipts[-1]
    if (
        not isinstance(latest, PreparedSubmissionReceipt)
        or latest.observation is None
        or not latest.observation.terminal
        or latest.failure is not None
        or latest.observation.status != ObservationStatus.SUCCEEDED
        # A run that gave no sample (no benchmark reading) will not give one next time.
        or all(sample.run != latest.request_id for sample in state.samples)
    ):
        return False
    return isinstance(submission_released(state, context, latest), Proven) and len(
        budget.receipts
    ) < min(budget.limit, MAX_SUBMISSIONS_PER_MEASUREMENT)


def record_samples(
    state: EvaluationState,
    context: EvaluationContext,
    job: OwnedJob | RegisteredOwnedJob,
    newly: tuple[EvidenceRef, ...],
    observed_at: Seconds,
) -> tuple[EvaluationState, tuple[Outbound, ...]]:
    """Record the sample a finished framework run gives, and what it changes about comparisons.

    One run is one sample, however many records it carries, and a sample cannot be built
    without the commit and the measured input of its plan (`sample_of`). Every new or changed
    comparison is logged; a close call earns the base one more run (`remeasures_base`).
    """
    if not isinstance(job, OwnedJob):
        return state, ()
    sample = next(
        (
            made
            for evidence in newly
            if (made := sample_of(job, evidence, observed_at)) is not None
            and all(held.run != made.run for held in state.samples)
        ),
        None,
    )
    if sample is None:
        return state, ()
    state = state.model_copy(update={"samples": (*state.samples, _with_known_base(state, sample))})
    moment = moment_of(context.run)
    log = state.comparisons
    change = AreaChange(state=state)
    for comparison in comparisons_of(state, moment):
        last = next((c for c in reversed(log) if _same_subject(c, comparison)), None)
        if last == comparison:
            continue
        log = (*log, comparison)
        change = change.model_copy(
            update={"state": change.state.model_copy(update={"comparisons": log})}
        )
        change = _remeasure(change, context, comparison, sample.evaluation)
    return change.state, change.requests


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


def _remeasure(
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    comparison: Comparison,
    evaluation: str,
) -> AreaChange[EvaluationState]:
    """``change`` plus one more run of the base when ``comparison`` is a close call it would narrow."""
    state = change.state
    candidate = next(
        (
            job
            for job in reversed(state.jobs)
            if job.scope == comparison.scope
            and job.base is not None
            and job.plan.measured_input == comparison.candidate_input
        ),
        None,
    )
    if candidate is None or candidate.base is None:
        return change
    if not remeasures_base(state, comparison, evaluation, moment_of(context.run)):
        return change
    plan = base_plan(candidate.plan, candidate.base)
    return _base_run(
        change,
        context,
        comparison.scope,
        plan,
        candidate.submission_id,
        Within(limit=candidate.plan.time_bound),
    )
