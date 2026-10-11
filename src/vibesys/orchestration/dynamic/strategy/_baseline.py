"""Dynamic's binding of the input baseline (`vibesys.candidates`) to its state and operations.

The baseline policy lives in `vibesys.candidates`; this module supplies what is dynamic's: the
`dyn` identities, the measurement recipe built from its config, the `InterpretEvidence`
operation and where the baseline sits in its state.
"""

from vibesys.candidates import api as candidates
from vibesys.orchestration.dynamic.strategy._draft import Draft, measurement_recipe
from vibesys.orchestration.dynamic.strategy._ids import IDS
from vibesys.orchestration.dynamic.strategy._operations import EvidenceReadings, InterpretEvidence
from vibesys.orchestration.dynamic.strategy._state import DynamicStrategyState
from vs_core.api import MeasurementResult, RunView

from ._config import DynamicConfig


def decide(draft: Draft) -> None:
    """Propose the baseline measurement or its evidence interpretation when due."""
    baseline, decisions = candidates.propose(
        draft.state.baseline,
        draft.view,
        draft.config,
        candidates.InputMeasurement(
            ids=IDS,
            recipe=measurement_recipe(draft.config, draft.view.facts),
            interpret=lambda refs: InterpretEvidence(evidence=refs),
            operation_seconds=draft.config.operation_seconds,
        ),
    )
    for decision in decisions:
        draft.emit(decision)
    if baseline != draft.state.baseline:
        draft.update(baseline=baseline)


def on_measurement(
    state: DynamicStrategyState, view: RunView, event: MeasurementResult, config: DynamicConfig
) -> DynamicStrategyState:
    """Record the baseline's evidence, or retry or give up when it produced none."""
    baseline = candidates.on_measurement(state.baseline, view, event, config)
    return state.model_copy(update={"baseline": baseline})


def on_readings(
    state: DynamicStrategyState, view: RunView, outcome: EvidenceReadings
) -> DynamicStrategyState:
    """Fold decoded readings into the baseline; a failed benchmark makes it unmeasurable."""
    baseline = candidates.on_readings(
        state.baseline, view, status=outcome.status, readings=outcome.readings
    )
    return state.model_copy(update={"baseline": baseline})


def on_rejected(state: DynamicStrategyState, detail: str) -> DynamicStrategyState:
    """A refused baseline measurement leaves the input unmeasurable."""
    return state.model_copy(update={"baseline": candidates.on_rejected(state.baseline, detail)})
