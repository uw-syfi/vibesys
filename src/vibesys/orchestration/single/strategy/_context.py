"""Prompt data and bookkeeping derived from state, the run view and config.

Everything here is a pure function of `SingleStrategyState`, `RunView` and `SingleConfig`.
Prompt contexts are typed values; the strategy never writes prompt text.
"""

from vibesys.candidates.api import BaselineStage
from vibesys.hypothesis import OrchestratorPlan, PlanningContext, rounds
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.orchestration.single.strategy._prompts import PlanPrompt, RoundPrompt
from vibesys.orchestration.single.strategy._state import RoundState, SingleStrategyState
from vibesys.profile_focus import FocusView
from vibesys.serial_search import api as serial
from vibesys.serial_search.api import LastProfile, PlanEvidence
from vs_core.api import RevisionRef, RunView


def ledger_of(state: SingleStrategyState, config: SingleConfig) -> rounds.Ledger:
    """The decoded hypothesis ledger, projected onto the run's metric space."""
    return rounds.decode(
        rounds.EncodedLedger(search_json=state.search_json, carry_json=state.carry_json),
        config.metric_space,
    )


def baseline_resolved(state: SingleStrategyState) -> bool:
    """Whether the input measurement has reached a terminal stage."""
    return state.baseline.stage in {
        BaselineStage.MEASURED,
        BaselineStage.UNMEASURABLE,
        BaselineStage.NOT_CONFIGURED,
    }


def input_trusted(state: SingleStrategyState) -> bool:
    """Whether the input revision has a trusted reading, or no gate is configured to give one."""
    return (
        state.baseline.stage in {BaselineStage.MEASURED, BaselineStage.NOT_CONFIGURED}
        and state.baseline.accuracy_passed is not False
    )


def build_base(state: SingleStrategyState, view: RunView) -> RevisionRef:
    """The newest revision a later round may build on: a retained checkpoint, else the input."""
    return state.checkpoint or view.facts.baseline


def commit_ref(commit: str | None, fallback: RevisionRef) -> RevisionRef:
    """The revision a recorded commit names, else ``fallback``."""
    return fallback if not commit else RevisionRef.of_git_commit(commit)


def plan_of(round_state: RoundState) -> OrchestratorPlan:
    """The designer's validated plan of the open round."""
    return OrchestratorPlan.model_validate_json(round_state.plan_json)


def response_of(round_state: RoundState) -> SingleAgentRoundResponse | None:
    """The last valid combined reply of the open round, if any."""
    if round_state.response_json is None:
        return None
    return SingleAgentRoundResponse.model_validate_json(round_state.response_json)


def plan_prompt(
    state: SingleStrategyState,
    config: SingleConfig,
    view: RunView,
    planning: PlanningContext,
    focus: FocusView | None = None,
) -> PlanPrompt:
    """The new-hypothesis request: history, the frontier, carried notices and the last profile."""
    evidence = serial.plan_evidence(
        config.hypotheses,
        ledger_of(state, config),
        planning,
        focus,
    ).model_copy(
        update={
            "benchmark_enabled": config.benchmark_configured,
            "last_profile": state.last_profile,
        }
    )
    return PlanPrompt(
        **{name: getattr(evidence, name) for name in PlanEvidence.model_fields},
        base_revision=build_base(state, view),
    )


def last_profile_of(response: SingleAgentRoundResponse | None) -> LastProfile | None:
    """The profile fields the implementer reported, for the next designer prompt."""
    if response is None:
        return None
    return LastProfile(
        analysis=response.profile_analysis,
        bottlenecks=response.bottlenecks,
        suggestions=response.suggestions,
        perf_metric=response.perf_metric,
        perf_unit=response.perf_unit,
    )


def round_prompt(round_state: RoundState, config: SingleConfig) -> RoundPrompt:
    """The implementation, self-review and profile request of the open round."""
    plan = plan_of(round_state)
    return RoundPrompt(
        round_number=round_state.number,
        retry=round_state.turns_spent + 1,
        hypothesis_id=plan.hypothesis_id,
        title=plan.title,
        hypothesis=plan.hypothesis,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        invariants=plan.invariants,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
        base_revision=round_state.base,
        feedback=round_state.feedback,
        no_valid_reply=round_state.no_valid_reply,
        dominated_by_rounds=round_state.dominated_by_rounds,
        official_due=round_state.official_reason is not None,
        official_reason=round_state.official_reason,
        interface=config.interface,
        profiler_id=config.profiler_id,
    )


__all__ = [
    "baseline_resolved",
    "build_base",
    "commit_ref",
    "input_trusted",
    "last_profile_of",
    "ledger_of",
    "plan_of",
    "plan_prompt",
    "response_of",
    "round_prompt",
]
