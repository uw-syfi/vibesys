"""After the judge: the implementer's validation recipes, then the framework's official gates.

A passing review is followed by the trusted run of the validation recipes the implementer wrote
(when it wrote any and the run offers the operation), by approval of the claims it made, and by
the official gates (accuracy, then benchmark) when the cadence says so. A gate that rejects the
candidate marks the hypothesis for revalidation, which shortens the review cadence of the next
attempt. A measurement the machinery could not complete says nothing about the candidate, so it
is submitted again within core's bound and, past it, ends the round without another paid turn.
"""

from vibesys.hypothesis import reviewed, rounds
from vibesys.metrics import accept_readings, reading_of
from vibesys.orchestration.multi.strategy import _context as context
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._operations import (
    VALIDATE_LOCAL_KIND,
    EvidenceReadings,
    LocalValidationReport,
)
from vibesys.orchestration.multi.strategy._prompts import ValidationFailure
from vibesys.orchestration.multi.strategy._record import (
    Repair,
    fail,
    put,
    retry_or_settle,
    to_settle,
)
from vibesys.orchestration.multi.strategy._state import (
    NOTE_LIMIT,
    MultiStrategyState,
    RoundState,
    Step,
    Unreachable,
    UnreachableReason,
    WorkPhase,
)
from vs_core.api import (
    EvidenceKind,
    MeasurementResult,
    RunView,
    ledger_refs,
    may_resubmit,
    trusted_keys,
)


def validation_offered(view: RunView) -> bool:
    """Whether the run offers the trusted run of validation recipes."""
    return any(item.kind == VALIDATE_LOCAL_KIND for item in view.capabilities.operations)


def after_review(
    state: MultiStrategyState, view: RunView, config: MultiConfig, record: RoundState
) -> MultiStrategyState:
    """A passing review: validate the implementer's recipes when it wrote any, else approve."""
    reply = context.response_of(record)
    recipe = None if reply is None else reply.validation_recipe_artifact
    if recipe is None or record.candidate is None:
        return approve(state, config, record)
    if not validation_offered(view):
        missing = Unreachable(
            reason=UnreachableReason.VALIDATE_LOCAL_NOT_OFFERED, subject=record.hypothesis_id
        )
        noted = state.model_copy(update={"unreachable": (*state.unreachable, missing)})
        return approve(noted, config, record)
    return put(
        state,
        record.model_copy(
            update={"phase": WorkPhase.VALIDATE, "step": Step.NEEDED, "awaiting": None}
        ),
    )


def on_validated(
    state: MultiStrategyState,
    config: MultiConfig,
    record: RoundState,
    report: LocalValidationReport,
) -> MultiStrategyState:
    """Fold the recipes' trusted run: a failure is feedback for another attempt."""
    validated = record.model_copy(update={"validated": True, "awaiting": None})
    if report.status != "succeeded":
        # The run could not be validated (nothing to blame on the candidate): approve it on the
        # judge's verdict, as a run that does not offer validation does.
        noted = state.model_copy(
            update={"notes": (*state.notes, f"validation: {report.detail}")[-NOTE_LIMIT:]}
        )
        return approve(noted, config, validated)
    if report.passed:
        return approve(state, config, validated)
    failure = ValidationFailure(
        recipe=report.recipe,
        error=report.error,
        output=report.output,
        recipe_unusable=report.recipe_unusable,
    )
    label = "validation recipe unusable" if report.recipe_unusable else "local validation failed"
    ledger = rounds.note_attempt_failure(config.hypotheses, context.ledger_of(state, config), label)
    noted = state.model_copy(update={"search_json": rounds.encode(ledger).search_json})
    return retry_or_settle(noted, config, validated, Repair(validation_failure=failure))


