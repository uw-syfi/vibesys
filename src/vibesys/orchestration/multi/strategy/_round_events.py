"""Lifecycle feedback of the open round: admission, operations, failures and settlement.

The replies of the roles are folded in `_turn_events`, the validation and the gates in
`_gates`. This module folds what core and the owners report about the attempt itself, and closes
the round in the ledger once core has settled it.
"""

from vibesys.candidates.api import STOPPED
from vibesys.hypothesis import (
    AgentAttribution,
    JudgeOutcome,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    reviewed,
    rounds,
)
from vibesys.metrics import FrameworkBenchmarkOutcome, MetricRow, reading_of
from vibesys.orchestration.multi.strategy import _context as context
from vibesys.orchestration.multi.strategy import _gates as gates
from vibesys.orchestration.multi.strategy import _ids as ids
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._operations import (
    EvidenceReadings,
    LocalValidationReport,
    RenderedArtifacts,
)
from vibesys.orchestration.multi.strategy._record import awaiting, fail, put
from vibesys.orchestration.multi.strategy._state import (
    MultiStrategyState,
    RoundOutcome,
    RoundState,
    Step,
    WorkPhase,
)
from vibesys.serial_search import api as serial
from vs_core.api import (
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    EvidenceKind,
    IntentBlocked,
    OperationResult,
    Rejected,
    RunView,
    Settlement,
)


def on_rejected(state: MultiStrategyState, event: Rejected) -> MultiStrategyState:
    """A decision core refused fails the round awaiting it."""
    record = awaiting(state, event.decision_id.root)
    if record is None:
        return state
    reason = f"decision rejected: {event.code.value}: {event.detail}"
    if record.phase is WorkPhase.SETTLE and record.settle_sent:
        # Core refused the settlement itself: it will never send `AttemptSettled` and the round
        # cannot ask again, so the round ends here with its typed failure.
        return put(
            state,
            record.failed(reason).model_copy(
                update={"phase": WorkPhase.DONE, "awaiting": None, "step": Step.NEEDED}
            ),
        )
    return fail(state, record, reason)


def on_ready(state: MultiStrategyState, event: AttemptReady) -> MultiStrategyState:
    """The attempt holds a slot: begin its first turn, or settle it if a stop raced."""
    record = state.round
    if (
        record is None
        or record.attempt != event.attempt.attempt_id
        or record.phase is not WorkPhase.START
    ):
        return state
    record = record.admitted(event.attempt.generation)
    if state.stopping:
        record = record.model_copy(
            update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED, "failure": STOPPED}
        )
    else:
        record = record.model_copy(
            update={"phase": WorkPhase.IMPLEMENT, "step": Step.NEEDED, "turn": None}
        )
    return put(state, record)


def on_operation(
    state: MultiStrategyState, view: RunView, config: MultiConfig, event: OperationResult
) -> MultiStrategyState:
    """Route a registered operation's typed outcome to the round that awaits it."""
    record = awaiting(state, ids.decision_of_operation(event.operation_id.root))
    if record is None:
        return state
    outcome = event.outcome
    if isinstance(outcome, RenderedArtifacts):
        if outcome.status != "succeeded" or not outcome.prompts or record.turn is None:
            return fail(state, record, f"prompt render {outcome.status}")
        turn = record.turn.with_prompts(outcome.prompts, outcome.tool_policy)
        return put(
            state, record.model_copy(update={"turn": turn, "step": Step.RENDERED, "awaiting": None})
        )
    if isinstance(outcome, EvidenceReadings):
        return gates.on_interpreted(state, config, record, outcome, view)
    if isinstance(outcome, LocalValidationReport):
        return gates.on_validated(state, config, record, outcome)
    return state


def on_exhausted(state: MultiStrategyState, event: AttemptExhausted) -> MultiStrategyState:
    """A spent budget ends the round; core grants no automatic retry."""
    record = state.round
    if record is None or record.attempt != event.attempt.attempt_id:
        return state
    return fail(state, record, f"attempt budget exhausted ({event.reason})")


def on_blocked(state: MultiStrategyState, event: IntentBlocked) -> MultiStrategyState:
    """A blocked request leaves its effect unknown: end the round awaiting it."""
    detail = f"request blocked: {event.diagnostic}"
    record = awaiting(state, ids.decision_of_operation(event.target.root))
    if (
        record is None
        and state.round is not None
        and event.scope.owner.kind == "attempt"
        and state.round.attempt == event.scope.owner
        and state.round.generation == event.scope.generation
        and state.round.step is Step.AWAITING
        and state.round.phase is not WorkPhase.DONE
    ):
        record = state.round
    return state if record is None else fail(state, record, detail)


