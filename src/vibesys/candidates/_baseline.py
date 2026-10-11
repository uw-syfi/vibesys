"""Input baseline: one trusted measurement of the starting revision.

Candidates and adoption are gated on this reading. A measurement that yields no evidence is
retried up to `max_input_measurement_attempts`; a workload rejection of the input is a typed
failure shown to the planner, not an exception. Every function takes the `BaselineState` and
returns the next one (and the decisions to propose); the strategy keeps the state.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from vibesys.candidates._config import CandidateConfig
from vibesys.metrics import EvidenceReading, MetricRow, PartialRow, accept_readings, reading_of
from vs_core.api import (
    Decision,
    DecisionId,
    EvidenceKey,
    EvidenceKind,
    EvidenceRef,
    IdScheme,
    Measure,
    MeasurementRecipe,
    MeasurementResult,
    Operation,
    OperationRequest,
    RunView,
    Value,
    declared_operation,
    ledger_refs,
    may_resubmit,
    run_scope,
    trusted_keys,
)


class BaselineStage(StrEnum):
    """Input measurement progress; the baseline gates candidates and adoption."""

    NEEDED = "needed"
    AWAITING = "awaiting"
    INTERPRET = "interpret"
    INTERPRETING = "interpreting"
    MEASURED = "measured"
    UNMEASURABLE = "unmeasurable"
    NOT_CONFIGURED = "not-configured"


class BaselineState(Value):
    """The input revision's reading, measured once per run."""

    stage: BaselineStage = BaselineStage.NEEDED
    attempts: int = Field(default=0, ge=0)
    awaiting: DecisionId | None = None
    evidence: tuple[EvidenceKey, ...] = ()
    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None
    failure: str | None = None


def stages(config: CandidateConfig) -> tuple[str, ...]:
    """Measurement stages the run configured, accuracy first."""
    return (
        *(("accuracy",) if config.accuracy_configured else ()),
        *(("benchmark",) if config.benchmark_configured else ()),
    )


@dataclass(frozen=True)
class InputMeasurement:
    """What the strategy supplies to measure its input: identities, recipe and its own operation."""

    ids: IdScheme
    recipe: MeasurementRecipe
    interpret: Callable[[tuple[EvidenceRef, ...]], OperationRequest]
    """Builds the strategy's operation request that decodes the recorded evidence."""
    operation_seconds: float


def propose(
    baseline: BaselineState, view: RunView, config: CandidateConfig, measuring: InputMeasurement
) -> tuple[BaselineState, tuple[Decision, ...]]:
    """Propose the baseline measurement or its evidence interpretation when due."""
    ids, recipe = measuring.ids, measuring.recipe
    if baseline.stage is BaselineStage.NEEDED:
        names = stages(config)
        if not names:
            return baseline.model_copy(update={"stage": BaselineStage.NOT_CONFIGURED}), ()
        identifier = ids.decision_id("measure", "baseline", baseline.attempts)
        decision = Measure(
            decision_id=identifier,
            scope=run_scope(view),
            plan=recipe.plan(
                view.facts.baseline,
                purpose="baseline",
                now_at=view.run.now_at,
                stages=names,
                submission_limit=config.max_input_measurement_attempts,
            ),
        )
        return (
            baseline.model_copy(
                update={
                    "stage": BaselineStage.AWAITING,
                    "awaiting": identifier,
                    "attempts": baseline.attempts + 1,
                }
            ),
            (decision,),
        )
    if baseline.stage is BaselineStage.INTERPRET:
        refs = ledger_refs(view, baseline.evidence)
        if refs is None:
            return (
                baseline.model_copy(
                    update={
                        "stage": BaselineStage.UNMEASURABLE,
                        "failure": "the input evidence is missing from core's ledger",
                    }
                ),
                (),
            )
        identifier = ids.decision_id("interpret", "baseline", baseline.attempts)
        operation: Operation = declared_operation(
            view,
            identifier,
            run_scope(view),
            measuring.interpret(refs),
            seconds=measuring.operation_seconds,
        )
        return (
            baseline.model_copy(
                update={"stage": BaselineStage.INTERPRETING, "awaiting": identifier}
            ),
            (operation,),
        )
    return baseline, ()


def on_measurement(
    baseline: BaselineState, view: RunView, event: MeasurementResult, config: CandidateConfig
) -> BaselineState:
    """Record the baseline's evidence, or retry or give up when it produced none.

    Only a result for the run's own scope and generation counts, and only its
    trusted baseline evidence of the input revision.
    """
    scope = run_scope(view)
    if baseline.stage is not BaselineStage.AWAITING or event.scope != scope:
        return baseline
    evidence = trusted_keys(event, scope=scope, candidate=view.facts.baseline, purpose="baseline")
    if may_resubmit(
        event.failure, submissions=baseline.attempts, limit=config.max_input_measurement_attempts
    ):
        update: dict[str, object] = {"stage": BaselineStage.NEEDED}
    elif evidence:
        update = {"stage": BaselineStage.INTERPRET, "evidence": evidence}
    elif baseline.attempts >= config.max_input_measurement_attempts:
        update = {
            "stage": BaselineStage.UNMEASURABLE,
            "failure": (
                f"the input measurement produced no evidence ({event.status.value})"
                + (f": {event.diagnostic}" if event.diagnostic else "")
            ),
        }
    else:
        update = {"stage": BaselineStage.NEEDED}
    return baseline.model_copy(update={**update, "awaiting": None})


def on_readings(
    baseline: BaselineState,
    view: RunView,
    *,
    status: str,
    readings: tuple[EvidenceReading, ...],
) -> BaselineState:
    """Fold decoded readings into the baseline; a failed benchmark makes it unmeasurable."""
    if baseline.stage is not BaselineStage.INTERPRETING:
        return baseline
    refs = ledger_refs(view, baseline.evidence) or ()
    accepted = accept_readings(readings, refs)
    accuracy = None if isinstance(accepted, str) else reading_of(accepted, EvidenceKind.CORRECTNESS)
    benchmark = None if isinstance(accepted, str) else reading_of(accepted, EvidenceKind.BENCHMARK)
    failure = None
    if isinstance(accepted, str):
        failure = f"the input readings were refused: {accepted}"
    elif status != "succeeded" or not accepted:
        failure = "the input readings could not be decoded"
    elif benchmark is not None and not benchmark.passed:
        failure = benchmark.feedback or "the input benchmark failed"
    return BaselineState(
        stage=BaselineStage.UNMEASURABLE if failure else BaselineStage.MEASURED,
        attempts=baseline.attempts,
        evidence=baseline.evidence,
        accuracy_passed=None if accuracy is None else accuracy.passed,
        benchmark_passed=None if benchmark is None else benchmark.passed,
        metrics=() if benchmark is None else benchmark.metrics,
        partial=None if benchmark is None else benchmark.partial,
        failure=failure,
    )


def on_rejected(baseline: BaselineState, detail: str) -> BaselineState:
    """A refused baseline measurement leaves the input unmeasurable."""
    return baseline.model_copy(
        update={"stage": BaselineStage.UNMEASURABLE, "awaiting": None, "failure": detail}
    )
