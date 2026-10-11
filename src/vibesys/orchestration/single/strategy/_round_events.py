"""Event handlers of the open round: fold each core feedback into its record.

Handlers never propose decisions; they move the record to the phase and step whose decision
`_round.advance` proposes next. Duplicate or late events find no matching awaiting record and
leave the state unchanged.
"""

from pydantic import TypeAdapter, ValidationError

from vibesys.candidates.api import STOPPED
from vibesys.hypothesis import AgentAttribution, rounds
from vibesys.metrics import FrameworkBenchmarkOutcome, MetricRow, accept_readings, reading_of
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.single.strategy import _context as context
from vibesys.orchestration.single.strategy import _ids as ids
from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.orchestration.single.strategy._draft import invocation_for
from vibesys.orchestration.single.strategy._operations import EvidenceReadings, RenderedArtifacts
from vibesys.orchestration.single.strategy._prompts import (
    ReplyCorrectionPrompt,
    ResumePrompt,
    WaitUnrecordedPrompt,
)
from vibesys.orchestration.single.strategy._round import subject_of
from vibesys.orchestration.single.strategy._state import (
    Role,
    RoundOutcome,
    RoundState,
    SingleStrategyState,
    Step,
    TurnRecord,
    Unreachable,
    UnreachableReason,
    WorkPhase,
)
from vibesys.serial_search import api as serial
from vs_core.api import (
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    EvidenceKind,
    IntentBlocked,
    InvocationRef,
    MeasurementResult,
    ObservationStatus,
    OperationResult,
    Rejected,
    ResumeAuthorized,
    RevisionRef,
    RunView,
    TurnFailureKind,
    TurnResult,
    TurnSuspended,
    Waitable,
    WaitingForEvaluation,
    ledger_refs,
    may_resubmit,
    trusted_keys,
    turn_candidate,
)

_REPLY = TypeAdapter(Waitable[SingleAgentRoundResponse])


def _put(state: SingleStrategyState, record: RoundState) -> SingleStrategyState:
    return state.model_copy(update={"round": record})


def _awaiting(state: SingleStrategyState, decision: str | None) -> RoundState | None:
    record = state.round
    if record is not None and record.phase is not WorkPhase.DONE and record.awaits(decision):
        return record
    return None


def fail(state: SingleStrategyState, record: RoundState, reason: str) -> SingleStrategyState:
    """End the round with a typed failure; an admitted attempt must still settle."""
    if record.phase is WorkPhase.DONE:
        return state
    failed = record.failed(reason)
    if failed.owes_settlement:
        return _put(state, failed.model_copy(update={"phase": WorkPhase.SETTLE}))
    # An attempt core never admitted has nothing to settle: its slot is refunded and the
    # round is not counted, so the designer's hypothesis is asked again from the ledger.
    return _put(state, failed.model_copy(update={"phase": WorkPhase.DONE}))


def on_rejected(state: SingleStrategyState, event: Rejected) -> SingleStrategyState:
    """A decision core refused fails the round awaiting it."""
    record = _awaiting(state, event.decision_id.root)
    if record is None:
        return state
    return fail(state, record, f"decision rejected: {event.code.value}: {event.detail}")


def on_ready(state: SingleStrategyState, event: AttemptReady) -> SingleStrategyState:
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
    return _put(state, record)


def on_operation(
    state: SingleStrategyState, view: RunView, config: SingleConfig, event: OperationResult
) -> SingleStrategyState:
    """Route a registered operation's typed outcome to the round that awaits it."""
    record = _awaiting(state, ids.decision_of_operation(event.operation_id.root))
    if record is None:
        return state
    outcome = event.outcome
    if isinstance(outcome, RenderedArtifacts):
        if outcome.status != "succeeded" or not outcome.prompts or record.turn is None:
            return fail(state, record, f"prompt render {outcome.status}")
        turn = record.turn.with_prompts(outcome.prompts, outcome.tool_policy)
        return _put(
            state, record.model_copy(update={"turn": turn, "step": Step.RENDERED, "awaiting": None})
        )
    if isinstance(outcome, EvidenceReadings):
        return _interpreted(state, config, record, outcome, view)
    return state


