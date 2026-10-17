"""The trusted gates of a candidate: measure it with the task's stages and read what they say.

The measurement's span is the round's gate: it is labelled with the stages it ran and, once the
evidence is read, with the candidate's headline value, its ratio to the input's and the verdict.

The search runs its gates on a cadence, so a round may keep a candidate they never measured. Its
agents may have measured it themselves through the evaluation tool, which core holds as trusted
evidence of the attempt (``label_taken``): the round's gate is then the reading of the latest of
those that read a value or failed a check, labelled the same way, so every measured round says
what its candidate measured.
"""

from __future__ import annotations

from vibesys.candidates.api import Baseline, input_value
from vibesys.metrics import AcceptedReading, accept_readings, reading_of
from vibesys.search_spans.api import FAILED_CHECK, GATE, PASSED, SLOWER, GateAttrs
from vibesys.serial_search._closing import beats_input, failed_gate
from vs_core.api.search import (
    EvidenceKind,
    EvidenceRef,
    Failed,
    Flow,
    Label,
    MeasurementRecipe,
    MeasurementResult,
    RevisionRef,
    SpanId,
    TaskTerms,
    Value,
    Workspace,
    trusted_keys,
)


class Gated(Value):
    """The gates ran and their readings were decoded; a gate may still have rejected."""

    readings: tuple[AcceptedReading, ...]


class NotMeasured(Value):
    """The machinery could not measure the candidate; nothing is known against it."""

    feedback: str


class Unreadable(Value):
    """The evidence of the gates could not be read back; the round fails with this reason."""

    reason: str


type GateResult = Gated | NotMeasured | Unreadable


async def measure_gates(
    ctx: Flow, ws: Workspace, revision: RevisionRef, baseline: Baseline
) -> GateResult:
    """Measure ``revision`` with the task's stages, then have the evidence owner decode them.

    The backend measures again after an infrastructure failure, so what reaches here is the
    candidate's own result or a measurement that ended without one. ``baseline`` is the input's
    measurement, which the gate's label compares the candidate with.
    """
    terms = ctx.task.terms
    names = terms.measured
    recipe = MeasurementRecipe.of(ctx.facts)
    plan = recipe.plan(revision, purpose="official", now_at=ctx.clock.now(), stages=names)
    stage = ",".join(names)
    label = Label(GATE, title=" + ".join(names), attrs=GateAttrs(stage=stage))
    found = await ctx.evaluation.measure(
        plan, workspace=ws, within=recipe.budget(names), label=label
    )
    gate = ctx.operation_span
    result = await _gated(ctx, ws, revision, found)
    if gate is not None and isinstance(result, Gated):
        _label_reading(ctx, gate, stage, result.readings, baseline)
    return result


def _label_reading(
    ctx: Flow,
    gate: SpanId,
    stage: str,
    readings: tuple[AcceptedReading, ...],
    baseline: Baseline,
) -> None:
    """Label the gate with what its readings say: the value, the ratio to the input's, the verdict."""
    attrs = GateAttrs.read(stage, readings, input_value(baseline))
    ctx.label(GATE, span=gate, attrs=attrs, verdict=_verdict(ctx, readings, baseline))


def _verdict(ctx: Flow, readings: tuple[AcceptedReading, ...], baseline: Baseline) -> str | None:
    """What the readings say of the candidate, or None when they lack the check it is gated on.

    It passed when every trusted gate passed and the candidate beats the input; it is slower when
    it does not beat the input, and failed a check when a gate rejected it. Readings without the
    accuracy check a task requires cannot pass it (an agent may have measured only the benchmark).
    """
    terms = ctx.task.terms
    if failed_gate(readings) is not None:
        return FAILED_CHECK
    if terms.requires_accuracy_proof and reading_of(readings, EvidenceKind.CORRECTNESS) is None:
        return None
    if not beats_input(baseline, readings, terms):
        return SLOWER
    return PASSED


