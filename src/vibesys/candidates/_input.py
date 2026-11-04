"""The input revision's trusted reading, measured once per run, as a straight-line flow.

Candidates and the choice of a winner are gated on this reading. `measure_input` measures the
input with the task's gates, has the evidence owner decode what was recorded, and returns one of
three values: `Measured`, `Unmeasurable` (a typed failure the planner is shown) or
`NotConfigured` (the task has no gate to measure). A workload that rejects the input is a typed
failure, never an exception. Infrastructure failures are the backend's first: it measures again
within its own bound before a result reaches the flow. A failure that still shows no fault of the
input (no evidence, a wait core ended, a stage the backend could not blame on the input, or one
that printed nothing) is measured once more here (`again_after`), and the result keeps the first
attempt's failure (`first_attempt`).

An input that failed only its accuracy check but whose benchmark passed keeps its benchmark
numbers as the bar candidates must beat (`input_metrics`). With no bar at all, a candidate is
judged on the task's gates alone, and the run says so when it adopts one (`uncompared`).

The judgments that need no `ctx` (`input_from_readings`, `trusted`, `reading`, `failure`) are
plain functions, property-tested on their own.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, Discriminator, Tag, model_validator

from vibesys.metrics import (
    EvidenceReading,
    MetricRow,
    PartialRow,
    accept_readings,
    reading_of,
)
from vs_core.api import MeasurementFailure
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


_STAGE_FAILURES = {
    InputFailureKind.ACCURACY_FAILED: "accuracy",
    InputFailureKind.BENCHMARK_FAILED: "benchmark",
}
"""The kinds that name the gate stage whose reading failed, with the stage a record written
before stages were recorded is read as having failed (the gate's own name)."""

_ENDED_AGAIN = frozenset({FailureReason.TIMED_OUT, FailureReason.LOST, FailureReason.OWNER_GONE})
"""The ends of a wait that say nothing of the input: it ran out of time, or was lost."""


class InputFailure(Value):
    """A typed reason the input is unmeasurable, with only the fields its kind has.

    ``output`` is text an evaluator printed (a benchmark's feedback, a failed job's diagnostic),
    carried as captured data for the planner prompt and never written by the search.
    """

    kind: InputFailureKind
    stage: str | None = None
    """The stage whose reading failed: only a failed accuracy check or benchmark has one. A
    record written before stages were kept has none; loading it as a `Baseline` names the
    gate's own stage (`_current_shape`)."""
    blamed: MeasurementFailure | None = None
    """Whom the backend blamed for the failed stage's measurement (None: not classified, as in
    a record written before this was kept). Only a failed accuracy check or benchmark has it."""
    status: ObservationStatus | None = None
    output: str = ""
    ended: FailureReason | None = None
    cause: RejectionCode | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> "InputFailure":
        """Refuse a field that does not belong to the kind, or a kind missing its field."""
        if self.blamed is not None and self.kind not in _STAGE_FAILURES:
            message = f"blamed does not belong to {self.kind.value}"
            raise ValueError(message)
        if self.stage is not None and self.kind not in _STAGE_FAILURES:
            message = f"stage does not belong to {self.kind.value}"
            raise ValueError(message)
        wanted = {
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


def again_after(failure: InputFailure) -> bool:
    """Whether a failed measurement of the input shows no fault of the input, so it is taken again.

    That is a measurement with no trusted evidence, a wait core ended on its time or lost, or a
    failed stage the backend did not blame on the input (its time limit, the machinery) or that
    printed nothing. A stage the input failed on its own, and readings that do not decode, are
    final. The flow measures again once at most.
    """
    match failure.kind:
        case InputFailureKind.NO_EVIDENCE:
            return True
        case InputFailureKind.WAIT_ENDED:
            return failure.ended in _ENDED_AGAIN
        case InputFailureKind.ACCURACY_FAILED | InputFailureKind.BENCHMARK_FAILED:
            unblamed = (
                failure.blamed is not None and failure.blamed is not MeasurementFailure.WORKLOAD
            )
            return unblamed or not failure.output.strip()
        case InputFailureKind.READINGS_REFUSED | InputFailureKind.READINGS_UNDECODABLE:
            return False


class InputReading(Value):
    """What the trusted evaluations said about the input revision."""

    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None


class Measured(Value):
    """The input has a trusted reading: it passed every gate it was measured by.

    An input that failed its accuracy check is `Unmeasurable` (``accuracy-failed``), never a
    `Measured` reading that says so. A record an earlier version wrote that holds one is read as
    the ``accuracy-failed`` input it was (`Baseline`'s migration), so no reader sees both.
    """

    kind: Literal["measured"] = "measured"
    reading: InputReading
    first_attempt: InputFailure | None = None
    """Why the first measurement failed, when the input was measured again (`again_after`)."""

    @model_validator(mode="after")
    def passed_its_gates(self) -> "Measured":
        """Refuse a measured input that failed its accuracy check: that input is unmeasurable."""
        if self.reading.accuracy_passed is False:
            message = "a measured input passed its accuracy check; one that failed is unmeasurable"
            raise ValueError(message)
        return self


class Unmeasurable(Value):
    """The input has no trusted reading; candidates then compete on their own readings.

    One that failed only its accuracy check, while its benchmark passed, still has a bar for
    them to beat: its benchmark numbers (`input_metrics`).
    """

    kind: Literal["unmeasurable"] = "unmeasurable"
    failure: InputFailure
    reading: InputReading | None = None
    first_attempt: InputFailure | None = None
    """Why the first measurement failed, when the input was measured again (`again_after`)."""


class NotConfigured(Value):
    """The task has no gate to measure, so there is nothing to measure."""

    kind: Literal["not_configured"] = "not_configured"


class _MeasuredBeforeS173(Measured):
    """A record an earlier version wrote for an input that failed its accuracy check (S173).

    Before S173 such an input was written as ``measured`` with ``accuracy_passed: false``.
    Loading a `Baseline` reads it into this class only to turn it into the ``accuracy-failed``
    input it was (`_current_shape`), so no instance ever leaves validation.
    """

    @model_validator(mode="after")
    def passed_its_gates(self) -> "_MeasuredBeforeS173":
        """Accept the failed accuracy check `Measured` refuses: that is what this record holds."""
        return self


def _shape_of(data: object) -> str | None:
    """The tag of the class ``data`` validates as: its ``kind``, unless it is a pre-S173 record."""
    if isinstance(data, Measured | Unmeasurable | NotConfigured):
        return data.kind
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    seen = data.get("reading")
    failed = seen.get("accuracy_passed") if isinstance(seen, dict) else None
    if isinstance(seen, InputReading):
        failed = seen.accuracy_passed
    if kind == "measured" and failed is False:
        return "measured-before-s173"
    return kind if isinstance(kind, str) else None


def _current_shape(baseline: Measured | Unmeasurable | NotConfigured) -> object:
    """Read a record an earlier version wrote as the input it describes; one migration for all.

    - A pre-S173 ``measured`` input whose accuracy check failed is the ``accuracy-failed``
      input it was, keeping what it measured as its reading (so a benchmark it passed stays
      the bar, `input_metrics`). It kept no stage or output.
    - A stage failure written before stages were kept (``benchmark-failed`` with no
      ``stage``) failed its gate's own stage: ``benchmark``, or ``accuracy``.

    A record of the current shape passes through unchanged.
    """
    if isinstance(baseline, _MeasuredBeforeS173):
        failed = InputFailure(kind=InputFailureKind.ACCURACY_FAILED)
        baseline = Unmeasurable(failure=failed, reading=baseline.reading)
    if isinstance(baseline, Unmeasurable) and baseline.failure.stage is None:
        stage = _STAGE_FAILURES.get(baseline.failure.kind)
        if stage is not None:
            named = baseline.failure.model_copy(update={"stage": stage})
            baseline = baseline.model_copy(update={"failure": named})
    return baseline


# The pre-S173 class is a `Measured`, so to a type checker this is the plain closed union.
type Baseline = Annotated[
    Annotated[Measured, Tag("measured")]
    | Annotated[Unmeasurable, Tag("unmeasurable")]
    | Annotated[NotConfigured, Tag("not_configured")]
    | Annotated[_MeasuredBeforeS173, Tag("measured-before-s173")],
    Discriminator(_shape_of),
    AfterValidator(_current_shape),
]


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
    """The input's headline value: the bar's first number, when there is a bar."""
    bar = input_metrics(baseline)
    return bar[0].value if bar else None


def input_metrics(baseline: Baseline | None) -> tuple[MetricRow, ...]:
    """The bar a candidate must beat: every reading the input's benchmark reported, headline first.

    A measured input's benchmark, and the benchmark of an input that failed only its accuracy
    check while its benchmark passed (its numbers are still the input's throughput). Empty
    when there is nothing to beat: the input is pending, unconfigured, or failed otherwise.
    """
    match baseline:
        case Measured(reading=seen):
            return seen.metrics
        case Unmeasurable(failure=why, reading=seen) if (
            why.kind is InputFailureKind.ACCURACY_FAILED
            and seen is not None
            and seen.benchmark_passed is True
        ):
            return seen.metrics
        case _:
            return ()


def uncompared(baseline: Baseline | None) -> bool:
    """Whether a candidate adopted now was never compared with the input: it failed with no bar."""
    return isinstance(baseline, Unmeasurable) and not input_metrics(baseline)


def failure(baseline: Baseline) -> InputFailure | None:
    """Why the input is unmeasurable, or None."""
    return baseline.failure if isinstance(baseline, Unmeasurable) else None


def with_first_attempt(baseline: Baseline, first: InputFailure | None) -> Baseline:
    """``baseline``, the result of measuring the input again after ``first`` failed."""
    if first is None or isinstance(baseline, NotConfigured):
        return baseline
    return baseline.model_copy(update={"first_attempt": first})


def input_from_readings(
    refs: tuple[EvidenceRef, ...],
    status: str,
    readings: tuple[EvidenceReading, ...],
    blamed: MeasurementFailure | None = None,
) -> Baseline:
    """Fold decoded readings into the baseline; a failed check makes it unmeasurable.

    A failed accuracy check or benchmark names the stage that failed, accuracy first, and whom
    the backend blamed for the measurement (``blamed``, the measurement's own classification).
    """
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
            failed = InputFailure(
                kind=kind, stage=found.stage, blamed=blamed, output=found.feedback
            )
            return Unmeasurable(failure=failed, reading=seen)
    return Measured(reading=seen)


async def measure_input(ctx: Flow) -> Baseline:
    """Measure the input revision with the task's gates and decode its evidence.

    The result is typed, never raised: an input the task cannot measure is a value the planner
    is shown. A failure that shows no fault of the input is measured once more (`again_after`);
    the second result stands, and keeps the first's failure.
    """
    first = await _measured_once(ctx)
    why = failure(first)
    if why is None or not again_after(why):
        return first
    return with_first_attempt(await _measured_once(ctx), why)


async def _measured_once(ctx: Flow) -> Baseline:
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
            return input_from_readings(
                refs, outcome.status, tuple(outcome.readings), blamed=result.failure
            )
        case None:
            return Unmeasurable(failure=InputFailure(kind=InputFailureKind.READINGS_UNDECODABLE))


def _ended(failed: Failed) -> InputFailure:
    return InputFailure(kind=InputFailureKind.WAIT_ENDED, ended=failed.reason, cause=failed.cause)
