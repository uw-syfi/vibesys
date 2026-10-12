"""The run-scoped steps that design a new hypothesis: attribution, routing, profile, plan.

A new hypothesis starts with run-scoped, free turns before any attempt. In the profile-guided
preset the configured attribution command runs first and its ranked components steer the
designer. When the run offers a profiler, a routing turn decides whether fresh profile evidence
is wanted and a profiler turn collects it. The designer then writes the plan, with one
correction on a bad one; a valid plan starts the hypothesis in the ledger and opens the round.
The routing and profiling turns are advisory: a failure of either is a note and the plan
proceeds. Attribution and the plan are the run's contract, so a failure of either ends the
search. A hypothesis the ledger keeps active continues without any of these.
"""

from typing import Literal

from pydantic import ValidationError

from vibesys.hypothesis import Continue, Finished, OrchestratorPlan, rounds
from vibesys.orchestration.multi.models import PreRoundDecision
from vibesys.orchestration.multi.strategy import _context as context
from vibesys.orchestration.multi.strategy import _ids as ids
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._draft import (
    Draft,
    invocation_for,
    operation,
    render_id,
    render_request,
    request_turn,
    turn_id,
)
from vibesys.orchestration.multi.strategy._operations import (
    CaptureCommand,
    CommandCapture,
    RenderedArtifacts,
)
from vibesys.orchestration.multi.strategy._prompts import PromptContext, ReplyCorrectionPrompt
from vibesys.orchestration.multi.strategy._replies import ProfilerFindings
from vibesys.orchestration.multi.strategy._schemas import (
    PLANNER_REPLY,
    PRE_ROUND_REPLY,
    PROFILER_REPLY,
)
from vibesys.orchestration.multi.strategy._state import (
    NOTE_LIMIT,
    DesignState,
    MultiStrategyState,
    Role,
    RoundState,
    Step,
    Track,
    TurnRecord,
    WorkPhase,
)
from vibesys.profile_focus import ProfileAttributionError
from vibesys.serial_search import api as serial
from vibesys.serial_search.api import LastProfile
from vs_core.api import (
    Access,
    ObservationStatus,
    RunView,
    SchemaRef,
    TurnFailureKind,
    TurnResult,
    TurnShape,
    run_scope,
)

_SUBJECTS = {Track.PRE_ROUND: "preround", Track.PROFILE: "profile", Track.PLAN: "plan"}
_ROLES = {Track.PRE_ROUND: Role.PLANNER, Track.PROFILE: Role.PROFILER, Track.PLAN: Role.PLANNER}
_CORRECTION_ROLES: dict[Track, Literal["planner", "profiler"]] = {
    Track.PRE_ROUND: "planner",
    Track.PROFILE: "profiler",
    Track.PLAN: "planner",
}
_ADVISORY = frozenset({Track.PRE_ROUND, Track.PROFILE})
CAPTURE_SUBJECT = "run"


def subject_of(track: Track) -> str:
    """Decision subject of the run-scoped turns of ``track``."""
    return _SUBJECTS[track]


def may_plan(state: MultiStrategyState, config: MultiConfig) -> bool:
    """Whether a new design may begin."""
    return (
        state.round is None
        and state.phase.value == "searching"
        and not state.stopping
        and not state.design.failed
        and context.baseline_resolved(state)
        and state.closed < config.max_rounds
    )


def first_track(config: MultiConfig) -> Track:
    """Where a design starts: attribution, else routing when profiling, else the plan."""
    if config.attribution is not None:
        return Track.CAPTURE
    return Track.PRE_ROUND if config.profiling else Track.PLAN


def after(track: Track, config: MultiConfig, design: DesignState) -> Track:
    """The track that follows ``track``, given what the routing decided."""
    if track is Track.CAPTURE:
        return Track.PRE_ROUND if config.profiling else Track.PLAN
    if track is Track.PRE_ROUND:
        return Track.PROFILE if design.profile_focus is not None else Track.PLAN
    return Track.PLAN


def decide(draft: Draft) -> None:
    """Open a continued round, or run the design steps and request the next turn."""
    state, config = draft.state, draft.config
    design = state.design
    if not design.active:
        if not may_plan(state, config):
            return
        ledger = context.ledger_of(state, config)
        decision = rounds.next_round(config.hypotheses, ledger, state.closed + 1)
        if isinstance(decision, Finished):
            return
        if isinstance(decision, Continue):
            draft.update(**_continued(state, draft, decision))
            return
        design = DesignState(serial=design.serial, active=True, track=first_track(config))
    draft.update(design=_advance(draft, design))


