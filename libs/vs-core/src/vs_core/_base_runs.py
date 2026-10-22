"""The runs core takes itself so a candidate is compared with its base, and the samples they give.

A candidate's evaluation is compared with its base (`vs_core._comparison`), whether an agent
asked for it through its tool or the strategy asks for it as the official measurement. This
module takes the admission decisions that need core's state: asking for a base run when no fresh
sample of it is held or being taken, counting the strategy's own measurement of the input as
samples of the base, recording the sample a finished run gives with the comparison it changes,
and taking the runs a waiting comparison still needs whenever a run of either side ends
(`after_run`).

That last decision is re-taken from the state at every end of a run, whatever the run gave: a
sample, a failure, a cancellation (its attempt closed, or it ran out of queue allowance) or a
rejection. A base run is shared by every candidate that waits on it, so a run that ends without
a sample leaves each of them waiting on nothing; the next end re-checks them and starts the
base run that is still missing, and a close call takes its next run as the previous one ends.
Nothing waits on an edge that may never come. Every decision is a pure function of state and
event.

What core's runs cost an attempt is bounded by constants, never by how many candidate inputs
its agent measures (`_side_open`, `_sample_ready`): per scope, at most one run of the base and
one of a candidate's input of core's in flight at once; at most ``MAX_CORE_BASE_RUNS`` runs of
the base over the attempt's lifetime, and ``MAX_SUBMISSIONS_PER_MEASUREMENT`` of each candidate
input, whoever asked for them; and at most ``MAX_SUBMISSIONS_PER_MEASUREMENT`` of core's runs
of a side in a row that end without a sample, after which that side stops until one gives a
sample. A run core ended out of queue budget is not replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._comparison import (
    MAX_CORE_BASE_RUNS,
    Measured,
    base_in_flight,
    base_missing,
    claimed_base,
    comparison_of,
    comparisons_of,
    evaluation_of,
    fresh,
    moment_of,
    opened_answer,
    remeasures_base,
    remeasures_candidate,
    restated_answers,
    run_under_way,
    running_baseline,
    sample_of,
    samples_of,
    scope_samples,
)
from ._proofs import (
    Proven,
)
from ._submission_receipts import receipt_ended
from ._submissions import Origin, allocate, current, identity_of, submission_released
from .types.common import (
    MAX_SUBMISSIONS_PER_MEASUREMENT,
    ObservationStatus,
    RequestId,
    Scope,
)
from .types.evaluation import (
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
from .types.until import FailureReason, Within

if TYPE_CHECKING:
    from .types.common import InvocationRef, RequestId, Seconds
    from .types.evaluation import (
        EvaluationState,
        EvidenceRef,
        MeasurementIdentity,
        MeasurementPlan,
    )
    from .types.kernel import EvaluationContext

__all__ = ["after_run", "base_of", "base_plan", "record_samples", "with_base"]


@dataclass(frozen=True, slots=True)
class _Behalf:
    """The candidate a run of core's serves: its run, and the measured input it compares."""

    run: RequestId
    measured_input: str


def base_of(context: EvaluationContext, scope: Scope, plan: MeasurementPlan) -> BaseRef | None:
    """The attempt's own base revision with its measured input, when the candidate is comparable.

    Core knows the base (the workspace the attempt was opened on); the digest of what the
    evaluation reads there is the runtime's, given when it ensured that workspace
    (``AttemptView.base_input``). A candidate without a measured input has no sample, so there
    is nothing to compare.
    """
    owner = next(
        (
            row
            for row in context.attempts.attempts
            if row.attempt_id == scope.owner and row.generation == scope.generation
        ),
        None,
    )
    if owner is None or owner.base_input is None or plan.measured_input is None:
        return None
    return BaseRef(commit=owner.workspace.base, measured_input=owner.base_input)


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
    origin: Origin,
    plan: MeasurementPlan,
    candidate: RequestId,
) -> AreaChange[EvaluationState]:
    """``change`` plus a run of the base, when no fresh sample of it is held or being taken.

    The base is measured once per (measured input, evaluation), first here. Whether its samples
    are held is read from the ledger of samples, so another attempt's run of the same base
    counts, and nothing is measured twice while a run of it is under way; one that ends without
    a sample is replaced (`after_run`). ``origin`` names the candidate's scope and the base it is
    compared with, ``plan`` its run (an agent's, or the strategy's own final check) and
    ``candidate`` that run's request. The base run is bounded as the agent's call is, or by the
    plan's own stages for a Measure decision.
    """
    base, own = origin.base, plan.measured_input
    if base is None or own is None or not _compares(context, plan):
        return change
    change = change.model_copy(update={"state": _anchored(change.state, base)})
    if base.measured_input == own:
        return change
    base_run = base_plan(plan, base)
    identity = identity_of(base_run)
    if not isinstance(identity, Proven):
        return change
    measured = Measured(evaluation_of(identity.value), base.measured_input)
    if not base_missing(change.state, measured, moment_of(context.run)):
        return change
    within = origin.within or Within(limit=plan.time_bound)
    behalf = _Behalf(candidate, own)
    return _measure_base(
        change,
        context,
        origin.scope,
        base_run,
        base,
        behalf,
        within,
        requested_by=origin.requested_by,
    )


