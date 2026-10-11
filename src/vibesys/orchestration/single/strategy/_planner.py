"""The designer's turns: attribution, a plan, one correction, then the round it opens.

A new hypothesis starts with the designer, a run-scoped free turn that may write the roadmap
artifacts. Its reply is parsed into an `OrchestratorPlan`, checked against the ledger, and
corrected once; a valid plan starts the hypothesis in the ledger and opens the round. A
hypothesis the ledger keeps active continues without a designer turn. In the profile-guided
preset the configured attribution command runs first, in a throwaway candidate, and its ranked
components steer the designer.
"""

from pydantic import ValidationError

from vibesys.hypothesis import (
    Continue,
    Finished,
    InvalidPlanError,
    OrchestratorPlan,
    StartedHypothesis,
    normalize_hypothesis_title,
    rounds,
)
from vibesys.orchestration.single.strategy import _context as context
from vibesys.orchestration.single.strategy import _ids as ids
from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.orchestration.single.strategy._draft import (
    Draft,
    invocation_for,
    operation,
    render_id,
    render_request,
    request_turn,
    turn_id,
)
from vibesys.orchestration.single.strategy._focus import BEGIN, END, OUTPUT_ARGUMENT, focus_policy
from vibesys.orchestration.single.strategy._operations import (
    CaptureCommand,
    CommandCapture,
    RenderedArtifacts,
)
from vibesys.orchestration.single.strategy._prompts import ReplyCorrectionPrompt
from vibesys.orchestration.single.strategy._schemas import PLANNER_REPLY
from vibesys.orchestration.single.strategy._state import (
    PlannerState,
    Role,
    RoundState,
    SingleStrategyState,
    Step,
    TurnRecord,
    WorkPhase,
)
from vibesys.profile_focus import ProfileAttributionError, parse_attribution
from vs_core.api import (
    Access,
    ObservationStatus,
    RevisionRef,
    RunView,
    TurnFailureKind,
    TurnResult,
    TurnShape,
    run_scope,
)

SUBJECT = "run"


def may_plan(state: SingleStrategyState, config: SingleConfig) -> bool:
    """Whether a new designer call may begin."""
    return (
        state.round is None
        and state.phase.value == "searching"
        and not state.stopping
        and not state.planner.failed
        and context.baseline_resolved(state)
        and state.closed < config.max_rounds
    )


def decide(draft: Draft) -> None:
    """Open a continued round, or run attribution, render and request the designer's turn."""
    state, config = draft.state, draft.config
    planner = state.planner
    if not planner.active:
        if not may_plan(state, config):
            return
        ledger = context.ledger_of(state, config)
        decision = rounds.next_round(config.hypotheses, ledger, state.closed + 1)
        if isinstance(decision, Finished):
            return
        if isinstance(decision, Continue):
            draft.update(**_continued(state, draft.view, decision))
            return
        planner = planner.model_copy(
            update={
                "active": True,
                "step": Step.NEEDED,
                "awaiting": None,
                "last_error": None,
                "capture_step": Step.NEEDED,
                "capture_awaiting": None,
                "capture_done": config.attribution is None,
                "turn": TurnRecord(role=Role.PLANNER, serial=planner.serial, charge="free"),
            }
        )
    planner = _capture(draft, planner)
    if planner.capture_done:
        planner = _turn(draft, planner)
    draft.update(planner=planner)


def _continued(state: SingleStrategyState, view: RunView, decision: Continue) -> dict[str, object]:
    hypothesis = decision.hypothesis
    plan = hypothesis.plan
    return {
        "round": RoundState(
            attempt=ids.attempt_id(plan.hypothesis_id, state.closed + 1),
            number=state.closed + 1,
            hypothesis_id=plan.hypothesis_id,
            plan_json=plan.model_dump_json(),
            base=context.build_base(state, view),
            feedback=hypothesis.feedback,
        )
    }


def _capture(draft: Draft, planner: PlannerState) -> PlannerState:
    """Propose the attribution command when the profile-guided preset needs it."""
    attribution = draft.config.attribution
    if planner.capture_done or attribution is None or planner.capture_step is not Step.NEEDED:
        return planner
    identifier = ids.decision_id("capture", SUBJECT, draft.state.closed + 1)
    draft.emit(
        operation(
            draft,
            identifier,
            run_scope(draft.view),
            CaptureCommand(
                revision=context.build_base(draft.state, draft.view),
                argv=attribution.command,
                output_argument=OUTPUT_ARGUMENT,
                timeout_seconds=attribution.timeout_seconds,
            ),
        )
    )
    return planner.model_copy(
        update={"capture_step": Step.AWAITING, "capture_awaiting": identifier}
    )


