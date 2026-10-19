"""The input revision's trusted reading, measured once per run, as a straight-line flow.

Candidates and the choice of a winner are gated on this reading. `measure_input` measures the
input with the task's gates, has the evidence owner decode what was recorded, and returns one of
three values: `Measured`, `Unmeasurable` (a typed failure the planner is shown) or
`NotConfigured` (the task has no gate to measure). A workload that rejects the input is a typed
failure, never an exception. Infrastructure failures are the backend's: it measures again within
its own bound before a result reaches the flow.

The judgments that need no `ctx` (`input_from_readings`, `trusted`, `reading`, `failure`) are
plain functions, property-tested on their own.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vibesys.metrics import (
    EvidenceReading,
    MetricRow,
    PartialRow,
    accept_readings,
    reading_of,
)
from vs_core.api.search import (
    EvidenceKind,
    EvidenceReadingsBase,
    EvidenceRef,
    Failed,
    FailureReason,
    Flow,
    MeasurementRecipe,
    MeasurementResult,
    ObservationStatus,
    RejectionCode,
    Value,
    trusted_keys,
)


class InputFailureKind(StrEnum):
    """Why the input has no trusted reading. Closed: nothing here is free text."""

    NO_EVIDENCE = "no-evidence"
    """The measurement ended without trusted evidence of the input; ``status`` says how."""

    READINGS_REFUSED = "readings-refused"
    """The decoded readings did not match the evidence that was asked about."""

    READINGS_UNDECODABLE = "readings-undecodable"
    """The evidence owner could not decode the evidence."""

    ACCURACY_FAILED = "accuracy-failed"
    """The input failed its own trusted accuracy check (a stage that hung past its time limit
    fails it too); ``stage`` is the stage and ``output`` what it printed."""

    BENCHMARK_FAILED = "benchmark-failed"
    """The input's own benchmark failed; ``stage`` is the stage and ``output`` what it printed."""

    WAIT_ENDED = "wait-ended"
    """Core ended the measurement or its decoding without a result; ``ended`` says why."""


_STAGE_FAILURES = frozenset({InputFailureKind.ACCURACY_FAILED, InputFailureKind.BENCHMARK_FAILED})
"""The kinds that name the gate stage whose reading failed."""


class InputFailure(Value):
    """A typed reason the input is unmeasurable, with only the fields its kind has.

    ``output`` is text an evaluator printed (a benchmark's feedback, a failed job's diagnostic),
    carried as captured data for the planner prompt and never written by the search.
    """

    kind: InputFailureKind
    stage: str | None = None
    """The stage whose reading failed: set exactly for a failed accuracy check or benchmark."""
    status: ObservationStatus | None = None
    output: str = ""
    ended: FailureReason | None = None
    cause: RejectionCode | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> "InputFailure":
        """Refuse a field that does not belong to the kind, or a kind missing its field."""
        wanted = {
            "stage": self.kind in _STAGE_FAILURES,
            "status": self.kind is InputFailureKind.NO_EVIDENCE,
            "ended": self.kind is InputFailureKind.WAIT_ENDED,
        }
        for name, allowed in wanted.items():
            present = getattr(self, name) is not None
            if present != allowed:
                message = f"{name} {'does not belong to' if present else 'is required by'} {self.kind.value}"
                raise ValueError(message)
        if self.cause is not None and self.ended is not FailureReason.REFUSED:
            message = "cause belongs to an ended wait that was refused"
            raise ValueError(message)
        return self


class InputReading(Value):
    """What the trusted evaluations said about the input revision."""

    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None


class Measured(Value):
    """The input has a trusted reading: it passed every gate it was measured by.

    An input that failed its accuracy check is `Unmeasurable` (``accuracy-failed``), never a
    `Measured` reading that says so, so every reader of `failure` sees the failure.
    `input_from_readings` never makes one; a checkpoint an earlier version wrote may still hold
    one (S173), and `failure` reads it as the ``accuracy-failed`` input it was.
    """

    kind: Literal["measured"] = "measured"
    reading: InputReading


class Unmeasurable(Value):
    """The input has no trusted reading; candidates then compete on their own readings."""

    kind: Literal["unmeasurable"] = "unmeasurable"
    failure: InputFailure
    reading: InputReading | None = None


class NotConfigured(Value):
    """The task has no gate to measure, so there is nothing to measure."""

    kind: Literal["not_configured"] = "not_configured"


type Baseline = Annotated[Measured | Unmeasurable | NotConfigured, Field(discriminator="kind")]


def trusted(baseline: Baseline) -> bool:
    """Whether the input revision has a trusted reading, or no gate is configured to give one."""
    return failure(baseline) is None


def reading(baseline: Baseline) -> InputReading | None:
    """The input's reading, when there is one (an unmeasurable input may keep a partial one)."""
    match baseline:
        case Measured(reading=found) | Unmeasurable(reading=found):
            return found
        case NotConfigured():
            return None