def _measure_base(  # noqa: PLR0913  # lint-waiver: LW-310002 [PLR0913]; the change, the scope, the plan, the base, the candidate it serves, its limit and the turn that asked are independent facts of one admission.
    change: AreaChange[EvaluationState],
    context: EvaluationContext,
    scope: Scope,
    plan: MeasurementPlan,
    base: BaseRef,
    behalf: _Behalf,
    within: Within,
    *,
    requested_by: InvocationRef | None = None,
) -> AreaChange[EvaluationState]:
    """``change`` plus one more sample of ``base`` on the way: the strategy's run, else core's.

    A run of the base the strategy has under way is claimed instead of starting a second: its
    sample counts when it ends, and if it ends without one the claim's candidates are
    re-checked like any other (`after_run`).
    """
    awaited = running_baseline(change.state, base)
    if awaited is not None:
        return change.model_copy(update={"state": _claimed(change.state, awaited, base)})
    return _core_run(
        change, context, Origin(scope, None, within, requested_by=requested_by), plan, behalf
    )


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
    behalf: _Behalf,
) -> AreaChange[EvaluationState]:
    """``change`` plus one more run of ``plan`` in ``origin``'s scope, recorded as core's own.

    Admitted only while core may run that side in the scope at all (`_side_open`) and the
    identity may take one more run for ``behalf``'s comparison (`_sample_ready`). The first run
    of the base opens its budget at ``MAX_CORE_BASE_RUNS``. A refusal adds nothing: a side that
    cannot be measured leaves the candidate without a comparison, never rejected, and a need
    deferred while the side's run is in flight is taken when that run ends (`after_run`).
    """
    scope = origin.scope
    identity = identity_of(plan)
    if not isinstance(identity, Proven) or not isinstance(current(context, scope), Proven):
        return change
    state = change.state
    if requirement_unmet(state, identity.value):
        return change
    if not _side_open(state, context, scope, baseline=plan.purpose == "baseline"):
        return change
    matches = tuple(
        b for b in state.submission_budgets if b.scope == scope and b.identity == identity.value
    )
    budget = matches[0] if matches else None
    if len(matches) > 1 or not _sample_ready(state, context, budget, behalf.measured_input):
        return change
    if budget is None and plan.purpose == "baseline":
        budget = SubmissionBudget(scope=scope, identity=identity.value, limit=MAX_CORE_BASE_RUNS)
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
                    candidate=behalf.run,
                    candidate_input=behalf.measured_input,
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
    state: EvaluationState,
    context: EvaluationContext,
    budget: SubmissionBudget | None,
    candidate_input: str,
) -> bool:
    """Whether the identity may take one more run: its last ended, released, and may give a sample.

    A run that gave a sample may be followed by another, and so may one that ended without a
    sample for a reason that says nothing of the next run: it was cancelled with its attempt,
    failed in the machinery, was refused at submission or ended by core before it named a job,
    or succeeded and its records could not be collected (those are bounded per side,
    `_side_open`). Three ends refuse another, since the
    next run would end the same way: a run that succeeded with records but without a reading
    (the benchmark reports no metric), a workload failure (the revision fails the
    evaluation, which is durable, as it is for any submission), and a run environment that
    can run nothing (``RUN_ENVIRONMENT_UNAVAILABLE``: a local Slurm root failed its probe).

    Two limits hold. Every run of the identity in the scope, whoever asked for it and whatever
    it gave, counts toward the budget's own limit over the attempt's lifetime
    (``SubmissionBudget`` holds no more receipts than that): ``MAX_CORE_BASE_RUNS`` for the
    base, whose budget core opens, and at most ``MAX_SUBMISSIONS_PER_MEASUREMENT`` for a
    candidate input, whose budget its agent's call opened. And of the runs that gave a sample,
    a comparison is charged only its own and those nobody asked for it alone (`_charged`), at
    most ``MAX_SUBMISSIONS_PER_MEASUREMENT``: a base is shared by every candidate of its
    attempt, and once its samples go stale a later candidate takes its own runs of it.
    """
    if budget is None or not budget.receipts:
        return True
    latest = budget.receipts[-1]
    if (
        not isinstance(latest, PreparedSubmissionReceipt)
        or not receipt_ended(latest)
        or latest.failure
        in (MeasurementFailure.WORKLOAD, MeasurementFailure.RUN_ENVIRONMENT_UNAVAILABLE)
    ):
        return False
    observation = latest.observation
    sampled = any(sample.run == latest.request_id for sample in state.samples)
    recorded = any(job.submission_id == latest.request_id and job.evidence for job in state.jobs)
    if (
        observation is not None
        and observation.status == ObservationStatus.SUCCEEDED
        and recorded
        and not sampled
    ):
        return False
    # A submission core ended before any observation named a job ran nothing core knows of,
    # like one refused at submission.
    released = (
        observation is None
        or not observation.accepted
        or isinstance(submission_released(state, context, latest), Proven)
    )
    return (
        released
        and len(budget.receipts) < budget.limit
        and _charged(state, budget, candidate_input) < MAX_SUBMISSIONS_PER_MEASUREMENT
    )