def _turn(draft: Draft, planner: PlannerState) -> PlannerState:
    turn = planner.turn
    if turn is None:
        return planner
    if planner.step is Step.NEEDED:
        if not draft.due(turn):
            return planner
        ledger = context.ledger_of(draft.state, draft.config)
        planning = rounds.planning(draft.config.hypotheses, ledger, draft.state.closed + 1)
        focus_state = ledger.search.profile_guidance
        policy = focus_policy(draft.config)
        view = None if policy is None or focus_state is None else policy.focus(focus_state)
        prompt = context.plan_prompt(draft.state, draft.config, draft.view, planning, view)
        body = prompt
        if planner.last_error is not None and turn.charge == "correction":
            body = ReplyCorrectionPrompt(role="planner", error=planner.last_error)
        identifier = render_id(SUBJECT, turn)
        draft.emit(
            operation(draft, identifier, run_scope(draft.view), render_request(SUBJECT, turn, body))
        )
        return planner.model_copy(update={"step": Step.RENDERING, "awaiting": identifier})
    if planner.step is Step.RENDERED:
        scope = run_scope(draft.view)
        draft.emit(
            request_turn(
                draft,
                TurnShape(
                    role=Role.PLANNER,
                    subject=SUBJECT,
                    workspace=scope,
                    access=Access.WRITE_ARTIFACTS,
                    output_schema=PLANNER_REPLY,
                    seconds=draft.config.planner_turn_seconds,
                ),
                turn,
                scope,
            )
        )
        return planner.model_copy(
            update={"step": Step.AWAITING, "awaiting": turn_id(SUBJECT, turn)}
        )
    return planner