def approve(
    state: MultiStrategyState, config: MultiConfig, record: RoundState
) -> MultiStrategyState:
    """Approve the reply's claims, then settle or schedule the official gates."""
    reply = context.response_of(record)
    if reply is None:
        return put(state, to_settle(record))
    ledger = context.ledger_of(state, config)
    ledger = reviewed.approve_candidate(config.hypotheses, ledger, reply)
    reason = rounds.official_reason(
        config.hypotheses,
        ledger,
        round_number=record.number,
        requested=context.plan_of(record).request_official_evaluation,
        candidate_ready=reviewed.candidate_ready(reply),
    )
    measurable = config.accuracy_configured or config.benchmark_configured
    if reason is None or not measurable or record.candidate is None:
        encoded = rounds.encode(ledger)
        return state.model_copy(
            update={
                "search_json": encoded.search_json,
                "round": to_settle(record, passed=True, official_reason=None),
            }
        )
    ledger = reviewed.approve_performance(config.hypotheses, ledger, reply)
    return state.model_copy(
        update={
            "search_json": rounds.encode(ledger).search_json,
            "round": record.model_copy(
                update={
                    "official_reason": reason,
                    "phase": WorkPhase.MEASURE,
                    "step": Step.NEEDED,
                    "awaiting": None,
                }
            ),
        }
    )


def on_measurement(
    state: MultiStrategyState, event: MeasurementResult, config: MultiConfig
) -> MultiStrategyState:
    """Record the candidate's trusted evidence keys for interpretation.

    A measurement the machinery interrupted says nothing about the candidate, so it is
    submitted again while `may_resubmit` allows it; anything else is final, and its evidence,
    failed or not, goes to interpretation so the agent reads why.
    """
    record = state.round
    if (
        record is None
        or record.attempt != event.scope.owner
        or record.generation != event.scope.generation
        or record.phase is not WorkPhase.MEASURE
        or record.step is not Step.AWAITING
        or record.candidate is None
    ):
        return state
    if may_resubmit(
        event.failure, submissions=record.measurements, limit=config.max_input_measurement_attempts
    ):
        return put(state, record.model_copy(update={"step": Step.NEEDED, "awaiting": None}))
    evidence = trusted_keys(
        event, scope=event.scope, candidate=record.candidate, purpose="official"
    )
    if not evidence:
        # The machinery could not measure it: the candidate is not charged another attempt
        # and the agent is not asked to repair the infrastructure.
        reason = f"the benchmark could not be measured ({event.status.value})"
        detail = f"{reason}: {event.diagnostic}" if event.diagnostic else reason
        return put(state, to_settle(record, unmeasured=True, feedback=detail))
    return put(
        state,
        record.model_copy(
            update={
                "evidence": evidence,
                "phase": WorkPhase.INTERPRET,
                "step": Step.NEEDED,
                "awaiting": None,
            }
        ),
    )


def on_interpreted(
    state: MultiStrategyState,
    config: MultiConfig,
    record: RoundState,
    outcome: EvidenceReadings,
    view: RunView,
) -> MultiStrategyState:
    """Decode the gates' readings: all passing passes the round, a failing gate is feedback."""
    readings = accept_readings(outcome.readings, ledger_refs(view, record.evidence) or ())
    if isinstance(readings, str):
        return fail(state, record, f"evidence readings refused: {readings}")
    if outcome.status != "succeeded" or not readings:
        return fail(state, record, f"evidence could not be decoded ({outcome.status})")
    accuracy = reading_of(readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(readings, EvidenceKind.BENCHMARK)
    base = record.model_copy(update={"readings": readings, "step": Step.NEEDED, "awaiting": None})
    for reading in (accuracy, benchmark):
        if reading is not None and not reading.passed:
            feedback = reading.feedback or f"{reading.kind.value} gate failed"
            return _gate_failed(state, config, base, feedback)
    return put(state, to_settle(base, passed=True))


def _gate_failed(
    state: MultiStrategyState, config: MultiConfig, record: RoundState, feedback: str
) -> MultiStrategyState:
    """A trusted gate rejected the candidate: another attempt may repair it, or the round ends."""
    ledger = context.ledger_of(state, config)
    accuracy = reading_of(record.readings, EvidenceKind.CORRECTNESS)
    noted = rounds.note_attempt_failure(
        config.hypotheses,
        ledger,
        feedback,
        rounds.GateFailure(
            None if record.candidate is None else record.candidate.revision_id.root,
            accuracy_passed=accuracy is not None and accuracy.passed,
        ),
    )
    state = state.model_copy(update={"search_json": rounds.encode(noted).search_json})
    failed = record.model_copy(
        update={"gate_failed": True, "passed": False, "revalidation_required": True}
    )
    return retry_or_settle(state, config, failed, Repair(feedback=feedback))
