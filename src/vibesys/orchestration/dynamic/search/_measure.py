"""The trusted measurement of a candidate, and what its evidence says."""

from vibesys.candidates.api import Baseline
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
from vibesys.orchestration.dynamic.search._values import (
    Failure,
    FailureKind,
    Proof,
    WorkKind,
)
from vs_core.api.search import (
    Failed,
    Flow,
    MeasurementPurpose,
    MeasurementRecipe,
    RevisionRef,
    Workspace,
    trusted_keys,
)

PROFILE_STAGE = "profile"
"""The task's stage that captures a profile."""


def input_so_far(ctx: Flow, w: WorkstreamStart) -> Baseline | None:
    """The input's measurement as known now, without waiting; None while it has not landed."""
    return w.baseline if w.baseline is not None else ctx.latch(INPUT_MEASURED).value


async def the_input(ctx: Flow, w: WorkstreamStart, work: Work) -> Baseline:
    """The input's measurement: known when the workstream started, else waited for here.

    A workstream may start while the input is still being measured: its agents plan, write code
    and run their own jobs meanwhile. Only what compares the candidate with the input waits for
    it: the trusted measurement (so the input's own measurement is never queued behind a
    candidate's), the judge's review, and the settlement. An input that comes back unmeasurable
    does not stop the candidate's measurement: a candidate can fix what broke the input, and with
    no input value to beat it is eligible once it passes the task's gates.
    """
    if work.baseline is None:
        work.baseline = w.baseline if w.baseline is not None else await ctx.latch(INPUT_MEASURED)
    return work.baseline


async def measure_and_interpret(
    ctx: Flow, w: WorkstreamStart, ws: Workspace, revision: RevisionRef
) -> Evidence:
    """Measure ``revision`` with the trusted evaluations and decode what the evidence says.

    The backend measures again after an infrastructure failure, so a result that reaches here
    is the candidate's own; a measurement that ended without one is a failure of the workstream.
    """
    purpose = purpose_of(w)
    names = (PROFILE_STAGE,) if w.plan.kind is WorkKind.PROFILE else ctx.task.terms.measured
    recipe = MeasurementRecipe.of(ctx.facts)
    if w.plan.kind is WorkKind.PROFILE and w.plan.required_fields:
        recipe = recipe.requiring(PROFILE_STAGE, w.plan.required_fields)
    plan = recipe.plan(revision, purpose=purpose, now_at=ctx.clock.now(), stages=names)
    found = await ctx.evaluation.measure(plan, workspace=ws, within=recipe.budget(names))
    if isinstance(found, Failed):
        raise ended_by(found, None)
    keys = trusted_keys(found, scope=found.scope, candidate=revision, purpose=purpose)
    if not keys:
        none = Failure(
            kind=FailureKind.NO_TRUSTED_EVIDENCE, status=found.status, output=found.diagnostic
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