def _benchmark_outcome(record: RoundState) -> tuple[FrameworkBenchmarkOutcome, float | None]:
    reading = reading_of(record.readings, EvidenceKind.BENCHMARK)
    headline: MetricRow | None = None if reading is None else reading.headline()
    if reading is None or headline is None:
        return FrameworkBenchmarkOutcome(
            feedback=None if reading is None else reading.feedback
        ), None
    return (
        FrameworkBenchmarkOutcome(
            feedback=reading.feedback,
            metric_name=headline.name,
            metric_value=headline.value,
            metric_direction=headline.direction,
            metric_unit=headline.unit,
            row={row.name: row.value for row in reading.metrics},
        ),
        headline.value,
    )


def judge_outcome(record: RoundState, *, replied: bool) -> JudgeOutcome:
    """The round's review as the ledger records it."""
    if record.judge_pass is not None:
        return JudgeReviewed("pass" if record.judge_pass else "fail")
    if not replied:
        return JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
    if record.review_skipped:
        return JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
    return JudgeSkipped(JudgeSkipReason.NOT_REACHED)


def on_settled(
    state: MultiStrategyState, config: MultiConfig, event: AttemptSettled
) -> MultiStrategyState:
    """Close the round in the ledger and remember what core settled."""
    record = state.round
    settlement = event.settlement
    if (
        record is None
        or record.attempt != settlement.attempt.attempt_id
        or record.phase is WorkPhase.DONE
    ):
        return state
    if record.failure == STOPPED:
        return put(state, record.model_copy(update={"phase": WorkPhase.DONE, "awaiting": None}))
    return _close(state, config, record, settlement)


def retire(state: MultiStrategyState, config: MultiConfig) -> MultiStrategyState:
    """Close a round that is done without core's settlement having closed it.

    Whichever way a round reaches DONE (an attempt core never admitted, a settlement core
    refused), it is closed from its state, not from the event that ended it: it is counted as
    failed in the ledger, so the search moves on instead of asking for the same attempt again. A
    round a stop ended is cleared without counting; the run is ending.
    """
    record = state.round
    if record is None or record.phase is not WorkPhase.DONE:
        return state
    if record.failure == STOPPED:
        return state.model_copy(update={"round": None})
    return _close(state, config, record, None)


def _close(
    state: MultiStrategyState,
    config: MultiConfig,
    record: RoundState,
    settlement: Settlement | None,
) -> MultiStrategyState:
    ledger = context.ledger_of(state, config)
    reply = context.response_of(record)
    benchmark, metric = _benchmark_outcome(record)
    retained = None if settlement is None else settlement.candidate
    commit = None if retained is None else retained.revision_id.root
    passed = record.passed and record.failure is None
    closed = reviewed.close_reviewed_round(
        config.hypotheses,
        ledger,
        reviewed.ReviewedClosing(
            round_number=record.number,
            plan=context.plan_of(record),
            reply=reply,
            judge=judge_outcome(record, replied=reply is not None),
            passed=passed,
            feedback=None
            if passed
            else (record.feedback or record.no_valid_reply or record.failure),
            official_reason=record.official_reason if passed else None,
            benchmark=benchmark,
            benchmark_metric=metric,
            candidate_commit=commit,
            attempts=record.turns_spent,
            accuracy_configured=config.accuracy_configured,
            benchmark_configured=config.benchmark_configured,
            implementer=AgentAttribution("core", None, None),
            revalidation_required=record.revalidation_required,
        ),
    )
    closed = serial.record_round(closed, serial.focus_policy(config.attribution), passed=passed)
    encoded = rounds.encode(closed)
    benchmark_reading = reading_of(record.readings, EvidenceKind.BENCHMARK)
    outcomes = state.outcomes
    if settlement is not None:
        outcomes = (
            *outcomes,
            RoundOutcome(
                round_number=record.number,
                hypothesis_id=record.hypothesis_id,
                settlement=settlement.settlement_id,
                candidate=retained,
                eligible=settlement.eligible,
                metrics=() if benchmark_reading is None else benchmark_reading.metrics,
            ),
        )
    return state.model_copy(
        update={
            "search_json": encoded.search_json,
            "carry_json": encoded.carry_json,
            "closed": state.closed + 1,
            "round": None,
            "checkpoint": retained if retained is not None else state.checkpoint,
            "outcomes": outcomes,
        }
    )
