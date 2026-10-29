"""The trusted measurement of a candidate, and what its evidence says.

A workstream does not reserve its measurement up front: it submits the candidate to the root's
evaluation queue and waits until a slot is bound to it (`a_slot`), so its agents' slot goes to new
work meanwhile. Which waiting candidate a free slot measures is the queue's choice (`_queue`).
"""

from typing import Literal

from vibesys.candidates.api import (
    Baseline,
    InputFailure,
    again_after,
    decode_input,
    failure,
    input_measurement,
    with_first_attempt,
)
from vibesys.metrics import AcceptedReading, accept_readings
from vibesys.orchestration.dynamic.search._ids import INPUT_MEASURED
from vibesys.orchestration.dynamic.search._operations import EvidenceReadings
from vibesys.orchestration.dynamic.search._outcomes import (
    Evidence,
    Work,
    WorkEndedError,
    WorkstreamStart,
    ended_by,
)
from vibesys.orchestration.dynamic.search._queue import Bound, Dropped, Submission
from vibesys.orchestration.dynamic.search._reports import (
    REPORTS,
    VERDICTS,
    InputOverdue,
    Released,
)
from vibesys.orchestration.dynamic.search._values import (
    DropReason,
    Failure,
    FailureKind,
    Proof,
    WorkKind,
)
from vibesys.search_spans.api import GATE, GateAttrs
from vs_core.api.search import (
    Failed,
    Flow,
    MeasurementPurpose,
    MeasurementRecipe,
    MeasurementResult,
    RevisionRef,
    Source,
    Stopped,
    TimeUp,
    Value,
    Workspace,
    flow,
    trusted_keys,
)

PROFILE_STAGE = "profile"
"""The task's stage that captures a profile."""


class InputPending(Value):
    """The input's measurement is still to be taken (or in flight)."""

    kind: Literal["input-pending"] = "input-pending"
    first_attempt: InputFailure | None = None
    """Why the first measurement failed, when this is the second (`again_after`)."""


class InputTaken(Value):
    """The input's trusted measurement is in, to decode."""

    kind: Literal["input-taken"] = "input-taken"
    result: MeasurementResult
    first_attempt: InputFailure | None = None
    """Why the first measurement failed, when this is the second (`again_after`)."""


@flow(snapshot=InputPending | InputTaken, name="measure-input")
async def measure_the_input(ctx: Flow, at: InputPending | InputTaken) -> Baseline:
    """Measure the input once per run, or twice when the first failure shows no fault of it.

    A failure with no evidence against the input (`again_after`: no evidence, a wait core ended
    on its time, a stage the backend did not blame on the input or that printed nothing) is
    measured once more, and the second result stands with the first's failure kept
    (``first_attempt``). It checkpoints each measurement as soon as it is in, so a restart while
    its evidence is decoded decodes it again instead of measuring again; a restart while it is
    measured waits for the same measurement (core re-attaches the request), and a restart
    during the second measurement does not take a third.
    """
    ctx.checkpoint(at)
    while True:
        found = await _measured_once(ctx, at)
        why = failure(found)
        if at.first_attempt is not None or why is None or not again_after(why):
            return with_first_attempt(found, at.first_attempt)
        at = InputPending(first_attempt=why)
        ctx.checkpoint(at)


async def _measured_once(ctx: Flow, at: InputPending | InputTaken) -> Baseline:
    if isinstance(at, InputTaken):
        return await decode_input(ctx, at.result)
    measured = await input_measurement(ctx)
    if not isinstance(measured, MeasurementResult):
        return measured
    ctx.checkpoint(InputTaken(result=measured, first_attempt=at.first_attempt))
    return await decode_input(ctx, measured)


def input_so_far(ctx: Flow, w: WorkstreamStart) -> Baseline | None:
    """The input's measurement as known now, without waiting; None while it has not landed."""
    return w.baseline if w.baseline is not None else ctx.latch(INPUT_MEASURED).value