def owns_turn(state: SingleStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers the designer's outstanding turn."""
    planner = state.planner
    return (
        planner.step is Step.AWAITING
        and planner.turn is not None
        and event.invocation.invocation_id.root
        == invocation_for(Role.PLANNER, SUBJECT, planner.turn)
    )


def _failed(state: SingleStrategyState, detail: str) -> SingleStrategyState:
    """The designer could not produce a plan: no further round can start."""
    planner = state.planner.model_copy(
        update={
            "active": False,
            "step": Step.NEEDED,
            "awaiting": None,
            "last_error": detail,
            "failed": True,
            "serial": (state.planner.turn.serial + 1)
            if state.planner.turn
            else state.planner.serial,
        }
    )
    return state.model_copy(update={"planner": planner})


def _check(plan: OrchestratorPlan, state: SingleStrategyState, config: SingleConfig) -> None:
    """Raise `ValueError` for a plan the ledger cannot accept."""
    updates = [item.hypothesis_id for item in plan.hypothesis_updates]
    if len(updates) != len(set(updates)):
        raise InvalidPlanError.duplicate_updates()
    if plan.hypothesis_id in updates:
        raise InvalidPlanError.self_reference()
    ledger = context.ledger_of(state, config)
    if ledger.search.by_id(plan.hypothesis_id) is not None:
        raise InvalidPlanError.reused_id(plan.hypothesis_id)
    rounds.validate_plan(config.hypotheses, ledger, plan.hypothesis_updates)


def _plan_from(
    state: SingleStrategyState, config: SingleConfig, event: TurnResult
) -> OrchestratorPlan | str:
    """The reply as a validated plan, else what is wrong with it."""
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        return (
            event.detail or f"the designer turn did not complete ({event.observation.status.value})"
        )
    try:
        plan = OrchestratorPlan.model_validate_json(event.output_json)
        plan = plan.model_copy(
            update={
                "hypothesis_id": plan.hypothesis_id.strip() or f"hypothesis-{state.closed + 1:04d}",
                "title": normalize_hypothesis_title(plan.title),
                "recommended_skills": [],
            }
        )
        _check(plan, state, config)
    except (ValidationError, ValueError) as problem:
        return str(problem)
    return plan


def _ask_again(
    state: SingleStrategyState, view: RunView, config: SingleConfig, event: TurnResult
) -> SingleStrategyState:
    """The transport lost the designer's turn: ask the same question again, within the budget."""
    planner = state.planner
    turn = planner.turn
    again = (
        None
        if turn is None
        else turn.asked_again(
            event.invocation,
            now=view.run.now_at,
            limit=config.max_turn_drops,
            backoff=config.drop_backoff(turn.drops),
        )
    )
    if again is None:
        return _failed(state, event.detail or "the designer turn was lost")
    return state.model_copy(
        update={
            "planner": planner.model_copy(
                update={"step": Step.NEEDED, "awaiting": None, "turn": again}
            )
        }
    )


def on_turn(
    state: SingleStrategyState, view: RunView, config: SingleConfig, event: TurnResult
) -> SingleStrategyState:
    """Interpret the designer's reply: start the round, ask for a correction, or give up."""
    planner = state.planner
    turn = planner.turn
    if turn is None:
        return state
    if event.failure is TurnFailureKind.TRANSPORT_LOST:
        return _ask_again(state, view, config, event)
    plan = _plan_from(state, config, event)
    if not isinstance(plan, str):
        return _start(state, view, config, plan)
    corrected = turn.corrected(event.invocation, None, limit=config.max_corrections)
    if corrected is None:
        return _failed(state, plan)
    return state.model_copy(
        update={
            "planner": planner.model_copy(
                update={
                    "step": Step.NEEDED,
                    "awaiting": None,
                    "last_error": plan,
                    "turn": corrected,
                }
            )
        }
    )


def _start(
    state: SingleStrategyState, view: RunView, config: SingleConfig, plan: OrchestratorPlan
) -> SingleStrategyState:
    ledger = context.ledger_of(state, config)
    base = context.build_base(state, view)
    started_ledger, started = rounds.start(
        config.hypotheses,
        ledger,
        plan,
        round_number=state.closed + 1,
        current_commit=base.revision_id.root,
        input_baseline=view.facts.baseline.revision_id.root,
    )
    encoded = rounds.encode(started_ledger)
    fresh = PlannerState(
        serial=(state.planner.turn.serial + 1) if state.planner.turn else state.planner.serial,
        capture_done=False,
    )
    return state.model_copy(
        update={
            "search_json": encoded.search_json,
            "carry_json": encoded.carry_json,
            "planner": fresh,
            "round": RoundState(
                attempt=ids.attempt_id(plan.hypothesis_id, state.closed + 1),
                number=state.closed + 1,
                hypothesis_id=plan.hypothesis_id,
                plan_json=plan.model_dump_json(),
                base=_round_base(started, view),
                feedback=None,
                phase=WorkPhase.START,
            ),
        }
    )


def _round_base(started: StartedHypothesis, view: RunView) -> RevisionRef:
    rollback = started.rollback
    commit = (
        rollback.commit
        if rollback is not None and rollback.resolved
        else started.hypothesis.parent_commit
    )
    return context.commit_ref(commit, view.facts.baseline)


def on_rendered(state: SingleStrategyState, outcome: RenderedArtifacts) -> SingleStrategyState:
    """Attach the rendered designer prompt, or end the call when rendering failed."""
    planner = state.planner
    if planner.step is not Step.RENDERING or planner.turn is None:
        return state
    if outcome.status != "succeeded" or not outcome.prompts:
        return _failed(state, f"designer prompt render {outcome.status}")
    turn = planner.turn.with_prompts(outcome.prompts, outcome.tool_policy)
    return state.model_copy(
        update={
            "planner": planner.model_copy(
                update={"turn": turn, "step": Step.RENDERED, "awaiting": None}
            )
        }
    )


def on_captured(
    state: SingleStrategyState, config: SingleConfig, outcome: CommandCapture
) -> SingleStrategyState:
    """Fold the attribution into the focus state once; a failed command ends the run.

    Profile guidance is the preset's contract, so a capture that did not produce a valid result
    protocol is a failure of the run, as it is for the legacy loop.
    """
    planner = state.planner
    policy = focus_policy(config)
    if planner.capture_step is not Step.AWAITING or policy is None:
        return state
    if outcome.status != "succeeded" or outcome.exit_code != 0:
        detail = outcome.detail or f"the attribution command exited with code {outcome.exit_code}"
        return _failed(state, f"profile-guided attribution failed: {detail}")
    try:
        bottlenecks = parse_attribution(f"{BEGIN}\n{outcome.output}\n{END}")
    except ProfileAttributionError as error:
        return _failed(state, f"profile-guided attribution failed: {error}")
    ledger = context.ledger_of(state, config)
    focus_state = ledger.search.profile_guidance or policy.initial()
    focused = policy.observe(focus_state, round_number=state.closed + 1, bottlenecks=bottlenecks)
    search = ledger.search.model_copy(update={"profile_guidance": focused}, deep=True)
    encoded = rounds.encode(rounds.Ledger(search, ledger.carry))
    return state.model_copy(
        update={
            "search_json": encoded.search_json,
            "planner": planner.model_copy(
                update={"capture_step": Step.NEEDED, "capture_awaiting": None, "capture_done": True}
            ),
        }
    )


def on_rejected(state: SingleStrategyState, detail: str) -> SingleStrategyState:
    """A refused designer decision ends the call: no plan can be produced."""
    return _failed(state, detail)
