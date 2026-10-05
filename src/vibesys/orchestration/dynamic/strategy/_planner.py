"""Planner turns: ask for a portfolio, correct it once, schedule what is valid.

The planner is a run-scoped, free turn. Its reply is parsed against the exact
parents offered, validated into a `PlanCheck`, and corrected up to
`max_corrections` times. When the correction does not fix the plan, the strategy
keeps the best valid outcome: a held underfilled plan, else the valid part of the
last reply, else a typed failure when nothing is in flight to wait for.
"""

from pydantic import ValidationError

from vibesys.orchestration.dynamic.models import PortfolioPlan, planner_response_type
from vibesys.orchestration.dynamic.strategy import _context as context
from vibesys.orchestration.dynamic.strategy import _ids as ids
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._draft import (
    Draft,
    TurnShape,
    invocation_for,
    operation,
    render_id,
    render_request,
    request_turn,
    run_scope,
    turn_id,
)
from vibesys.orchestration.dynamic.strategy._operations import RenderedArtifacts
from vibesys.orchestration.dynamic.strategy._parents import resolve
from vibesys.orchestration.dynamic.strategy._plan import PlanCheck, validate
from vibesys.orchestration.dynamic.strategy._prompts import PlannerCorrectionPrompt
from vibesys.orchestration.dynamic.strategy._schemas import PLANNER_REPLY
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    DynamicStrategyState,
    HypothesisRecord,
    PlannerState,
    Role,
    RunPhase,
    Step,
    TurnRecord,
    WorkKind,
    WorkPhase,
    WorkPlan,
)
from vs_core.api import Access, ObservationStatus, RevisionRef, RunView, TurnResult

SUBJECT = "run"


def _done(state: DynamicStrategyState) -> int:
    return sum(item.phase is WorkPhase.DONE for item in state.attempts)


def _may_plan(state: DynamicStrategyState, config: DynamicConfig) -> bool:
    planner = state.planner
    if state.phase is not RunPhase.SEARCHING or state.stopping or planner.failed:
        return False
    if not context.baseline_resolved(state) or context.capacity(state, config) == 0:
        return False
    return planner.blocked_at_done is None or _done(state) > planner.blocked_at_done


def decide(draft: Draft) -> None:
    """Begin a planning call when slots are free, then render and request its turn."""
    planner = draft.state.planner
    if not planner.active and not _may_plan(draft.state, draft.config):
        return
    if not planner.active:
        serial = 0 if planner.turn is None else planner.turn.serial + 1
        planner = planner.model_copy(
            update={
                "active": True,
                "step": Step.NEEDED,
                "blocked_at_done": None,
                "turn": TurnRecord(role=Role.PLANNER, serial=serial, charge="free"),
            }
        )
    turn = planner.turn
    if turn is None:
        return
    if planner.step is Step.NEEDED:
        prompt = context.planner_prompt(draft.state, draft.config, draft.view)
        body = (
            prompt
            if planner.last_error is None
            else PlannerCorrectionPrompt(planner=prompt, error=planner.last_error, scheduled=0)
        )
        identifier = render_id(SUBJECT, turn)
        draft.emit(
            operation(draft, identifier, run_scope(draft.view), render_request(SUBJECT, turn, body))
        )
        planner = planner.model_copy(update={"step": Step.RENDERING, "awaiting": identifier})
    elif planner.step is Step.RENDERED:
        scope = run_scope(draft.view)
        draft.emit(
            request_turn(
                draft,
                TurnShape(
                    role=Role.PLANNER,
                    subject=SUBJECT,
                    workspace=scope,
                    access=Access.READ_ONLY,
                    output_schema=PLANNER_REPLY,
                    seconds=draft.config.planner_turn_seconds,
                ),
                turn,
                scope,
            )
        )
        planner = planner.model_copy(
            update={"step": Step.AWAITING, "awaiting": turn_id(SUBJECT, turn)}
        )
    draft.update(planner=planner)