def _charged(state: EvaluationState, budget: SubmissionBudget, candidate_input: str) -> int:
    """How many of ``budget``'s runs that gave a sample count toward ``candidate_input``'s comparison.

    Every one but core's own for another candidate input: a run an agent or the strategy asked
    for is charged to every comparison, as it was asked for no particular one. A run that gave
    no sample is bounded by `_side_open` instead, over the attempt's lifetime.
    """
    others = {run.request_id for run in state.core_runs if run.candidate_input != candidate_input}
    sampled = {sample.run for sample in state.samples}
    return sum(
        1
        for receipt in budget.receipts
        if isinstance(receipt, PreparedSubmissionReceipt)
        and receipt.request_id in sampled
        and receipt.request_id not in others
    )


def requirement_unmet(state: EvaluationState, identity: MeasurementIdentity) -> bool:
    """Whether a run of ``identity``'s revision under its evaluation ended with an unmet requirement.

    The evaluator's own statement, kept on the run's evidence (``EvidenceRef.unmet_requirement``),
    that the revision lacks a capability the task requires. That is a fact of the revision and the
    evaluation, not of a scope or of why the run was asked for, so every scope's runs are read,
    the strategy's own measurement of the input among them: another run would end the same way.
    A run names its revision and evaluation the way ``identity`` does, whatever it measured it
    for (``purpose``) and whatever input its plan names (the strategy's measurement of the input
    names none), so those are not compared.
    """
    evaluation = evaluation_of(identity)
    for job in state.jobs:
        if not any(ref.unmet_requirement is not None for ref in job.evidence):
            continue
        ran = identity_of(job.plan)
        if (
            isinstance(ran, Proven)
            and ran.value.candidate == identity.candidate
            and evaluation_of(ran.value) == evaluation
        ):
            return True
    return False


def _side_open(
    state: EvaluationState, context: EvaluationContext, scope: Scope, *, baseline: bool
) -> bool:
    """Whether core may start a run of one side (the base, else a candidate's input) in ``scope``.

    Read over core's own runs of that side in the scope, newest first, back to the latest that
    gave a sample:

    - none of them is still under way: core's runs never hold an agent's turn, so nothing else
      paces them, and one run a side in flight per scope bounds them however many candidate
      inputs the agent measures. A run whose records are still being collected is under way;
    - none of them was ended by core because its job waited in the queue for its whole queue
      budget (``FailureReason.QUEUE_BUDGET_SPENT``): the queue that held it holds the next, so
      it is not replaced at all;
    - fewer than ``MAX_SUBMISSIONS_PER_MEASUREMENT`` of them ended without a sample. A side
      whose runs keep dying (a machine that fails, a benchmark whose records never come) stops
      there until one of core's runs of it gives a sample, however many candidate inputs share
      it. The agent's own runs do not reopen it: they are of inputs the agent chose.
    """
    side = {
        receipt.request_id: receipt
        for budget in state.submission_budgets
        if budget.scope == scope and (budget.identity.purpose == "baseline") == baseline
        for receipt in budget.receipts
        if isinstance(receipt, PreparedSubmissionReceipt)
    }
    own = tuple(run.request_id for run in state.core_runs if run.request_id in side)
    if any(run_under_way(state, side[run]) for run in own):
        return False
    sampled = {sample.run for sample in state.samples}
    unsampled = 0
    for run in reversed(own):
        if _out_of_queue(context, run):
            return False
        if run in sampled:
            return True
        unsampled += 1
    return unsampled < MAX_SUBMISSIONS_PER_MEASUREMENT


