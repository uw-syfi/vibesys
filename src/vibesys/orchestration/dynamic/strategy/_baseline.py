"""Input baseline: one trusted measurement of the starting revision.

Candidates and adoption are gated on this reading. A measurement that yields no
evidence is retried up to `max_input_measurement_attempts`; a workload rejection
of the input is a typed failure shown to the planner, not an exception.
"""

from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._draft import (
    Draft,
    measurement_plan,
    operation,
    run_scope,
)
from vibesys.orchestration.dynamic.strategy._evidence import (
    accept_readings,
    ledger_refs,
    trusted_keys,
)
from vibesys.orchestration.dynamic.strategy._ids import decision_id
from vibesys.orchestration.dynamic.strategy._operations import EvidenceReadings, InterpretEvidence
from vibesys.orchestration.dynamic.strategy._rows import reading_of
from vibesys.orchestration.dynamic.strategy._state import (
    BaselineStage,
    BaselineState,
    DynamicStrategyState,
)
from vs_core.api import EvidenceKind, Measure, MeasurementResult, RunView


def stages(config: DynamicConfig) -> tuple[str, ...]:
    """Measurement stages the run configured, accuracy first."""
    return (
        *(("accuracy",) if config.accuracy_configured else ()),
        *(("benchmark",) if config.benchmark_configured else ()),
    )


def decide(draft: Draft) -> None:
    """Propose the baseline measurement or its evidence interpretation when due."""
    baseline = draft.state.baseline
    if baseline.stage is BaselineStage.NEEDED:
        names = stages(draft.config)
        if not names:
            draft.update(
                baseline=baseline.model_copy(update={"stage": BaselineStage.NOT_CONFIGURED})
            )
            return
        identifier = decision_id("measure", "baseline", baseline.attempts)
        draft.emit(
            Measure(
                decision_id=identifier,
                scope=run_scope(draft.view),
                plan=measurement_plan(draft, draft.view.facts.baseline, "baseline", names),
            )
        )
        draft.update(
            baseline=baseline.model_copy(
                update={
                    "stage": BaselineStage.AWAITING,
                    "awaiting": identifier,
                    "attempts": baseline.attempts + 1,
                }
            )
        )
    elif baseline.stage is BaselineStage.INTERPRET:
        refs = ledger_refs(draft.view, baseline.evidence)
        if refs is None:
            draft.update(
                baseline=baseline.model_copy(
                    update={
                        "stage": BaselineStage.UNMEASURABLE,
                        "failure": "the input evidence is missing from core's ledger",
                    }
                )
            )
            return
        identifier = decision_id("interpret", "baseline", baseline.attempts)
        draft.emit(
            operation(draft, identifier, run_scope(draft.view), InterpretEvidence(evidence=refs))
        )
        draft.update(
            baseline=baseline.model_copy(
                update={"stage": BaselineStage.INTERPRETING, "awaiting": identifier}
            )
        )


def on_measurement(
    state: DynamicStrategyState, view: RunView, event: MeasurementResult, config: DynamicConfig
) -> DynamicStrategyState:
    """Record the baseline's evidence, or retry or give up when it produced none.

    Only a result for the run's own scope and generation counts, and only its
    trusted baseline evidence of the input revision.
    """
    baseline = state.baseline
    scope = run_scope(view)
    if baseline.stage is not BaselineStage.AWAITING or event.scope != scope:
        return state
    evidence = trusted_keys(event, scope=scope, candidate=view.facts.baseline, purpose="baseline")
    if evidence:
        update: dict[str, object] = {"stage": BaselineStage.INTERPRET, "evidence": evidence}
    elif baseline.attempts >= config.max_input_measurement_attempts:
        update = {
            "stage": BaselineStage.UNMEASURABLE,
            "failure": f"the input measurement produced no evidence ({event.status.value})",
        }
    else:
        update = {"stage": BaselineStage.NEEDED}
    return state.model_copy(
        update={"baseline": baseline.model_copy(update={**update, "awaiting": None})}
    )


def on_readings(
    state: DynamicStrategyState, view: RunView, outcome: EvidenceReadings
) -> DynamicStrategyState:
    """Fold decoded readings into the baseline; a failed benchmark makes it unmeasurable."""
    baseline = state.baseline
    if baseline.stage is not BaselineStage.INTERPRETING:
        return state
    refs = ledger_refs(view, baseline.evidence) or ()
    readings = accept_readings(outcome, refs)
    accuracy = None if isinstance(readings, str) else reading_of(readings, EvidenceKind.CORRECTNESS)
    benchmark = None if isinstance(readings, str) else reading_of(readings, EvidenceKind.BENCHMARK)
    failure = None
    if isinstance(readings, str):
        failure = f"the input readings were refused: {readings}"
    elif outcome.status != "succeeded" or not readings:
        failure = "the input readings could not be decoded"
    elif benchmark is not None and not benchmark.passed:
        failure = benchmark.feedback or "the input benchmark failed"
    resolved = BaselineState(
        stage=BaselineStage.UNMEASURABLE if failure else BaselineStage.MEASURED,
        attempts=baseline.attempts,
        evidence=baseline.evidence,
        accuracy_passed=None if accuracy is None else accuracy.passed,
        benchmark_passed=None if benchmark is None else benchmark.passed,
        metrics=() if benchmark is None else benchmark.metrics,
        partial=None if benchmark is None else benchmark.partial,
        failure=failure,
    )
    return state.model_copy(update={"baseline": resolved})


def on_rejected(state: DynamicStrategyState, detail: str) -> DynamicStrategyState:
    """A refused baseline measurement leaves the input unmeasurable."""
    baseline = state.baseline.model_copy(
        update={"stage": BaselineStage.UNMEASURABLE, "awaiting": None, "failure": detail}
    )
    return state.model_copy(update={"baseline": baseline})