def owns_turn(state: DynamicStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers the planner's outstanding turn."""
    planner = state.planner
    return (
        planner.step is Step.AWAITING
        and planner.turn is not None
        and event.invocation.invocation_id.root
        == invocation_for(Role.PLANNER, SUBJECT, planner.turn)
    )


def _parse(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig, event: TurnResult
) -> tuple[PortfolioPlan | None, str]:
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        return None, f"the planner turn did not complete ({event.observation.status.value})"
    revisions = tuple(
        item.snapshot.revision.revision_id.root for item in context.offered(state, view)
    )
    try:
        return planner_response_type(revisions, profiling=config.profiling).model_validate_json(
            event.output_json
        ), ""
    except ValidationError as error:
        return None, str(error)


def _check_json(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig, reply: str
) -> PlanCheck | None:
    try:
        plan = PortfolioPlan.model_validate_json(reply)
    except ValidationError:
        return None
    return validate(
        plan,
        state,
        context.offered(state, view),
        capacity=context.capacity(state, config),
        profiling=config.profiling,
    )


def _final_choice(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig, check: PlanCheck | None
) -> PlanCheck | None:
    """The best valid outcome once corrections are exhausted."""
    if check is not None and check.valid and check.accepted:
        return check
    held = state.planner.held_plan_json
    if held is not None:
        kept = _check_json(state, view, config, held)
        if kept is not None and kept.valid and kept.accepted:
            return kept
    if check is not None and check.accepted:
        return check
    return None


def on_turn(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig, event: TurnResult
) -> DynamicStrategyState:
    """Interpret the planner's reply into scheduled workstreams, a correction or a wait."""
    planner = state.planner
    turn = planner.turn
    if turn is None:
        return state
    plan, parse_error = _parse(state, view, config, event)
    check = (
        None
        if plan is None
        else validate(
            plan,
            state,
            context.offered(state, view),
            capacity=context.capacity(state, config),
            profiling=config.profiling,
        )
    )
    want = context.capacity(state, config)
    complete = check is not None and check.valid and len(check.accepted) >= want
    if complete and check is not None:
        return schedule(state, view, check)
    if turn.corrections < config.max_corrections:
        held = event.output_json if check is not None and check.valid and check.accepted else None
        if check is None:
            error = parse_error
        elif check.valid:
            error = (
                f"the plan scheduled {len(check.accepted)} of {want} free slots; fill every slot"
            )
        else:
            error = "; ".join(item.render() for item in check.violations)
        return state.model_copy(
            update={
                "planner": planner.model_copy(
                    update={
                        "step": Step.NEEDED,
                        "awaiting": None,
                        "held_plan_json": held or planner.held_plan_json,
                        "last_error": error,
                        "turn": turn.model_copy(
                            update={
                                "serial": turn.serial + 1,
                                "corrections": turn.corrections + 1,
                                "charge": "correction",
                                "invocation": event.invocation,
                            }
                        ),
                    }
                )
            }
        )
    chosen = _final_choice(state, view, config, check)
    if chosen is not None:
        return schedule(state, view, chosen)
    return _nothing_valid(state, planner, parse_error, check)


def _nothing_valid(
    state: DynamicStrategyState,
    planner: PlannerState,
    parse_error: str,
    check: PlanCheck | None,
) -> DynamicStrategyState:
    detail = parse_error or "; ".join(item.render() for item in (check.violations if check else ()))
    stuck = not context.active(state)
    return state.model_copy(
        update={
            "planner": planner.model_copy(
                update={
                    "active": False,
                    "step": Step.NEEDED,
                    "awaiting": None,
                    "held_plan_json": None,
                    "last_error": detail,
                    "failed": stuck,
                    "blocked_at_done": _done(state),
                }
            )
        }
    )


def _parent_of(
    state: DynamicStrategyState, view: RunView, plan: WorkPlan
) -> tuple[RevisionRef, str | None]:
    """Freeze the parent revision this workstream builds on, resolved only from offers."""
    prior = next((item for item in state.hypotheses if item.hypothesis_id == plan.work_id), None)
    if plan.kind is WorkKind.IMPLEMENT and plan.continue_hypothesis and prior is not None:
        last = prior.rounds[-1] if prior.rounds else None
        if last is not None and last.candidate is not None:
            return last.candidate, prior.lineage_parent_id
        previous = [item for item in state.attempts if item.plan.work_id == plan.work_id]
        if previous:
            return previous[-1].parent, previous[-1].parent_hypothesis_id
    if plan.parent_hypothesis_id is not None:
        snapshot = resolve(state.parents, view, plan.parent_hypothesis_id, plan.parent_revision)
        if snapshot is not None:
            return snapshot.revision, plan.parent_hypothesis_id
    return view.facts.baseline, None


def _hypothesis_record(plan: WorkPlan, sequence: int) -> HypothesisRecord:
    return HypothesisRecord(
        hypothesis_id=plan.work_id,
        title=plan.title,
        hypothesis=plan.hypothesis,
        first_sequence=sequence,
        lineage_parent_id=plan.parent_hypothesis_id,
        last_task=plan.task,
    )


def schedule(state: DynamicStrategyState, view: RunView, check: PlanCheck) -> DynamicStrategyState:
    """Apply strategy updates and append one attempt per accepted workstream."""
    hypotheses = list(state.hypotheses)
    for update in check.updates:
        index = next(
            i for i, item in enumerate(hypotheses) if item.hypothesis_id == update.hypothesis_id
        )
        hypotheses[index] = hypotheses[index].model_copy(
            update={
                "strategy": update.disposition,
                "reason": update.reason,
                "reason_kind": update.reason_kind,
            }
        )
    attempts = list(state.attempts)
    for plan in check.accepted:
        sequence = len(attempts) + 1
        parent, parent_hypothesis = _parent_of(state, view, plan)
        if plan.kind is WorkKind.IMPLEMENT:
            index = next(
                (i for i, item in enumerate(hypotheses) if item.hypothesis_id == plan.work_id), None
            )
            if index is None:
                hypotheses.append(_hypothesis_record(plan, sequence))
            else:
                hypotheses[index] = hypotheses[index].model_copy(update={"last_task": plan.task})
        attempts.append(
            AttemptRecord(
                plan=plan,
                sequence=sequence,
                attempt=ids.attempt_id(plan.work_id, sequence),
                parent=parent,
                parent_hypothesis_id=parent_hypothesis,
                phase=(
                    WorkPhase.START if parent == view.facts.baseline else WorkPhase.VERIFY_PARENT
                ),
            )
        )
    planner = state.planner.model_copy(
        update={
            "call": state.planner.call + 1,
            "active": False,
            "step": Step.NEEDED,
            "awaiting": None,
            "held_plan_json": None,
            "last_error": None,
            "blocked_at_done": None,
        }
    )
    return state.model_copy(
        update={"hypotheses": tuple(hypotheses), "attempts": tuple(attempts), "planner": planner}
    )


def on_rendered(state: DynamicStrategyState, outcome: RenderedArtifacts) -> DynamicStrategyState:
    """Attach the rendered planner prompt, or end the call when rendering failed."""
    planner = state.planner
    if planner.step is not Step.RENDERING or planner.turn is None:
        return state
    if outcome.status != "succeeded" or not outcome.prompts:
        return _nothing_valid(state, planner, f"planner prompt render {outcome.status}", None)
    turn = planner.turn.model_copy(
        update={"prompts": outcome.prompts, "tool_policy": outcome.tool_policy}
    )
    return state.model_copy(
        update={
            "planner": planner.model_copy(
                update={"turn": turn, "step": Step.RENDERED, "awaiting": None}
            )
        }
    )


def on_rejected(state: DynamicStrategyState, detail: str) -> DynamicStrategyState:
    """A refused planner decision ends the call like an invalid plan with nothing to wait for."""
    return _nothing_valid(state, state.planner, detail, None)