def _out_of_queue(context: EvaluationContext, run: RequestId) -> bool:
    """Whether core ended ``run`` because its job waited in the queue for its whole queue budget."""
    row = context.records.record(run)
    return (
        row is not None
        and row.failure is not None
        and row.failure.reason is FailureReason.QUEUE_BUDGET_SPENT
    )


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
) -> AreaChange[EvaluationState]:
    """Record the sample a finished framework run gives, and what its end changes.

    One run is one sample, however many records it carries, and a sample cannot be built
    without the commit and the measured input of its plan (`sample_of`). The official answer
    of its input in its scope is restated to the mean it now makes (`restated_answers`), and
    every new or changed comparison is logged. Once the run has ended, with a sample or without, the comparisons
    waiting on its input take the runs they still need (`after_run`).
    """
    if not isinstance(job, OwnedJob):
        return AreaChange(state=state)
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
        sample = _with_known_base(state, sample)
        state = restated_answers(
            state.model_copy(update={"samples": (*state.samples, sample)}),
            sample,
            context.run.now_at,
        )
        state = state.model_copy(update={"comparisons": _logged(state, context, sample)})
    if not job.terminal:
        return AreaChange(state=state)
    state = opened_answer(state, job, context.run.now_at)
    return after_run(AreaChange(state=state), context, job.submission_id)


def _logged(
    state: EvaluationState, context: EvaluationContext, sample: Sample
) -> tuple[Comparison, ...]:
    """The comparison log with every comparison ``sample`` changes that it does not hold.

    A sample feeds the comparisons of its own input in its own scope (it is their candidate
    side) and every comparison against its input (it is their base side). Another comparison
    is not recomputed here: its base samples may have gone stale on the run clock since it was
    logged, and logging that again would present an old pair, whose verdict the clock changed
    and no run did, as news beside an unrelated sample.
    """
    log = state.comparisons
    for comparison in comparisons_of(state, moment_of(context.run)):
        if not _fed_by(sample, comparison):
            continue
        last = next((c for c in reversed(log) if _same_subject(c, comparison)), None)
        if last != comparison:
            log = (*log, comparison)
    return log


def _fed_by(sample: Sample, comparison: Comparison) -> bool:
    """Whether ``sample`` is a run of the comparison's candidate input or of its base input."""
    candidate_side = (
        sample.scope == comparison.scope and sample.measured_input == comparison.candidate_input
    )
    return candidate_side or sample.measured_input == comparison.base_input


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
    it would take its input for unmeasured. So is a job whose evidence core is still collecting
    (``OwnedJob.collecting``): the collection's end decides, with its sample or without.
    """
    state = change.state
    if any(
        job.submission_id == run and (not job.terminal or job.collecting is not None)
        for job in state.jobs
    ):
        return change
    measured_input = _measured_input(state, context, run)
    own = next((core_run.scope for core_run in state.core_runs if core_run.request_id == run), None)
    for candidate in _candidates_of(state, context, measured_input, own):
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
    state: EvaluationState,
    context: EvaluationContext,
    measured_input: str | None,
    scope: Scope | None,
) -> tuple[_Candidate, ...]:
    """The latest run of each (scope, candidate input, base) of ``measured_input`` or ``scope``.

    A run is of ``measured_input`` when it is the candidate's input or its base's. Read from the submissions, so a candidate whose run is admitted but not yet started counts:
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
        if own is None or (
            measured_input not in (own, candidate.base.measured_input) and candidate.scope != scope
        ):
            continue
        key = (candidate.scope.model_dump_json(), own, candidate.base.model_dump_json())
        latest[key] = candidate
    return tuple(latest.values())


def _submission_requester(
    context: EvaluationContext, request_id: RequestId
) -> InvocationRef | None:
    """The turn recorded on ``request_id``'s submission, when it has one."""
    row = context.records.record(request_id)
    request = None if row is None else row.request
    if isinstance(request, SubmitMeasurement):
        return request.requested_by
    return None


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
    requested_by = _submission_requester(context, candidate.request_id)
    plan = base_plan(candidate.plan, base)
    mine = Measured(evaluation, own)
    behalf = _Behalf(candidate.request_id, own)
    comparison = comparison_of(state, scope, mine, moment)
    if comparison is None:
        sampled = bool(scope_samples(state, scope, mine))
        if (sampled or base_in_flight(state, mine, scope)) and base_missing(
            state, Measured(evaluation, base.measured_input), moment
        ):
            return _measure_base(
                change, context, scope, plan, base, behalf, within, requested_by=requested_by
            )
        return change
    if remeasures_base(state, comparison, evaluation, moment):
        change = _measure_base(
            change, context, scope, plan, base, behalf, within, requested_by=requested_by
        )
    if remeasures_candidate(change.state, comparison, evaluation, moment):
        again = Origin(scope, None, within, base=base, requested_by=requested_by)
        change = _core_run(change, context, again, candidate.plan, behalf)
    return change