async def label_taken(ctx: Flow, ws: Workspace, revision: RevisionRef, baseline: Baseline) -> None:
    """Label what the agents' latest telling evaluation of ``revision`` read as its gate.

    For a round whose trusted gates did not measure the candidate it keeps: its agents' own
    trusted evaluations of that exact revision (``ctx.evaluation.taken``) are what is known of
    it. They are read in one operation, whose span is labelled a gate with what the latest
    evaluation that tells something read (`_latest_telling`): its stages, the value, the ratio to
    the input's and the verdict, as the trusted gates' own are. Nothing is labelled when no
    evaluation tells anything or the evidence cannot be read: the round's outcome does not
    depend on it.
    """
    refs = ctx.evaluation.taken(ws, revision)
    if not refs:
        return
    read = await _read(ctx, ws, refs)
    gate = ctx.operation_span
    if gate is None or not isinstance(read, Gated):
        return
    readings = _latest_telling(_as_gates(read.readings, ctx.task.terms), refs)
    if not readings:
        return
    names = tuple(dict.fromkeys(item.stage for item in readings))
    attrs = GateAttrs.read(",".join(names), readings, input_value(baseline))
    verdict = _verdict(ctx, readings, baseline)
    ctx.label(GATE, span=gate, title=" + ".join(names), attrs=attrs, verdict=verdict)


def _as_gates(
    readings: tuple[AcceptedReading, ...], terms: TaskTerms
) -> tuple[AcceptedReading, ...]:
    """An agent's evaluation read as the gates its stages are.

    Core records each stage an agent evaluated as one ``local-validation`` record, never as
    proof; the task's terms say which of the stages it measures is the accuracy check, and the
    others are its benchmark. Stages the task does not measure are no gate.
    """
    return tuple(
        item.model_copy(
            update={
                "kind": EvidenceKind.CORRECTNESS
                if item.stage == terms.accuracy_stage
                else EvidenceKind.BENCHMARK
            }
        )
        for item in readings
        if item.kind is EvidenceKind.LOCAL_VALIDATION and item.stage in terms.measured
    )


def _latest_telling(
    readings: tuple[AcceptedReading, ...], refs: tuple[EvidenceRef, ...]
) -> tuple[AcceptedReading, ...]:
    """The readings of the latest evaluation (``refs`` are oldest first) that tells something.

    An evaluation tells something when it read a benchmark value or a check rejected the
    candidate; one whose only stage was an accuracy check that passed says neither.
    """
    for request in reversed(tuple(dict.fromkeys(item.source_request for item in refs))):
        taken = tuple(item for item in readings if item.source_request == request)
        benchmark = reading_of(taken, EvidenceKind.BENCHMARK)
        valued = benchmark is not None and benchmark.headline() is not None
        if valued or failed_gate(taken) is not None:
            return taken
    return ()


async def _gated(
    ctx: Flow, ws: Workspace, revision: RevisionRef, found: MeasurementResult | Failed
) -> GateResult:
    if isinstance(found, Failed):
        return NotMeasured(feedback=f"the benchmark could not be measured ({found.reason.value})")
    keys = trusted_keys(found, scope=found.scope, candidate=revision, purpose="official")
    if not keys:
        reason = f"the benchmark could not be measured ({found.status.value})"
        return NotMeasured(feedback=f"{reason}: {found.diagnostic}" if found.diagnostic else reason)
    refs = tuple(item for item in found.evidence if item.key in keys)
    return await _read(ctx, ws, refs)


async def _read(ctx: Flow, ws: Workspace, refs: tuple[EvidenceRef, ...]) -> Gated | Unreadable:
    """Have the evidence owner decode the gates' evidence into readings."""
    outcome = await ctx.evaluation.readings(
        refs, workspace=ws, within=ctx.task.terms.operation_seconds
    )
    if isinstance(outcome, Failed):
        return Unreadable(reason=f"evidence readings ended ({outcome.reason.value})")
    if outcome is None:
        return Unreadable(reason="evidence could not be decoded")
    accepted = accept_readings(outcome.readings, refs)
    if isinstance(accepted, str):
        return Unreadable(reason=f"evidence readings refused: {accepted}")
    if outcome.status != "succeeded" or not accepted:
        return Unreadable(reason=f"evidence could not be decoded ({outcome.status})")
    return Gated(readings=accepted)