def planned_reason(
    config: MultiConfig, ledger: rounds.Ledger, plan: OrchestratorPlan, number: int
) -> str | None:
    """Why the official gates are scheduled after this round's candidate, as the prompts show it."""
    reason = rounds.official_reason(
        config.hypotheses,
        ledger,
        round_number=number,
        requested=plan.request_official_evaluation,
    )
    focus = serial.focus_of(ledger, serial.focus_policy(config.attribution))
    if reason is None and focus is not None and focus.active_component:
        return "profile-guided component measurement"
    return reason


def _continued(state: MultiStrategyState, draft: Draft, decision: Continue) -> dict[str, object]:
    hypothesis = decision.hypothesis
    plan = hypothesis.plan
    number = state.closed + 1
    return {
        "round": RoundState(
            attempt=ids.attempt_id(plan.hypothesis_id, number),
            number=number,
            hypothesis_id=plan.hypothesis_id,
            plan_json=plan.model_dump_json(),
            base=context.build_base(state, draft.view),
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
            planned_official_reason=planned_reason(
                draft.config, context.ledger_of(state, draft.config), plan, number
            ),
        )
    }


def _advance(draft: Draft, design: DesignState) -> DesignState:
    if design.step in (Step.RENDERING, Step.AWAITING):
        return design
    if design.track is Track.CAPTURE:
        return _capture(draft, design)
    return _turn(draft, design)


def _capture(draft: Draft, design: DesignState) -> DesignState:
    """Propose the attribution command; its output is folded by `on_captured`."""
    attribution = draft.config.attribution
    if attribution is None or design.step is not Step.NEEDED:
        return design
    identifier = ids.decision_id("capture", CAPTURE_SUBJECT, draft.state.closed + 1)
    draft.emit(
        operation(
            draft,
            identifier,
            run_scope(draft.view),
            CaptureCommand(
                revision=context.build_base(draft.state, draft.view),
                argv=attribution.command,
                output_argument=serial.OUTPUT_ARGUMENT,
                timeout_seconds=attribution.timeout_seconds,
            ),
        )
    )
    return design.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _body(draft: Draft, design: DesignState) -> PromptContext:
    """The request the track's turn asks, from the ledger as it stands."""
    state, config = draft.state, draft.config
    ledger = context.ledger_of(state, config)
    number = state.closed + 1
    focus = serial.focus_of(ledger, serial.focus_policy(config.attribution))
    if design.track is Track.PROFILE:
        return context.profiler_prompt(state, config)
    planning = rounds.planning(config.hypotheses, ledger, number)
    if design.track is Track.PRE_ROUND:
        return context.pre_round_prompt(state, config, planning, focus)
    return context.plan_prompt(state, config, draft.view, planning, focus)


def _shape(draft: Draft, track: Track) -> tuple[Access, SchemaRef, float]:
    config = draft.config
    if track is Track.PRE_ROUND:
        return Access.READ_ONLY, PRE_ROUND_REPLY, config.orchestrator_turn_seconds
    if track is Track.PROFILE:
        return Access.WRITE_ARTIFACTS, PROFILER_REPLY, config.profiler_turn_seconds
    return Access.WRITE_ARTIFACTS, PLANNER_REPLY, config.orchestrator_turn_seconds


def _turn(draft: Draft, design: DesignState) -> DesignState:
    track = design.track
    subject = _SUBJECTS[track]
    turn = design.turn or TurnRecord(role=_ROLES[track], serial=design.serial, charge="free")
    if design.step is Step.NEEDED:
        if not draft.due(turn):
            return design.model_copy(update={"turn": turn})
        body = turn.context or _body(draft, design)
        identifier = render_id(subject, turn)
        draft.emit(
            operation(draft, identifier, run_scope(draft.view), render_request(subject, turn, body))
        )
        return design.model_copy(
            update={"step": Step.RENDERING, "awaiting": identifier, "turn": turn}
        )
    if design.step is Step.RENDERED:
        scope = run_scope(draft.view)
        access, schema, seconds = _shape(draft, track)
        draft.emit(
            request_turn(
                draft,
                TurnShape(
                    role=_ROLES[track],
                    subject=subject,
                    workspace=scope,
                    access=access,
                    output_schema=schema,
                    seconds=seconds,
                ),
                turn,
                scope,
            )
        )
        return design.model_copy(update={"step": Step.AWAITING, "awaiting": turn_id(subject, turn)})
    return design


