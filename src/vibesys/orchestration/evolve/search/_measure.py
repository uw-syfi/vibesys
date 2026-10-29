"""The trusted gates of a candidate: measure it, decode the evidence, judge each configured gate."""

from vibesys.metrics import accept_readings, reading_of
from vibesys.orchestration.evolve.search._operations import EvidenceReadings
from vibesys.orchestration.evolve.search._values import Closing, Failure, FailureKind
from vibesys.search_spans.api import FAILED_CHECK, GATE, PASSED, GateAttrs
from vs_core.api.search import (
    EvidenceKind,
    EvidenceRef,
    Failed,
    Flow,
    Label,
    MeasurementRecipe,
    SpanId,
    Workspace,
    trusted_keys,
)


async def gate(ctx: Flow, ws: Workspace, closing: Closing) -> Closing:
    """Measure the candidate with the task's gates; the closing says how it did.

    A gate that rejects the candidate ends the child as a failed individual carrying the gate's
    feedback (`passed` is false, no cause); a child does not retry its mutation. A measurement
    that ended without trusted evidence says nothing about the candidate, so the child earns no
    individual. The backend measures again after an infrastructure failure, so a result that
    reaches here is the candidate's own.
    """
    revision = closing.revision
    if revision is None:
        message = "only a candidate is measured"
        raise ValueError(message)
    names = ctx.task.terms.measured
    recipe = MeasurementRecipe.of(ctx.facts)
    plan = recipe.plan(revision, purpose="official", now_at=ctx.clock.now(), stages=names)
    stage = ",".join(names)
    label = Label(GATE, title=" + ".join(names), attrs=GateAttrs(stage=stage))
    found = await ctx.evaluation.measure(
        plan, workspace=ws, within=recipe.budget(names), label=label
    )
    gate_span = ctx.operation_span
    if isinstance(found, Failed):
        return closing.ended_by(Failure(kind=FailureKind.UNMEASURED, ended=found.reason))
    keys = trusted_keys(found, scope=found.scope, candidate=revision, purpose="official")
    if not keys:
        none = Failure(
            kind=FailureKind.UNMEASURED, status=found.status.value, detail=found.diagnostic
        )
        return closing.ended_by(none)
    refs = tuple(item for item in found.evidence if item.key in keys)
    measured = await interpret(ctx, ws, closing, refs)
    if gate_span is not None and measured.readings:
        label_reading(ctx, gate_span, stage, measured)
    return measured


def label_reading(ctx: Flow, gate: SpanId, stage: str, measured: Closing) -> None:
    """Label the gate with the value it read and its verdict: passed, or failed a check.

    Evolve does not measure the input, so there is no ratio to it and no gate is slower.
    """
    attrs = GateAttrs.read(stage, measured.readings)
    ctx.label(GATE, span=gate, attrs=attrs, verdict=PASSED if measured.passed else FAILED_CHECK)


async def interpret(
    ctx: Flow, ws: Workspace, closing: Closing, refs: tuple[EvidenceRef, ...]
) -> Closing:
    """Have the evidence owner decode the gates' readings: all passing passes the child."""
    seconds = ctx.task.terms.operation_seconds
    outcome = await ctx.evaluation.readings(refs, workspace=ws, within=seconds)
    if isinstance(outcome, Failed):
        unread = Failure(kind=FailureKind.EVIDENCE_UNDECODABLE, status=outcome.reason.value)
        return closing.ended_by(unread)
    if not isinstance(outcome, EvidenceReadings):
        return closing.ended_by(Failure(kind=FailureKind.EVIDENCE_UNDECODABLE, status="none"))
    readings = accept_readings(outcome.readings, refs)
    if isinstance(readings, str):
        return closing.ended_by(Failure(kind=FailureKind.EVIDENCE_REFUSED, detail=readings))
    if outcome.status != "succeeded" or not readings:
        undecoded = Failure(kind=FailureKind.EVIDENCE_UNDECODABLE, status=outcome.status)
        return closing.ended_by(undecoded)
    measured = closing.model_copy(update={"readings": readings})
    for kind in (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK):
        seen = reading_of(readings, kind)
        if seen is not None and not seen.passed:
            feedback = seen.feedback or f"{seen.kind.value} gate failed"
            return measured.model_copy(update={"passed": False, "feedback": feedback})
    return measured
