"""The trusted gates of a candidate: measure it with the task's stages and read what they say.

The measurement's span is the round's gate: it is labelled with the stages it ran and, once the
evidence is read, with the candidate's headline value, its ratio to the input's and the verdict.

There is one measurement path. The framework, not the agent, picks the revision it scores (the
one the round retains) and measures it with the official gates, serially, for every idea that
passes review. An agent's own evaluation (the evaluation tool)
may run while other agent work shares its host, so its timing is not comparable; the judge may
read it, but it is never a number the run shows or rests on. A round the gates did not measure
has no gate and no result.
"""

from __future__ import annotations

from vibesys.candidates.api import Baseline, input_metrics
from vibesys.metrics import AcceptedReading, accept_readings, reading_of
from vibesys.search_spans.api import FAILED_CHECK, GATE, PASSED, SLOWER, GateAttrs
from vibesys.serial_search._closing import beats_input, failed_gate
from vibesys.serial_search._plan_view import MeasurementNoise
from vs_core.api.search import (
    Comparison,
    ComparisonPolicy,
    EvidenceKind,
    EvidenceRef,
    Failed,
    Flow,
    Label,
    MeasurementRecipe,
    MeasurementResult,
    RevisionRef,
    SpanId,
    Value,
    Workspace,
    effect_resolution,
    terms_policy,
    trusted_keys,
)


class Gated(Value):
    """The gates ran and their readings were decoded; a gate may still have rejected."""

    readings: tuple[AcceptedReading, ...]
    comparison: Comparison | None = None
    """The candidate compared with its base over every sample core holds of its input.

    The agent's own evaluations of the same input are samples of it, so this is the one
    comparison the agent was told of and the framework's verdict rests on; None when the
    candidate has no base's samples (no benchmark stage, or a base that could not be measured)."""
    noise: MeasurementNoise | None = None
    """The measurement noise ``comparison`` states, which the next designer is shown."""


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
    compared = await ctx.evaluation.measure_compared(
        plan, workspace=ws, within=recipe.budget(names), label=label
    )
    found = compared if isinstance(compared, Failed) else compared.result
    gate = ctx.operation_span
    result = await _gated(ctx, ws, revision, found)
    if isinstance(result, Gated) and not isinstance(compared, Failed):
        noise = _noise_of(compared.comparison, terms_policy(terms))
        result = result.model_copy(
            update={
                "readings": _settled(result.readings, compared.comparison),
                "comparison": compared.comparison,
                "noise": noise,
            }
        )
    if gate is not None and isinstance(result, Gated):
        _label_reading(ctx, gate, stage, result.readings, baseline)
    return result


def _settled(
    readings: tuple[AcceptedReading, ...], comparison: Comparison | None
) -> tuple[AcceptedReading, ...]:
    """``readings`` with the benchmark's headline stated by the settled comparison.

    The record the gate read states the mean of the samples held when the official check was
    answered, and the runs a close call earns land after it. The comparison, awaited until
    none is under way, holds them all, so its candidate value is the one figure the round
    reports, ranks by and labels its gate with. The record's other rows are the answering run's
    own (a sample holds the headline only), and a comparison of another metric changes nothing.
    """
    reading = reading_of(readings, EvidenceKind.BENCHMARK)
    if comparison is None or reading is None or not reading.metrics:
        return readings
    headline, *rest = reading.metrics
    if headline.name != comparison.metric:
        return readings
    stated = headline.model_copy(update={"value": comparison.candidate_value})
    settled = reading.model_copy(update={"metrics": (stated, *rest)})
    return tuple(settled if item is reading else item for item in readings)


def _noise_of(comparison: Comparison | None, policy: ComparisonPolicy) -> MeasurementNoise | None:
    """The noise a comparison states, or None when there is none or it estimates no noise.

    A comparison over too few runs (no declared floor and fewer than three a side) or over runs
    that read alike has no estimate of its noise, so it states none.
    """
    resolution = None if comparison is None else effect_resolution(comparison, policy)
    if comparison is None or resolution is None:
        return None
    return MeasurementNoise(
        noise=comparison.noise,
        resolution=resolution,
        candidate_samples=comparison.candidate_samples,
        base_samples=comparison.base_samples,
    )


def _label_reading(
    ctx: Flow,
    gate: SpanId,
    stage: str,
    readings: tuple[AcceptedReading, ...],
    baseline: Baseline,
) -> None:
    """Label the gate with what its readings say: the value, the ratio to the input's, the verdict."""
    attrs = GateAttrs.read(stage, readings, input_metrics(baseline))
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
