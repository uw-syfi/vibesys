"""Prompt data and bookkeeping derived from state, the run view and config.

Everything here is a pure function of `MultiStrategyState`, `RunView` and `MultiConfig`.
Prompt contexts are typed values; the strategy never writes prompt text.
"""

from vibesys.candidates.api import BaselineStage
from vibesys.hypothesis import (
    ArchiveConflict,
    Hypothesis,
    OrchestratorPlan,
    PlanningContext,
    rounds,
)
from vibesys.orchestration.multi.models import ImplementerResponse
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._prompts import (
    ArchiveConflictView,
    Claim,
    ConflictMetric,
    ContinuePrompt,
    Dominator,
    GateFacts,
    HypothesisPlan,
    ImplementerClaims,
    ImplementPrompt,
    JudgePrompt,
    PlanPrompt,
    PreRoundPrompt,
    ProfilerPrompt,
)
from vibesys.orchestration.multi.strategy._state import MultiStrategyState, RoundState
from vibesys.profile_focus import FocusView
from vibesys.serial_search import api as serial
from vibesys.serial_search.api import PlanEvidence
from vs_core.api import EvidenceRef, RevisionRef, RunView

PROFILE_DIRECTORY = "profile"


def ledger_of(state: MultiStrategyState, config: MultiConfig) -> rounds.Ledger:
    """The decoded hypothesis ledger, projected onto the run's metric space."""
    return rounds.decode(
        rounds.EncodedLedger(search_json=state.search_json, carry_json=state.carry_json),
        config.metric_space,
    )


def baseline_resolved(state: MultiStrategyState) -> bool:
    """Whether the input measurement has reached a terminal stage."""
    return state.baseline.stage in {
        BaselineStage.MEASURED,
        BaselineStage.UNMEASURABLE,
        BaselineStage.NOT_CONFIGURED,
    }


def input_trusted(state: MultiStrategyState) -> bool:
    """Whether the input revision has a trusted reading, or no gate is configured to give one."""
    return (
        state.baseline.stage in {BaselineStage.MEASURED, BaselineStage.NOT_CONFIGURED}
        and state.baseline.accuracy_passed is not False
    )


def build_base(state: MultiStrategyState, view: RunView) -> RevisionRef:
    """The newest revision a later round may build on: a retained checkpoint, else the input."""
    return state.checkpoint or view.facts.baseline


def commit_ref(commit: str | None, fallback: RevisionRef) -> RevisionRef:
    """The revision a recorded commit names, else ``fallback``."""
    return fallback if not commit else RevisionRef.of_git_commit(commit)


def plan_of(round_state: RoundState) -> OrchestratorPlan:
    """The designer's validated plan of the open round."""
    return OrchestratorPlan.model_validate_json(round_state.plan_json)


def response_of(round_state: RoundState) -> ImplementerResponse | None:
    """The last valid implementer reply of the open round, if any."""
    if round_state.response_json is None:
        return None
    return ImplementerResponse.model_validate_json(round_state.response_json)


def hypothesis_plan(plan: OrchestratorPlan) -> HypothesisPlan:
    """The plan fields every round prompt shows."""
    return HypothesisPlan(
        hypothesis_id=plan.hypothesis_id,
        title=plan.title,
        hypothesis=plan.hypothesis,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        invariants=plan.invariants,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
    )


def gate_facts(hypothesis: Hypothesis | None) -> GateFacts:
    """What the framework already established for the active hypothesis."""
    if hypothesis is None:
        return GateFacts()
    return GateFacts(
        revert_applied=hypothesis.revert_applied,
        revert_round=hypothesis.parent_round,
        revert_commit=hypothesis.revert_commit,
        revalidation_pending=hypothesis.gate_revalidation_pending,
        approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        approved_perf_metric=hypothesis.gate_approved_perf_metric,
        approved_perf_unit=hypothesis.gate_approved_perf_unit,
    )


def _claims(values: dict[str, float]) -> tuple[Claim, ...]:
    return tuple(Claim(name=name, value=value) for name, value in values.items())


def claims_of(reply: ImplementerResponse) -> ImplementerClaims:
    """The implementer's reply as untrusted data for the judge."""
    return ImplementerClaims(
        summary=reply.summary,
        expected_behavior=reply.expected_behavior,
        hypothesis_outcome=reply.hypothesis_outcome.value,
        evidence=reply.evidence,
        next_step=reply.next_step,
        perf_metric=reply.perf_metric,
        perf_unit=reply.perf_unit,
        metrics=_claims(reply.metrics),
        evaluation_artifact=reply.evaluation_artifact,
        candidate_disposition=reply.candidate_disposition.value,
        candidate_metrics=_claims(reply.candidate_metrics),
        candidate_evaluation_artifact=reply.candidate_evaluation_artifact,
        candidate_operating_point=reply.candidate_operating_point,
        candidate_retention_reason=reply.candidate_retention_reason,
        validation_recipe_artifact=reply.validation_recipe_artifact,
    )