def on_measurement(
    state: SingleStrategyState, event: MeasurementResult, config: SingleConfig
) -> SingleStrategyState:
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
        return _put(state, record.model_copy(update={"step": Step.NEEDED, "awaiting": None}))
    evidence = trusted_keys(
        event, scope=event.scope, candidate=record.candidate, purpose="official"
    )
    if not evidence:
        # The machinery could not measure it: the candidate is not charged another attempt
        # and the agent is not asked to repair the infrastructure.
        reason = f"the benchmark could not be measured ({event.status.value})"
        detail = f"{reason}: {event.diagnostic}" if event.diagnostic else reason
        return _put(
            state,
            record.model_copy(
                update={
                    "phase": WorkPhase.SETTLE,
                    "step": Step.NEEDED,
                    "awaiting": None,
                    "unmeasured": True,
                    "feedback": detail,
                }
            ),
        )
    return _put(
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


def _interpreted(
    state: SingleStrategyState,
    config: SingleConfig,
    record: RoundState,
    outcome: EvidenceReadings,
    view: RunView,
) -> SingleStrategyState:
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
            return _gate_failed(
                state, config, base, reading.feedback or f"{reading.kind.value} gate failed"
            )
    return _put(state, base.model_copy(update={"passed": True, "phase": WorkPhase.SETTLE}))


def _gate_failed(
    state: SingleStrategyState, config: SingleConfig, record: RoundState, feedback: str
) -> SingleStrategyState:
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
    record = record.model_copy(update={"gate_failed": True, "passed": False})
    return _fail_attempt(state, config, record, feedback)


def _next_turn(record: RoundState, update: dict[str, object]) -> RoundState:
    turn = record.turn
    return record.model_copy(
        update={
            **update,
            "phase": WorkPhase.IMPLEMENT,
            "step": Step.NEEDED,
            "awaiting": None,
            "evidence": (),
            "turn": TurnRecord(
                role=Role.IMPLEMENTER, serial=0 if turn is None else turn.serial + 1, charge="paid"
            ),
        }
    )


def _candidate_of(
    view: RunView, record: RoundState, invocation: InvocationRef
) -> RevisionRef | None:
    """The revision the reply's turn retained; a resumed turn's is the chain's latest checkpoint.

    A turn that suspended to wait for its evaluations is checkpointed under its own
    invocation. The turn core resumes edits nothing more when it only reads the verdicts, so it
    retains nothing of its own: the candidate is what the chain checkpointed before it waited.
    """
    own = turn_candidate(view, record.attempt, invocation)
    if own is not None or record.turn is None or record.turn.charge != "resume":
        return own
    live = next((item for item in view.attempts if item.attempt_id == record.attempt), None)
    if live is None or not live.checkpoints:
        return None
    return live.checkpoints[-1].revision


def settle_retained(
    state: SingleStrategyState, view: RunView, config: SingleConfig
) -> SingleStrategyState:
    """Fold a held implementer reply once core has retained or declined its checkpoint."""
    record = state.round
    if record is None or not record.holds_reply:
        return state
    invocation = record.held_invocation
    if invocation is None or record.held_reply is None:
        return state
    live = next((item for item in view.attempts if item.attempt_id == record.attempt), None)
    candidate = _candidate_of(view, record, invocation)
    declined = live is not None and any(
        row.invocation == invocation for row in live.checkpoint_declines
    )
    if candidate is None and not declined:
        return state
    reply = _decode(record.held_reply)
    released = state.model_copy(update={"round": record.released()})
    if isinstance(reply, SingleAgentRoundResponse):
        return _replied(released, config, record.released(), reply, candidate)
    return released


def _decode(payload: str) -> SingleAgentRoundResponse | WaitingForEvaluation | str:
    try:
        return _REPLY.validate_python(_REPLY.validate_json(payload))
    except ValidationError as error:
        return str(error)


def owns_turn(state: SingleStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers the round's outstanding turn."""
    record = state.round
    return (
        record is not None
        and record.step is Step.AWAITING
        and record.phase is WorkPhase.IMPLEMENT
        and record.turn is not None
        and event.invocation.invocation_id.root
        == invocation_for(Role.IMPLEMENTER, subject_of(record), record.turn)
    )


def on_turn(
    state: SingleStrategyState, view: RunView, config: SingleConfig, event: TurnResult
) -> SingleStrategyState:
    """Interpret the combined reply: continue, correct, retry, suspend or fail."""
    record = state.round
    if record is None or record.turn is None:
        return state
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        return _unfinished(state, view, config, record, event)
    reply = _decode(event.output_json)
    if isinstance(reply, str):
        return _corrected(
            state, config, record, event, ReplyCorrectionPrompt(role="implementer", error=reply)
        )
    if isinstance(reply, WaitingForEvaluation):
        return _waiting(state, view, config, record, event)
    candidate = _candidate_of(view, record, event.invocation)
    if candidate is None:
        return _put(state, record.holding(event.output_json, event.invocation))
    return _replied(state, config, record, reply, candidate)


def _corrected(
    state: SingleStrategyState,
    config: SingleConfig,
    record: RoundState,
    event: TurnResult,
    correction: ReplyCorrectionPrompt | WaitUnrecordedPrompt,
) -> SingleStrategyState:
    """Ask the role to fix its reply; the correction names the invalid turn as its predecessor."""
    turn = record.turn
    corrected = (
        None
        if turn is None
        else turn.corrected(event.invocation, correction, limit=config.max_corrections)
    )
    if corrected is None:
        if isinstance(correction, ReplyCorrectionPrompt):
            return _no_reply(state, config, record, f"invalid reply: {correction.error}")
        return fail(state, record, "the turn asked to wait and the run recorded no wait")
    return _put(
        state, record.model_copy(update={"turn": corrected, "step": Step.NEEDED, "awaiting": None})
    )


def _waiting(
    state: SingleStrategyState,
    view: RunView,
    config: SingleConfig,
    record: RoundState,
    event: TurnResult,
) -> SingleStrategyState:
    """The reply asked to wait: only core's `TurnSuspended` completes the yield."""
    if not event.suspending:
        # Core recorded no wait (it refused it, or the host lost it), so no TurnSuspended will
        # come: waiting for one would stall the run.
        return _corrected(state, config, record, event, WaitUnrecordedPrompt())
    if "suspend" not in view.capabilities.lifecycle:
        failed = fail(state, record, "the run offers no suspend capability")
        missing = Unreachable(
            reason=UnreachableReason.RESUME_CAPABILITY_NOT_OFFERED, subject=record.hypothesis_id
        )
        return failed.model_copy(update={"unreachable": (*failed.unreachable, missing)})
    turn = record.turn
    if turn is None:
        return state
    return _put(state, record.model_copy(update={"turn": turn.yielding()}))


def _unfinished(
    state: SingleStrategyState,
    view: RunView,
    config: SingleConfig,
    record: RoundState,
    event: TurnResult,
) -> SingleStrategyState:
    """A turn ended without a usable reply: ask again if lost, else it is a failed attempt."""
    turn = record.turn
    if turn is None:
        return state
    if event.failure is TurnFailureKind.TRANSPORT_LOST:
        again = turn.asked_again(
            event.invocation,
            now=view.run.now_at,
            limit=config.max_turn_drops,
            backoff=config.drop_backoff(turn.drops),
        )
        if again is None:
            return fail(state, record, f"implementer turn was lost ({event.detail or 'transport'})")
        return _put(
            state, record.model_copy(update={"turn": again, "step": Step.NEEDED, "awaiting": None})
        )
    return _no_reply(
        state,
        config,
        record,
        event.detail or f"implementer turn did not complete ({event.observation.status.value})",
    )


def _no_reply(
    state: SingleStrategyState, config: SingleConfig, record: RoundState, reason: str
) -> SingleStrategyState:
    """The attempt produced no valid response: retry within the paid budget or end the round."""
    record = record.model_copy(
        update={"response_json": None, "no_valid_reply": reason, "verdict_pass": None}
    )
    if record.turns_spent >= config.max_retries_per_round:
        return _put(
            state,
            record.model_copy(
                update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED, "awaiting": None}
            ),
        )
    return _put(state, _next_turn(record, {"feedback": None}))


def _replied(
    state: SingleStrategyState,
    config: SingleConfig,
    record: RoundState,
    response: SingleAgentRoundResponse,
    candidate: object,
) -> SingleStrategyState:
    """Apply the Pareto guard to a valid reply, then gate, retry or close the attempt."""
    ledger = context.ledger_of(state, config)
    conflict = rounds.pareto_conflict(config.hypotheses, ledger, response)
    verdict_pass = response.verdict is Verdict.PASS and conflict is None
    guarded = response
    dominated: tuple[int, ...] = ()
    if conflict is not None and response.verdict is Verdict.PASS:
        guarded = response.model_copy(update={"verdict": Verdict.FAIL})
        dominated = tuple(item.round_number for item in conflict.dominators)
    record = record.model_copy(
        update={
            "response_json": guarded.model_dump_json(),
            "candidate": candidate or record.candidate,
            "verdict_pass": verdict_pass,
            "no_valid_reply": None,
            "dominated_by_rounds": dominated,
            "awaiting": None,
        }
    )
    if not verdict_pass:
        feedback = response.feedback or "self-review failed"
        state = state.model_copy(
            update={
                "search_json": rounds.encode(
                    rounds.note_attempt_failure(config.hypotheses, ledger, feedback)
                ).search_json
            }
        )
        return _fail_attempt(state, config, record, feedback)
    if record.candidate is None:
        return _fail_attempt(
            state,
            config,
            record.model_copy(
                update={"no_valid_reply": "the implementer retained no changed candidate"}
            ),
            "retained no changed candidate",
        )
    plan = context.plan_of(record)
    reason = rounds.official_reason(
        config.hypotheses,
        ledger,
        round_number=record.number,
        requested=plan.request_official_evaluation,
    )
    if reason is None or not (config.accuracy_configured or config.benchmark_configured):
        return _put(
            state,
            record.model_copy(
                update={
                    "passed": True,
                    "official_reason": None,
                    "phase": WorkPhase.SETTLE,
                    "step": Step.NEEDED,
                }
            ),
        )
    return _put(
        state,
        record.model_copy(
            update={
                "official_reason": reason,
                "phase": WorkPhase.MEASURE,
                "step": Step.NEEDED,
                "turn": None,
            }
        ),
    )


def _fail_attempt(
    state: SingleStrategyState, config: SingleConfig, record: RoundState, feedback: str
) -> SingleStrategyState:
    if record.turns_spent >= config.max_retries_per_round:
        return _put(
            state,
            record.model_copy(
                update={
                    "feedback": feedback,
                    "phase": WorkPhase.SETTLE,
                    "step": Step.NEEDED,
                    "awaiting": None,
                }
            ),
        )
    return _put(state, _next_turn(record, {"feedback": feedback}))


def on_suspended(state: SingleStrategyState, event: TurnSuspended) -> SingleStrategyState:
    """Core recorded the continuation: wait for `ResumeAuthorized` of exactly that ID."""
    record = state.round
    continuation = event.continuation
    if record is None or record.turn is None or record.step is not Step.AWAITING:
        return state
    if continuation.invocation.invocation_id.root != invocation_for(
        Role.IMPLEMENTER, subject_of(record), record.turn
    ):
        return state
    turn = record.turn.suspended(continuation.invocation, continuation.continuation_id)
    return _put(
        state, record.model_copy(update={"turn": turn, "step": Step.SUSPENDED, "awaiting": None})
    )


def on_resume(state: SingleStrategyState, event: ResumeAuthorized) -> SingleStrategyState:
    """Authorize the suspended turn's resume with the invocation core named."""
    record = state.round
    if (
        record is None
        or record.step is not Step.SUSPENDED
        or record.turn is None
        or record.turn.continuation != event.continuation_id
    ):
        return state
    resumed = record.turn.resumed(
        authorized=event.next_invocation.invocation_id,
        continuation=event.continuation_id,
        context=ResumePrompt(
            role="implementer",
            retained_revision=record.candidate,
            timed_out=event.timeout is not None,
            evidence=tuple(item.evidence_id for item in event.evidence),
            repeated_failure=None
            if event.repeated_failure is None
            else str(event.repeated_failure),
        ),
    )
    return _put(state, record.model_copy(update={"turn": resumed, "step": Step.NEEDED}))


def on_exhausted(state: SingleStrategyState, event: AttemptExhausted) -> SingleStrategyState:
    """A spent budget ends the round; core grants no automatic retry."""
    record = state.round
    if record is None or record.attempt != event.attempt.attempt_id:
        return state
    return fail(state, record, f"attempt budget exhausted ({event.reason})")


def on_blocked(state: SingleStrategyState, event: IntentBlocked) -> SingleStrategyState:
    """A blocked request leaves its effect unknown: end the round awaiting it."""
    detail = f"request blocked: {event.diagnostic}"
    record = _awaiting(state, ids.decision_of_operation(event.target.root))
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


def on_settled(
    state: SingleStrategyState, config: SingleConfig, event: AttemptSettled
) -> SingleStrategyState:
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
        return _put(state, record.model_copy(update={"phase": WorkPhase.DONE, "awaiting": None}))
    ledger = context.ledger_of(state, config)
    response = context.response_of(record)
    benchmark, metric = _benchmark_outcome(record)
    commit = None if settlement.candidate is None else settlement.candidate.revision_id.root
    passed = record.passed and record.failure is None
    official = passed and record.official_reason is not None
    if response is not None and official and metric is not None:
        response = response.model_copy(
            update={"perf_metric": metric, "perf_unit": benchmark.metric_name}
        )
    closed = rounds.close_round(
        config.hypotheses,
        ledger,
        rounds.ClosingFacts(
            round_number=record.number,
            plan=context.plan_of(record),
            reply=response,
            passed=passed,
            feedback=None if passed else (record.feedback or record.failure),
            official_reason=record.official_reason if passed else None,
            benchmark=benchmark,
            benchmark_metric=metric,
            candidate_commit=commit,
            attempts=record.turns_spent,
            accuracy_configured=config.accuracy_configured,
            benchmark_configured=config.benchmark_configured,
            implementer=AgentAttribution("core", None, None),
            revalidation_required=record.gate_failed,
            verdict_pass=record.verdict_pass,
        ),
    )
    closed = serial.record_round(closed, serial.focus_policy(config.attribution), passed=passed)
    encoded = rounds.encode(closed)
    benchmark_reading = reading_of(record.readings, EvidenceKind.BENCHMARK)
    retained = settlement.candidate
    return state.model_copy(
        update={
            "search_json": encoded.search_json,
            "carry_json": encoded.carry_json,
            "closed": state.closed + 1,
            "round": None,
            "checkpoint": retained if retained is not None else state.checkpoint,
            "last_profile": context.last_profile_of(response) or state.last_profile,
            "outcomes": (
                *state.outcomes,
                RoundOutcome(
                    round_number=record.number,
                    hypothesis_id=record.hypothesis_id,
                    settlement=settlement.settlement_id,
                    candidate=retained,
                    eligible=settlement.eligible,
                    metrics=() if benchmark_reading is None else benchmark_reading.metrics,
                ),
            ),
        }
    )