def owns_turn(state: MultiStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers the design's outstanding turn."""
    design = state.design
    return (
        design.active
        and design.step is Step.AWAITING
        and design.turn is not None
        and design.track is not Track.CAPTURE
        and event.invocation.invocation_id.root
        == invocation_for(design.turn.role, _SUBJECTS[design.track], design.turn)
    )


def _put(state: MultiStrategyState, design: DesignState) -> MultiStrategyState:
    return state.model_copy(update={"design": design})


def _note(state: MultiStrategyState, text: str) -> tuple[str, ...]:
    return (*state.notes, text)[-NOTE_LIMIT:]


def _failed(state: MultiStrategyState, detail: str) -> MultiStrategyState:
    """The design could not produce a plan: no further round can start."""
    design = state.design
    serial_next = design.turn.serial + 1 if design.turn else design.serial
    failed = design.model_copy(
        update={
            "active": False,
            "step": Step.NEEDED,
            "awaiting": None,
            "turn": None,
            "last_error": detail,
            "failed": True,
            "serial": serial_next,
        }
    )
    return state.model_copy(update={"design": failed})


def _skipped(state: MultiStrategyState, detail: str) -> MultiStrategyState:
    """An advisory step failed: note it and let the plan proceed without it."""
    design = state.design
    return state.model_copy(
        update={
            "design": _moved(design, Track.PLAN),
            "notes": _note(state, f"{design.track.value}: {detail}"),
        }
    )


def _moved(design: DesignState, track: Track, **extra: object) -> DesignState:
    """The design at the start of ``track``, past the turn that just ended."""
    serial_next = design.turn.serial + 1 if design.turn else design.serial
    return design.model_copy(
        update={
            "track": track,
            "step": Step.NEEDED,
            "awaiting": None,
            "turn": None,
            "last_error": None,
            "serial": serial_next,
            **extra,
        }
    )


def _lost(
    state: MultiStrategyState, view: RunView, config: MultiConfig, event: TurnResult
) -> MultiStrategyState:
    """The transport lost the turn: ask the same question again, within the budget."""
    design = state.design
    turn = design.turn
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
    detail = event.detail or "the turn was lost"
    if again is None:
        return _ended(state, detail)
    return _put(
        state, design.model_copy(update={"step": Step.NEEDED, "awaiting": None, "turn": again})
    )


def _ended(state: MultiStrategyState, detail: str) -> MultiStrategyState:
    """A turn ended without a usable answer: advisory tracks are skipped, the plan fails."""
    if state.design.track in _ADVISORY:
        return _skipped(state, detail)
    return _failed(state, detail)


def _corrected(
    state: MultiStrategyState,
    config: MultiConfig,
    event: TurnResult,
    error: str,
    *,
    explain: bool = True,
) -> MultiStrategyState:
    """Ask the role to fix its reply once; past the bound the track ends.

    ``explain`` tells the role what was wrong with a parsed reply; a turn that ended without a
    reply (the session refused output that broke its schema) is asked again as it was.
    """
    design = state.design
    turn = design.turn
    correction = (
        ReplyCorrectionPrompt(role=_CORRECTION_ROLES[design.track], error=error)
        if explain
        else None
    )
    corrected = (
        None
        if turn is None
        else turn.corrected(event.invocation, correction, limit=config.max_corrections)
    )
    if corrected is None:
        return _ended(state, error)
    return _put(
        state,
        design.model_copy(
            update={"step": Step.NEEDED, "awaiting": None, "last_error": error, "turn": corrected}
        ),
    )


def on_turn(
    state: MultiStrategyState, view: RunView, config: MultiConfig, event: TurnResult
) -> MultiStrategyState:
    """Interpret the answer of the design's turn: advance, correct, ask again or end."""
    design = state.design
    if design.turn is None:
        return state
    if event.failure is TurnFailureKind.TRANSPORT_LOST:
        return _lost(state, view, config, event)
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        detail = event.detail or f"the turn did not complete ({event.observation.status.value})"
        return _corrected(state, config, event, detail, explain=False)
    if design.track is Track.PRE_ROUND:
        return _routed(state, config, event)
    if design.track is Track.PROFILE:
        return _profiled(state, config, event)
    return _planned(state, view, config, event)


def _routed(
    state: MultiStrategyState, config: MultiConfig, event: TurnResult
) -> MultiStrategyState:
    try:
        decision = PreRoundDecision.model_validate_json(event.output_json or "")
    except ValidationError as error:
        return _corrected(state, config, event, str(error))
    focus = None
    if decision.need_profile:
        focus = decision.profile_focus or "general steady-state benchmark hotspots"
    routed = state.design.model_copy(update={"profile_focus": focus})
    return _put(state, _moved(routed, after(Track.PRE_ROUND, config, routed)))


def _profiled(
    state: MultiStrategyState, config: MultiConfig, event: TurnResult
) -> MultiStrategyState:
    try:
        findings = ProfilerFindings.model_validate_json(event.output_json or "")
    except ValidationError as error:
        return _corrected(state, config, event, str(error))
    profile = LastProfile(
        source="profiler",
        analysis=findings.analysis,
        bottlenecks=findings.bottlenecks,
        suggestions=findings.suggestions,
        perf_metric=findings.perf_metric,
        perf_unit=findings.perf_unit,
        evidence_directory=context.profiler_prompt(state, config).evidence_directory,
    )
    return _put(state, _moved(state.design, Track.PLAN, profile=profile))


def _planned(
    state: MultiStrategyState, view: RunView, config: MultiConfig, event: TurnResult
) -> MultiStrategyState:
    plan = serial.accept_plan(
        config.hypotheses,
        context.ledger_of(state, config),
        event.output_json or "",
        round_number=state.closed + 1,
    )
    if isinstance(plan, str):
        return _corrected(state, config, event, plan)
    return _start(state, view, config, plan)


def _start(
    state: MultiStrategyState, view: RunView, config: MultiConfig, plan: OrchestratorPlan
) -> MultiStrategyState:
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
    design = state.design
    fresh = DesignState(serial=design.turn.serial + 1 if design.turn else design.serial)
    number = state.closed + 1
    hypothesis = started.hypothesis
    return state.model_copy(
        update={
            "search_json": encoded.search_json,
            "carry_json": encoded.carry_json,
            "design": fresh,
            "round": RoundState(
                attempt=ids.attempt_id(plan.hypothesis_id, number),
                number=number,
                hypothesis_id=plan.hypothesis_id,
                plan_json=plan.model_dump_json(),
                base=context.commit_ref(serial.base_commit(started), view.facts.baseline),
                feedback=None,
                phase=WorkPhase.START,
                revalidation_required=hypothesis.gate_revalidation_pending,
                planned_official_reason=planned_reason(config, started_ledger, plan, number),
            ),
        }
    )


def on_rendered(state: MultiStrategyState, outcome: RenderedArtifacts) -> MultiStrategyState:
    """Attach the rendered prompt, or end the track when rendering failed."""
    design = state.design
    if design.step is not Step.RENDERING or design.turn is None:
        return state
    if outcome.status != "succeeded" or not outcome.prompts:
        return _ended(state, f"prompt render {outcome.status}")
    turn = design.turn.with_prompts(outcome.prompts, outcome.tool_policy)
    return _put(
        state, design.model_copy(update={"turn": turn, "step": Step.RENDERED, "awaiting": None})
    )


def on_captured(
    state: MultiStrategyState, config: MultiConfig, outcome: CommandCapture
) -> MultiStrategyState:
    """Fold the attribution into the focus state once; a failed command ends the run.

    Profile guidance is the preset's contract, so a capture that did not produce a valid result
    protocol is a failure of the run, as it is for the legacy loop.
    """
    design = state.design
    attribution = config.attribution
    policy = serial.focus_policy(attribution)
    if (
        design.track is not Track.CAPTURE
        or design.step is not Step.AWAITING
        or policy is None
        or attribution is None
    ):
        return state
    if outcome.status != "succeeded" or outcome.exit_code != 0:
        detail = serial.capture_failure(
            attribution.command,
            exit_code=outcome.exit_code,
            detail=outcome.detail,
            output=outcome.output,
        )
        return _failed(state, f"profile-guided attribution failed: {detail}")
    try:
        bottlenecks = serial.bottlenecks_of(outcome.output)
    except ProfileAttributionError as error:
        return _failed(state, f"profile-guided attribution failed: {error}")
    ledger = serial.observe(
        context.ledger_of(state, config),
        policy,
        round_number=state.closed + 1,
        bottlenecks=bottlenecks,
    )
    return state.model_copy(
        update={
            "search_json": rounds.encode(ledger).search_json,
            "design": _moved(design, after(Track.CAPTURE, config, design)),
        }
    )


def on_rejected(state: MultiStrategyState, detail: str) -> MultiStrategyState:
    """A refused decision of the design ends its track."""
    if state.design.track is Track.CAPTURE:
        return _failed(state, detail)
    return _ended(state, detail)


def awaits(state: MultiStrategyState, decision: str | None) -> bool:
    """Whether the design waits on the decision with this identity."""
    design = state.design
    return (
        design.active
        and design.awaiting is not None
        and decision is not None
        and design.awaiting.root == decision
    )