async def the_input(ctx: Flow, w: WorkstreamStart, work: Work) -> Baseline:
    """The input's measurement: known when the workstream started, else waited for here.

    A workstream may start, and its candidate may be reviewed, while the input is still being
    measured. Two things wait for it: the settlement, which compares the candidate with the
    input, and the candidate's trusted measurement. The measurement waits because the backend
    admits jobs in its own order and counts a measurement's deadline from its dispatch: a
    candidate dispatched while the input's job still waits for admission would spend its
    deadline in that queue and settle as failed. It waits once its slot is bound
    (`measurement_of`) until binding moves into the backend (#2662, steps 2 to 4). A wait
    longer than the run's ``input_wait_seconds``, counted from the workstream's first wait that
    needs the input (`while_input_pending`), is reported to the root once, which tells the
    orchestrator on its next turn; the workstream goes on waiting. An input that comes
    back unmeasurable does not stop the candidate: a candidate can fix what broke the input, and
    with no input value to beat it is eligible once it passes the task's gates.
    """
    if work.baseline is None:
        known = input_so_far(ctx, w)
        work.baseline = known if known is not None else await _wait_for_input(ctx, w, work)
    return work.baseline


async def _wait_for_input(ctx: Flow, w: WorkstreamStart, work: Work) -> Baseline:
    return await while_input_pending(ctx, w, work, ctx.latch(INPUT_MEASURED))


async def while_input_pending[T](ctx: Flow, w: WorkstreamStart, work: Work, source: Source[T]) -> T:
    """Wait for ``source``, reporting once if the input's measurement is overdue meanwhile.

    The wait for the input counts from the workstream's first wait that needs it: its submission
    for a slot, or its settlement. It is not counted from a bound slot, because with one slot the
    input's own measurement holds it, so no candidate is bound until the input lands, however
    long that takes. A wait longer than the run's ``input_wait_seconds`` while the input has not
    landed is reported to the root once (`InputOverdue`); the workstream goes on waiting.
    """
    limit = w.config.input_wait_seconds
    if limit is None or work.overdue_reported or input_so_far(ctx, w) is not None:
        return await source
    if work.input_wait_since is None:
        work.input_wait_since = ctx.clock.now()
    since = work.input_wait_since
    match await ctx.first(source, ctx.timer(since + limit)):
        case TimeUp():
            if input_so_far(ctx, w) is None:
                ctx.channel(REPORTS).send(InputOverdue(key=w.key, since=since, limit=limit))
            work.overdue_reported = True
            return await source
        case value:
            return value


async def a_slot(ctx: Flow, w: WorkstreamStart, work: Work, revision: RevisionRef) -> None:
    """Submit ``revision`` for its trusted measurement and wait until a slot is bound to it.

    The root answers with the queue's verdict: measure now, or never (superseded by a newer
    candidate of the same line, or the run stopped), which ends the work unmeasured. While the
    input has not landed, the wait counts toward its overdue report (`while_input_pending`).
    """
    ctx.channel(REPORTS).send(
        Submission(
            key=w.key,
            work_id=w.plan.work_id,
            kind=w.plan.kind,
            candidate=revision,
            parent=w.parent,
            at=ctx.clock.now(),
        )
    )
    match await while_input_pending(ctx, w, work, ctx.latch(VERDICTS.of(w.key))):
        case Bound():
            return
        case Dropped(reason=reason, by=by):
            raise WorkEndedError(
                Failure(kind=FailureKind.NOT_MEASURED, dropped=reason, superseded_by=by)
            )