def conflict_view(conflict: ArchiveConflict) -> ArchiveConflictView:
    """The archive conflict as prompt data."""
    return ArchiveConflictView(
        dominators=tuple(
            Dominator(
                round_number=item.round_number,
                metrics=tuple(
                    ConflictMetric(name=m.name, value=m.value, direction=m.direction)
                    for m in item.metrics
                ),
            )
            for item in conflict.dominators
        )
    )


def active_hypothesis(state: MultiStrategyState, config: MultiConfig) -> Hypothesis | None:
    """The hypothesis the open round works on."""
    return ledger_of(state, config).search.active_hypothesis


def _evidence(
    state: MultiStrategyState,
    config: MultiConfig,
    planning: PlanningContext,
    focus: FocusView | None,
) -> PlanEvidence:
    evidence = serial.plan_evidence(config.hypotheses, ledger_of(state, config), planning, focus)
    return evidence.model_copy(
        update={
            "benchmark_enabled": config.benchmark_configured,
            "last_profile": state.design.profile,
        }
    )


def plan_prompt(
    state: MultiStrategyState,
    config: MultiConfig,
    view: RunView,
    planning: PlanningContext,
    focus: FocusView | None = None,
) -> PlanPrompt:
    """The new-hypothesis request: history, the frontier, carried notices and the fresh profile."""
    evidence = _evidence(state, config, planning, focus)
    return PlanPrompt(
        **{name: getattr(evidence, name) for name in PlanEvidence.model_fields},
        base_revision=build_base(state, view),
    )


def pre_round_prompt(
    state: MultiStrategyState,
    config: MultiConfig,
    planning: PlanningContext,
    focus: FocusView | None = None,
) -> PreRoundPrompt:
    """The routing request: is fresh profile evidence wanted before this plan?"""
    evidence = _evidence(state, config, planning, focus)
    return PreRoundPrompt(
        round_number=planning.round_number,
        has_history=bool(evidence.history),
        profiler_kind=config.profiler_id,
        profile_execution=config.profile_execution,
        regression_kind=evidence.regression_kind,
        regression_round=evidence.regression_round,
        exhaustion_feedback=evidence.exhaustion_feedback,
        exhaustion_round=evidence.exhaustion_round,
    )


def profiler_prompt(state: MultiStrategyState, config: MultiConfig) -> ProfilerPrompt:
    """The advisory profiling request for the focus the routing turn chose."""
    number = state.closed + 1
    return ProfilerPrompt(
        round_number=number,
        profiler_kind=config.profiler_id,
        profile_focus=state.design.profile_focus or "general steady-state benchmark hotspots",
        benchmark_command=config.benchmark_command,
        profile_execution=config.profile_execution,
        profiler_support_name=config.profiler_support_name,
        profiler_mcp_name=config.profiler_mcp_name,
        evidence_directory=f"{PROFILE_DIRECTORY}/round-{number:04d}/",
    )


def implement_prompt(
    record: RoundState,
    config: MultiConfig,
    hypothesis: Hypothesis | None,
    focus: FocusView | None,
) -> ImplementPrompt | ContinuePrompt:
    """The implementation request of the open round: first brief, or a bounded continuation."""
    plan = hypothesis_plan(plan_of(record))
    gates = gate_facts(hypothesis)
    retry = record.turns_spent + 1
    step = None if hypothesis is None else hypothesis.next_step
    if step:
        return ContinuePrompt(
            round_number=record.number,
            retry=retry,
            plan=plan,
            base_revision=record.base,
            continuation_step=step,
            framework=gates,
            feedback=record.feedback,
            validation_failure=record.validation_failure,
            no_valid_reply=record.no_valid_reply,
            pareto_archive_conflict=record.conflict,
        )
    return ImplementPrompt(
        round_number=record.number,
        retry=retry,
        plan=plan,
        base_revision=record.base,
        framework=gates,
        feedback=record.feedback,
        validation_failure=record.validation_failure,
        no_valid_reply=record.no_valid_reply,
        pareto_archive_conflict=record.conflict,
        official_due=record.planned_official_reason is not None,
        official_reason=record.planned_official_reason,
        benchmark_enabled=config.benchmark_configured,
        active_component=None if focus is None else (focus.active_component or None),
        reference_path=config.reference_path,
        interface=config.interface,
    )


def judge_prompt(
    record: RoundState,
    config: MultiConfig,
    hypothesis: Hypothesis | None,
    reply: ImplementerResponse,
    cited_evidence: tuple[EvidenceRef, ...],
) -> JudgePrompt:
    """The independent review request for the implementer's latest reply."""
    return JudgePrompt(
        round_number=record.number,
        retry=record.turns_spent,
        plan=hypothesis_plan(plan_of(record)),
        claims=claims_of(reply),
        framework=gate_facts(hypothesis),
        pareto_archive_conflict=record.conflict,
        official_due=record.planned_official_reason is not None,
        official_reason=record.planned_official_reason,
        benchmark_enabled=config.benchmark_configured,
        interface=config.interface,
        cited_evidence=cited_evidence,
    )
