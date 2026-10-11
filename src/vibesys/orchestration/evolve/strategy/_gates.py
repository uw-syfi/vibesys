"""After the judge: the framework's official gates, then the advisory profile.

A passing review is followed by the gates the run configured (accuracy, then benchmark). A gate
that rejects the candidate ends the child as a failed individual carrying the gate's feedback; a
child does not retry its mutation. A measurement the machinery could not complete says nothing
about the candidate, so it is submitted again within core's bound and, past it, the child earns
no individual. A candidate that passed every gate is then profiled when the run has a profiler.
"""

from vibesys.candidates.api import stages
from vibesys.metrics import accept_readings, reading_of
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._operations import EvidenceReadings
from vibesys.orchestration.evolve.strategy._record import fail, put, slot_of, to_settle
from vibesys.orchestration.evolve.strategy._state import (
    EvolveStrategyState,
    SlotState,
    WorkPhase,
)
from vs_core.api import (
    EvidenceKind,
    MeasurementResult,
    RunView,
    Step,
    ledger_refs,
    may_resubmit,
    trusted_keys,
)


def after_review(config: EvolveConfig, slot: SlotState) -> SlotState:
    """A passing review: measure the configured gates, else go on to the profile or settle."""
    if stages(config) and slot.candidate is not None:
        return slot.model_copy(
            update={"phase": WorkPhase.MEASURE, "step": Step.NEEDED, "awaiting": None}
        )
    return after_gates(config, slot)


def after_gates(config: EvolveConfig, slot: SlotState) -> SlotState:
    """Every configured gate passed: profile the candidate when the run has a profiler."""
    if config.profiling and slot.candidate is not None:
        return slot.model_copy(
            update={
                "phase": WorkPhase.PROFILE,
                "step": Step.NEEDED,
                "awaiting": None,
                "turn": None,
                "passed": True,
            }
        )
    return finish(config, slot)


def finish(config: EvolveConfig, slot: SlotState) -> SlotState:
    """A candidate that passed everything: export its patch when the selector ranks by code."""
    if config.needs_code and slot.candidate is not None:
        return slot.model_copy(
            update={
                "phase": WorkPhase.EXPORT,
                "step": Step.NEEDED,
                "awaiting": None,
                "passed": True,
            }
        )
    return to_settle(slot, passed=True)


def on_measurement(
    state: EvolveStrategyState, event: MeasurementResult, config: EvolveConfig
) -> EvolveStrategyState:
    """Record the candidate's trusted evidence keys for interpretation.

    A measurement the machinery interrupted says nothing about the candidate, so it is
    submitted again while `may_resubmit` allows it; anything else is final, and its evidence,
    failed or not, goes to interpretation so the mutator's successors read why.
    """
    slot = slot_of(state, event.scope.owner) if event.scope.owner.kind == "attempt" else None
    if (
        slot is None
        or slot.generation != event.scope.generation
        or slot.phase is not WorkPhase.MEASURE
        or slot.step is not Step.AWAITING
        or slot.candidate is None
    ):
        return state
    if may_resubmit(
        event.failure,
        submissions=slot.measurements,
        limit=config.max_input_measurement_attempts,
    ):
        return put(state, slot.model_copy(update={"step": Step.NEEDED, "awaiting": None}))
    evidence = trusted_keys(event, scope=event.scope, candidate=slot.candidate, purpose="official")
    if not evidence:
        # The machinery could not measure it: the child earns no individual.
        reason = f"the candidate could not be measured ({event.status.value})"
        detail = f"{reason}: {event.diagnostic}" if event.diagnostic else reason
        return put(state, to_settle(slot, unmeasured=True, feedback=detail))
    return put(
        state,
        slot.model_copy(
            update={
                "evidence": evidence,
                "phase": WorkPhase.INTERPRET,
                "step": Step.NEEDED,
                "awaiting": None,
            }
        ),
    )


def on_interpreted(
    state: EvolveStrategyState,
    config: EvolveConfig,
    slot: SlotState,
    outcome: EvidenceReadings,
    view: RunView,
) -> EvolveStrategyState:
    """Decode the gates' readings: all passing passes the child, a failing gate is its feedback."""
    readings = accept_readings(outcome.readings, ledger_refs(view, slot.evidence) or ())
    if isinstance(readings, str):
        return fail(state, slot, f"evidence readings refused: {readings}")
    if outcome.status != "succeeded" or not readings:
        return fail(state, slot, f"evidence could not be decoded ({outcome.status})")
    accuracy = reading_of(readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(readings, EvidenceKind.BENCHMARK)
    measured = slot.model_copy(update={"readings": readings, "step": Step.NEEDED, "awaiting": None})
    for reading in (accuracy, benchmark):
        if reading is not None and not reading.passed:
            feedback = reading.feedback or f"{reading.kind.value} gate failed"
            return put(state, to_settle(measured, passed=False, feedback=feedback))
    return put(state, after_gates(config, measured))