async def measurement_of(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, work: Work, revision: RevisionRef
) -> MeasurementResult:
    """Measure ``revision`` with the trusted evaluations; what the evidence says is `evidence_of`.

    The measurement is dispatched only once a slot is bound to it and the input's measurement
    has landed (`the_input`): the workstream waits for both parked, so its agent slot may go to
    new work meanwhile. A stop while it waits ends it unmeasured (`FailureKind.NOT_MEASURED`),
    so its reviewed code is kept as work in progress, as a superseded candidate's is.

    The measurement is dispatched only once a slot is bound to it and the input's measurement
    has landed (`the_input`): the workstream waits for both parked, so its agent slot may go to
    new work meanwhile. A stop while it waits ends it unmeasured (`FailureKind.NOT_MEASURED`),
    so its reviewed code is kept as work in progress, as a superseded candidate's is.

    The backend measures again after an infrastructure failure, so a result that reaches here
    is the candidate's own; a measurement that ended without one is a failure of the workstream.
    One that ended with a result still holds its slot until `evidence_of` releases it, so the
    workstream can checkpoint the result first: a restart then resumes it with the result in
    hand, and it releases the slot then.
    """
    purpose = purpose_of(w)
    names = (PROFILE_STAGE,) if w.plan.kind is WorkKind.PROFILE else ctx.task.terms.measured
    recipe = MeasurementRecipe.of(ctx.facts)
    if w.plan.kind is WorkKind.PROFILE and w.plan.required_fields:
        recipe = recipe.requiring(PROFILE_STAGE, w.plan.required_fields)
    try:
        await a_slot(ctx, w, work, revision)
        await the_input(ctx, w, work)  # until a backend binds a slot at its job's start (#2662)
    except Stopped:
        # a reviewed candidate the stop kept from its measurement is kept, as a dropped one is
        stopped = Failure(kind=FailureKind.NOT_MEASURED, dropped=DropReason.STOPPED)
        raise WorkEndedError(stopped) from None
    plan = recipe.plan(revision, purpose=purpose, now_at=ctx.clock.now(), stages=names)
    found = await ctx.evaluation.measure(plan, workspace=ws, within=recipe.budget(names))
    gate = ctx.operation_span
    if gate is not None:
        stage = ",".join(names)
        work.gate = (gate, stage)
        ctx.label(GATE, span=gate, title=" + ".join(names), attrs=GateAttrs(stage=stage))
    ctx.channel(REPORTS).send(Released(key=w.key))
    if isinstance(found, Failed):
        ctx.channel(REPORTS).send(Released(key=w.key))
        raise ended_by(found, None)
    return found


async def evidence_of(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, revision: RevisionRef, found: MeasurementResult
) -> Evidence:
    """Release the slot ``found`` held and decode what its trusted evidence says of ``revision``.

    Releasing a slot is idempotent, so a workstream resumed with its measurement releases it again.
    """
    ctx.channel(REPORTS).send(Released(key=w.key))
    purpose = purpose_of(w)
    keys = trusted_keys(found, scope=found.scope, candidate=revision, purpose=purpose)
    if not keys:
        none = Failure(
            kind=FailureKind.NO_TRUSTED_EVIDENCE,
            status=found.status,
            output=found.diagnostic,
            operator_output=found.operator_diagnostic,
        )
        raise WorkEndedError(none)
    proof = Proof(refs=tuple(item for item in found.evidence if item.key in keys))
    return Evidence(readings=await interpret(ctx, ws, proof), proof=proof)


async def interpret(ctx: Flow, ws: Workspace, proof: Proof) -> tuple[AcceptedReading, ...]:
    """Have the evidence owner decode the trusted evidence into the readings a settlement needs."""
    seconds = ctx.task.terms.operation_seconds
    outcome = await ctx.evaluation.readings(proof.refs, workspace=ws, within=seconds)
    if isinstance(outcome, Failed):
        raise WorkEndedError(ended_by(outcome, None).failure, proof=proof)
    if not isinstance(outcome, EvidenceReadings):
        raise WorkEndedError(Failure(kind=FailureKind.EVIDENCE_UNDECODABLE), proof=proof)
    readings = accept_readings(outcome.readings, proof.refs)
    if isinstance(readings, str):
        raise WorkEndedError(Failure(kind=FailureKind.EVIDENCE_REFUSED), proof=proof)
    if outcome.status != "succeeded" or not readings:
        raise WorkEndedError(Failure(kind=FailureKind.EVIDENCE_UNDECODABLE), proof=proof)
    return readings


def purpose_of(w: WorkstreamStart) -> MeasurementPurpose:
    """What the measurement is for: a profile is looked at, anything else is scored."""
    return "profile" if w.plan.kind is WorkKind.PROFILE else "official"