def input_value(baseline: Baseline | None) -> float | None:
    """The input's headline value, when it was measured."""
    seen = baseline.reading if isinstance(baseline, Measured) else None
    return seen.metrics[0].value if seen is not None and seen.metrics else None


def input_metrics(baseline: Baseline | None) -> tuple[MetricRow, ...]:
    """Every reading the input's benchmark reported, headline first; empty when unmeasured."""
    return baseline.reading.metrics if isinstance(baseline, Measured) else ()


def failure(baseline: Baseline) -> InputFailure | None:
    """Why the input is unmeasurable, or None.

    A `Measured` input whose accuracy check failed is one an earlier version checkpointed
    (S173). That record kept neither the stage nor its output, so the stage is named for the
    gate (``accuracy``) and the output is empty.
    """
    match baseline:
        case Unmeasurable(failure=why):
            return why
        case Measured(reading=seen) if seen.accuracy_passed is False:
            return InputFailure(kind=InputFailureKind.ACCURACY_FAILED, stage="accuracy")
        case _:
            return None


def input_from_readings(
    refs: tuple[EvidenceRef, ...], status: str, readings: tuple[EvidenceReading, ...]
) -> Baseline:
    """Fold decoded readings into the baseline; a failed accuracy check or benchmark makes it
    unmeasurable, naming the stage that failed."""
    accepted = accept_readings(readings, refs)
    if isinstance(accepted, str):
        return Unmeasurable(failure=InputFailure(kind=InputFailureKind.READINGS_REFUSED))
    accuracy = reading_of(accepted, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(accepted, EvidenceKind.BENCHMARK)
    seen = InputReading(
        accuracy_passed=None if accuracy is None else accuracy.passed,
        benchmark_passed=None if benchmark is None else benchmark.passed,
        metrics=() if benchmark is None else benchmark.metrics,
        partial=None if benchmark is None else benchmark.partial,
    )
    if status != "succeeded" or not accepted:
        undecoded = InputFailure(kind=InputFailureKind.READINGS_UNDECODABLE)
        return Unmeasurable(failure=undecoded, reading=seen)
    # Accuracy first: a benchmark of an input that is not accurate measures nothing it must keep.
    for found, kind in (
        (accuracy, InputFailureKind.ACCURACY_FAILED),
        (benchmark, InputFailureKind.BENCHMARK_FAILED),
    ):
        if found is not None and not found.passed:
            failed = InputFailure(kind=kind, stage=found.stage, output=found.feedback)
            return Unmeasurable(failure=failed, reading=seen)
    return Measured(reading=seen)


async def measure_input(ctx: Flow) -> Baseline:
    """Measure the input revision with the task's gates and decode its evidence.

    The result is typed, never raised: an input the task cannot measure is a value the planner
    is shown.
    """
    measured = await input_measurement(ctx)
    if isinstance(measured, MeasurementResult):
        return await decode_input(ctx, measured)
    return measured


async def input_measurement(ctx: Flow) -> MeasurementResult | NotConfigured | Unmeasurable:
    """The first half of `measure_input`: the trusted measurement of the input, not decoded.

    A flow that checkpoints between the halves resumes a restart with the measurement in hand
    instead of measuring the input again.
    """
    terms = ctx.task.terms
    names = terms.measured
    if not names:
        return NotConfigured()
    recipe = MeasurementRecipe.of(ctx.facts)
    plan = recipe.plan(ctx.facts.baseline, purpose="baseline", now_at=ctx.clock.now(), stages=names)
    match await ctx.evaluation.measure(plan, within=recipe.budget(names)):
        case Failed() as failed:
            return Unmeasurable(failure=_ended(failed))
        case MeasurementResult() as result:
            return result


async def decode_input(ctx: Flow, result: MeasurementResult) -> Baseline:
    """The second half of `measure_input`: what the trusted evidence of the input says."""
    keys = trusted_keys(
        result, scope=result.scope, candidate=ctx.facts.baseline, purpose="baseline"
    )
    if not keys:
        none = InputFailure(
            kind=InputFailureKind.NO_EVIDENCE, status=result.status, output=result.diagnostic
        )
        return Unmeasurable(failure=none)
    refs = tuple(item for item in result.evidence if item.key in keys)
    match await ctx.evaluation.readings(refs, within=ctx.task.terms.operation_seconds):
        case Failed() as failed:
            return Unmeasurable(failure=_ended(failed))
        case EvidenceReadingsBase() as outcome:
            return input_from_readings(refs, outcome.status, tuple(outcome.readings))
        case None:
            return Unmeasurable(failure=InputFailure(kind=InputFailureKind.READINGS_UNDECODABLE))


def _ended(failed: Failed) -> InputFailure:
    return InputFailure(kind=InputFailureKind.WAIT_ENDED, ended=failed.reason, cause=failed.cause)
