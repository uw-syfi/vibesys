"""The roles' replies: fold each turn's answer into the slot that asked.

Every role answers through the same protocol: a reply that violates its schema is corrected, a turn
the transport lost is asked again, a reply that waits for evaluations suspends and later resumes.
The mutator's reply is held until core has retained (or declined) the checkpoint it made, so the
candidate the judge reads is known. A mutation is never retried: a reply that stays invalid, or a
candidate the judge rejects, is a failed individual. The profiler is advisory: a profile that
fails is a note and the candidate keeps its other evidence.
"""

from typing import Literal

from pydantic import TypeAdapter, ValidationError

from vibesys.orchestration.evolve.models import JudgeResponse, MutatorResponse
from vibesys.orchestration.evolve.strategy import _gates as gates
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._draft import invocation_for
from vibesys.orchestration.evolve.strategy._prompts import (
    ReplyCorrectionPrompt,
    ResumePrompt,
    WaitUnrecordedPrompt,
)
from vibesys.orchestration.evolve.strategy._record import fail, put, to_settle
from vibesys.orchestration.evolve.strategy._slots import role_of, subject_of
from vibesys.orchestration.evolve.strategy._state import (
    NOTE_LIMIT,
    EvolveStrategyState,
    ProfileReading,
    Role,
    SlotState,
    Unreachable,
    UnreachableReason,
    WorkPhase,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vs_core.api import (
    InvocationRef,
    ObservationStatus,
    ResumeAuthorized,
    RevisionRef,
    RunView,
    Step,
    TurnFailureKind,
    TurnResult,
    TurnSuspended,
    Waitable,
    WaitingForEvaluation,
)

_MUTATOR = TypeAdapter(Waitable[MutatorResponse])
_JUDGE = TypeAdapter(Waitable[JudgeResponse])
_PROFILER = TypeAdapter(ProfilerSummary)
# The shared protocol templates name the mutator's role by what it does for a candidate.
_PROTOCOL_ROLE: dict[Role, Literal["implementer", "judge", "profiler"]] = {
    Role.MUTATOR: "implementer",
    Role.JUDGE: "judge",
    Role.PROFILER: "profiler",
}


def _decode(
    role: Role, payload: str
) -> MutatorResponse | JudgeResponse | ProfilerSummary | WaitingForEvaluation | str:
    adapters = {Role.MUTATOR: _MUTATOR, Role.JUDGE: _JUDGE, Role.PROFILER: _PROFILER}
    adapter = adapters[role]
    try:
        return adapter.validate_python(adapter.validate_json(payload))
    except ValidationError as error:
        return str(error)


def candidate_of(view: RunView, slot: SlotState, invocation: InvocationRef) -> RevisionRef | None:
    """The revision the reply's turn chain retained, by core's one rule (`TurnChain.candidate`)."""
    if slot.turn is None:
        return None
    return slot.turn.candidate(view, slot.attempt, invocation)


def turn_slot(state: EvolveStrategyState, event: TurnResult) -> SlotState | None:
    """The slot whose outstanding turn ``event`` answers."""
    for slot in state.slots:
        role = role_of(slot.phase)
        if (
            role is not None
            and slot.step is Step.AWAITING
            and slot.turn is not None
            and event.invocation.invocation_id.root
            == invocation_for(role, subject_of(slot, role), slot.turn)
        ):
            return slot
    return None


def on_turn(
    state: EvolveStrategyState,
    view: RunView,
    config: EvolveConfig,
    slot: SlotState,
    event: TurnResult,
) -> EvolveStrategyState:
    """Interpret a reply: continue, correct, suspend or fail."""
    if slot.turn is None or role_of(slot.phase) is None:
        return state
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        return _unfinished(state, view, config, slot, event)
    return _answered(state, view, config, slot, event)


def _answered(
    state: EvolveStrategyState,
    view: RunView,
    config: EvolveConfig,
    slot: SlotState,
    event: TurnResult,
) -> EvolveStrategyState:
    """Act on the reply of a turn that completed with output."""
    role = role_of(slot.phase)
    payload = event.output_json
    if role is None or payload is None:
        return state
    reply = _decode(role, payload)
    if isinstance(reply, str):
        correction = ReplyCorrectionPrompt(role=_PROTOCOL_ROLE[role], error=reply)
        return _corrected(state, config, slot, event, correction)
    if isinstance(reply, WaitingForEvaluation):
        return _waiting(state, view, config, slot, event)
    if isinstance(reply, JudgeResponse):
        judged = slot.model_copy(update={"judge_invocation": event.invocation})
        return _judged(state, config, judged, reply)
    if isinstance(reply, ProfilerSummary):
        return _profiled(state, slot, reply)
    candidate = candidate_of(view, slot, event.invocation)
    if candidate is None:
        return put(state, slot.holding(payload, event.invocation))
    return _mutated(state, slot, reply, candidate)


def settle_retained(state: EvolveStrategyState, view: RunView) -> EvolveStrategyState:
    """Fold a held mutator reply once core has retained or declined its checkpoint."""
    for slot in state.slots:
        if not slot.holds_reply or slot.held_invocation is None or slot.held_reply is None:
            continue
        invocation = slot.held_invocation
        live = next((item for item in view.attempts if item.attempt_id == slot.attempt), None)
        candidate = candidate_of(view, slot, invocation)
        declined = live is not None and any(
            row.invocation == invocation for row in live.checkpoint_declines
        )
        if candidate is None and not declined:
            continue
        reply = _decode(Role.MUTATOR, slot.held_reply)
        released = slot.released()
        if isinstance(reply, MutatorResponse):
            state = _mutated(put(state, released), released, reply, candidate)
        else:
            state = put(state, released)
    return state


def _corrected(
    state: EvolveStrategyState,
    config: EvolveConfig,
    slot: SlotState,
    event: TurnResult,
    correction: ReplyCorrectionPrompt | WaitUnrecordedPrompt,
) -> EvolveStrategyState:
    """Ask the role to fix its reply; the correction names the invalid turn as its predecessor."""
    turn = slot.turn
    corrected = (
        None
        if turn is None
        else turn.corrected(event.invocation, correction, limit=config.max_corrections)
    )
    if corrected is None:
        if isinstance(correction, ReplyCorrectionPrompt):
            return _no_answer(state, slot, f"invalid reply: {correction.error}")
        return fail(state, slot, "the turn asked to wait and the run recorded no wait")
    return put(
        state, slot.model_copy(update={"turn": corrected, "step": Step.NEEDED, "awaiting": None})
    )


def _waiting(
    state: EvolveStrategyState,
    view: RunView,
    config: EvolveConfig,
    slot: SlotState,
    event: TurnResult,
) -> EvolveStrategyState:
    """The reply asked to wait: only core's `TurnSuspended` completes the yield."""
    if not event.suspending:
        # Core recorded no wait (it refused it, or the host lost it), so no TurnSuspended will
        # come: waiting for one would stall the run.
        return _corrected(state, config, slot, event, WaitUnrecordedPrompt())
    if "suspend" not in view.capabilities.lifecycle:
        failed = fail(state, slot, "the run offers no suspend capability")
        missing = Unreachable(reason=UnreachableReason.RESUME_CAPABILITY_NOT_OFFERED, subject=slot.key)
        return failed.model_copy(update={"unreachable": (*failed.unreachable, missing)})
    turn = slot.turn
    if turn is None:
        return state
    return put(state, slot.model_copy(update={"turn": turn.yielding()}))


def _unfinished(
    state: EvolveStrategyState,
    view: RunView,
    config: EvolveConfig,
    slot: SlotState,
    event: TurnResult,
) -> EvolveStrategyState:
    """A turn ended without a usable reply: ask again if lost, else it is a failed answer."""
    turn = slot.turn
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
            return _no_answer(state, slot, f"the turn was lost ({event.detail or 'transport'})")
        return put(
            state, slot.model_copy(update={"turn": again, "step": Step.NEEDED, "awaiting": None})
        )
    detail = event.detail or f"the turn did not complete ({event.observation.status.value})"
    return _no_answer(state, slot, detail)


def _no_answer(state: EvolveStrategyState, slot: SlotState, reason: str) -> EvolveStrategyState:
    """A role produced no valid answer: a failed individual, except for the advisory profiler."""
    role = role_of(slot.phase)
    if slot.phase is WorkPhase.PROFILE:
        noted = state.model_copy(
            update={"notes": (*state.notes, f"profiler: {reason}")[-NOTE_LIMIT:]}
        )
        return put(noted, to_settle(slot, passed=True))
    name = "mutator" if role is Role.MUTATOR else "judge"
    feedback = f"framework: the {name} returned no valid response ({reason})"
    return put(state, to_settle(slot, feedback=feedback, judge_pass=None, judge_invocation=None))


def _mutated(
    state: EvolveStrategyState,
    slot: SlotState,
    reply: MutatorResponse,
    candidate: RevisionRef | None,
) -> EvolveStrategyState:
    """Accept a valid mutator reply: review the candidate it left."""
    if candidate is None:
        # Nothing to review or measure: the mutation changed nothing the run retained.
        return put(
            state,
            to_settle(
                slot,
                summary=reply.summary,
                feedback="framework: the mutator retained no changed candidate",
            ),
        )
    return put(
        state,
        slot.model_copy(
            update={
                "summary": reply.summary,
                "candidate": candidate,
                "phase": WorkPhase.JUDGE,
                "step": Step.NEEDED,
                "awaiting": None,
                "turn": None,
            }
        ),
    )


def _judged(
    state: EvolveStrategyState, config: EvolveConfig, slot: SlotState, verdict: JudgeResponse
) -> EvolveStrategyState:
    """A verdict: a pass goes on to the gates, a fail is the individual's feedback."""
    if verdict.verdict is Verdict.PASS:
        passing = slot.model_copy(
            update={"judge_pass": True, "feedback": verdict.feedback, "awaiting": None}
        )
        return put(state, gates.after_review(config, passing))
    feedback = verdict.feedback or "review failed"
    return put(state, to_settle(slot, judge_pass=False, feedback=feedback))


def _profiled(
    state: EvolveStrategyState, slot: SlotState, summary: ProfilerSummary
) -> EvolveStrategyState:
    """Keep the profiler's self-reported fitness; it settles the candidate that passed."""
    reading = ProfileReading(
        perf_metric=summary.perf_metric,
        perf_unit=summary.perf_unit,
        metrics=tuple(summary.metrics.items()),
    )
    return put(state, to_settle(slot, profile=reading, passed=True))


def on_suspended(state: EvolveStrategyState, event: TurnSuspended) -> EvolveStrategyState:
    """Core recorded the continuation: wait for `ResumeAuthorized` of exactly that ID."""
    continuation = event.continuation
    for slot in state.slots:
        role = role_of(slot.phase)
        if role is None or slot.turn is None or slot.step is not Step.AWAITING:
            continue
        if continuation.invocation.invocation_id.root != invocation_for(
            role, subject_of(slot, role), slot.turn
        ):
            continue
        turn = slot.turn.suspended(continuation.invocation, continuation.continuation_id)
        return put(
            state,
            slot.model_copy(update={"turn": turn, "step": Step.SUSPENDED, "awaiting": None}),
        )
    return state


def on_resume(state: EvolveStrategyState, event: ResumeAuthorized) -> EvolveStrategyState:
    """Authorize the suspended turn's resume with the invocation core named."""
    for slot in state.slots:
        role = role_of(slot.phase)
        if (
            role is None
            or slot.step is not Step.SUSPENDED
            or slot.turn is None
            or slot.turn.continuation != event.continuation_id
        ):
            continue
        resumed = slot.turn.resumed(
            authorized=event.next_invocation.invocation_id,
            continuation=event.continuation_id,
            context=ResumePrompt(
                role=_PROTOCOL_ROLE[role],
                retained_revision=slot.candidate,
                timed_out=event.timeout is not None,
                evidence=tuple(item.evidence_id for item in event.evidence),
                repeated_failure=None
                if event.repeated_failure is None
                else str(event.repeated_failure),
            ),
        )
        return put(state, slot.model_copy(update={"turn": resumed, "step": Step.NEEDED}))
    return state
