"""The prompts the decision-input check renders: one context per role whose reply declares inputs.

Each case fills every field its role's reply names as an input with a distinctive value and
records what that value looks like in the rendered prompt, so the check can find it. A case
renders the way the render operation does: the strategy's own template directories, its
run-level variables and, for cited evidence, a ledger that holds the trusted record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.rendered_prompts import REVISION, Held, PromptRender, trusted_record
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS

import vibesys.orchestration.dynamic.core_policy.api as dynamic_policy
import vibesys.orchestration.evolve.core_policy.api as evolve_policy
import vibesys.orchestration.multi.core_policy.api as multi_policy
import vibesys.orchestration.single.core_policy.api as single_policy
from launch import built_in_orchestrations
from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.search.api import (
    EvidenceCitation,
    MergeableRow,
    MergePrompt,
    MergeSource,
    OutlinePrompt,
    PlannerPrompt,
    ProfilePrompt,
    ReviewPrompt,
    WorkKind,
)
from vibesys.orchestration.dynamic.search.api import ImplementPrompt as DynamicImplementPrompt
from vibesys.orchestration.dynamic.search.api import ReviewEvaluation as DynamicEvaluation
from vibesys.orchestration.evolve.search.api import JudgePrompt as EvolveJudgePrompt
from vibesys.orchestration.issue_queue.search.api import (
    IssueBrief,
    IssueDetail,
    PerformancePrompt,
    PriorPerformance,
)
from vibesys.orchestration.issue_queue.search.api import ReviewEvaluation as IssueEvaluation
from vibesys.orchestration.issue_queue.search.api import ReviewPrompt as IssueReviewPrompt
from vibesys.orchestration.multi.search.api import (
    ArchiveConflictView,
    ConflictMetric,
    Dominator,
    GateFacts,
    HypothesisPlan,
    ImplementerClaims,
    ImplementPrompt,
    JudgePrompt,
    PreRoundPrompt,
    ValidationFailure,
)
from vibesys.orchestration.single.search.api import RoundPrompt
from vibesys.serial_search.api import MeasurementNoise, PassedIdea, PlanPrompt, RoundRow
from vs_core.api import EvidenceId, EvidenceKind, ObservationStatus, RevisionId, RevisionRef
from vs_issue_tracker.api import RoadmapItem, RoadmapItemKind, RoadmapStatus
from vs_runtime.api import RunFacts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import SearchDef
    from vs_core.api.search import RolePromptContext

_ORCHESTRATION = Path(__file__).parents[2].resolve() / "src" / "vibesys" / "orchestration"
_OBJECTIVE = "Raise throughput."


@dataclass(frozen=True)
class Case:
    """One prompt of a role, rendered with a context whose declared inputs hold known values."""

    search: str
    role: str
    context: RolePromptContext
    needles: Mapping[str, str]
    """What each declared input shows in the prompt, by the dotted path the declaration names."""
    held: Held | None = None
    """The trusted record the evidence ledger holds for the evidence the context cites."""


def orchestration_folder(search: SearchDef[Any]) -> str:
    """The folder of ``vibesys.orchestration`` that owns ``search``."""
    return search.root.__module__.split(".")[2]


def registered_folders() -> set[str]:
    """The orchestration folders of the built-in orchestrations' core policies."""
    return {
        Path(registration.plugin.core.prompt_templates)
        .resolve()
        .relative_to(_ORCHESTRATION)
        .parts[0]
        for registration in built_in_orchestrations().registrations()
    }


def renderer(search: SearchDef[Any]) -> PromptRender:
    """How ``search`` renders a prompt: its core policy's template roots and run variables."""
    folder = orchestration_folder(search)
    core = next(
        registration.plugin.core
        for registration in built_in_orchestrations().registrations()
        if Path(registration.plugin.core.prompt_templates)
        .resolve()
        .relative_to(_ORCHESTRATION)
        .parts[0]
        == folder
    )
    roots = (Path(core.prompt_templates), *core.prompt_fallback_roots)
    return PromptRender(roots, search.reply_schemas, _RUN_VARIABLES[search.name]())


def _multi_variables() -> dict[str, object]:
    return multi_policy.run_prompt_variables(
        domain_sections=EMPTY_DOMAIN_SECTIONS,
        objective=_OBJECTIVE,
        environment_notes="note",
        agent_evaluation=True,
        validation_recipe_schema='{"version": 1}',
        profiler_id="linux_cpu",
        profile_execution="local",
        profiler_support_name="support",
        profiler_mcp_name="profiler",
        benchmark_command="bench",
        reference_path=".",
        interface="inprocess",
    )


def _single_variables() -> dict[str, object]:
    return single_policy.run_prompt_variables(
        domain_sections=EMPTY_DOMAIN_SECTIONS,
        objective=_OBJECTIVE,
        environment_notes="note",
        agent_evaluation=True,
        profiler_id="none",
    )


def _dynamic_variables() -> dict[str, object]:
    return dynamic_policy.run_prompt_variables(
        domain_sections=EMPTY_DOMAIN_SECTIONS,
        objective=_OBJECTIVE,
        environment_notes="note",
        agent_evaluation=True,
        agent_git_writable=True,
    )


def _evolve_variables() -> dict[str, object]:
    return evolve_policy.run_prompt_variables(
        domain_sections=EMPTY_DOMAIN_SECTIONS,
        objective=_OBJECTIVE,
        environment_notes="note",
        agent_evaluation=True,
        run_facts=evolve_policy.run_facts_variables(
            interface="inprocess",
            reference_path=".",
            profiler_id="none",
            profile_execution="local",
            benchmark_command=None,
        ),
    )


def _issue_queue_variables() -> dict[str, object]:
    facts = RunFacts(agent_git_writable=True, domain_id="generic", objective=_OBJECTIVE)
    return {
        "facts": facts,
        "objective": facts.objective,
        "agent_evaluation": True,
        "domain_sections": EMPTY_DOMAIN_SECTIONS,
    }


_RUN_VARIABLES = {
    "multi": _multi_variables,
    "single": _single_variables,
    "dynamic": _dynamic_variables,
    "evolve": _evolve_variables,
    "plain": _issue_queue_variables,
}


def _revision(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")


_CRITERIA = "criteria-e"
_IMPLEMENT_CRITERIA = "criteria-m1"
_REVIEW_CRITERIA = "criteria-n1"
_BASE = _revision("base-rev-c")
_EVIDENCE = trusted_record(1.0, "calls/s", "max", passed=True, evidence_id="e" * 64)
_PLAN = HypothesisPlan(
    hypothesis_id="hyp",
    hypothesis="claim",
    task="task",
    pass_criteria=_CRITERIA,
    invariants="invariants-f",
    minimum_ratio=1.37,
)
_DESIGNER = PlanPrompt(
    round_number=7,
    max_rounds=9,
    base_revision=_BASE,
    input_value=3001.5,
    measurement_noise=MeasurementNoise(
        noise=0.0314, resolution=0.0628, candidate_samples=2, base_samples=3
    ),
    history=(
        RoundRow(
            round_number=1,
            hypothesis_id="history-idea-a",
            passed=True,
            official=True,
            judge_verdict="pass",
        ),
    ),
    passed=(PassedIdea(round_number=1, idea="passed-idea-b"),),
    roadmap=(
        RoadmapItem(
            id="major-1",
            kind=RoadmapItemKind.MAJOR,
            title="roadmap-item-d",
            status=RoadmapStatus.TODO,
        ),
    ),
)
_DESIGNER_NEEDLES = {
    "input_value": "3001.5",
    "measurement_noise": "3.14%",
    "history": "history-idea-a",
    "passed": "passed-idea-b",
    "base_revision": "base-rev-c",
    "roadmap": "roadmap-item-d",
}


def _implement(continuation_step: str | None) -> ImplementPrompt:
    return ImplementPrompt(
        round_number=7,
        retry=2,
        plan=_PLAN,
        base_revision=_BASE,
        framework=GateFacts(),
        continuation_step=continuation_step,
        feedback="feedback-g",
        validation_failure=ValidationFailure(recipe="r", error="validation-error-h"),
        pareto_archive_conflict=ArchiveConflictView(
            dominators=(
                Dominator(
                    round_number=2,
                    metrics=(ConflictMetric(name="conflict-metric-i", value=1.0, direction="max"),),
                ),
            )
        ),
    )


_IMPLEMENT_NEEDLES = {
    "plan.pass_criteria": _CRITERIA,
    "feedback": "feedback-g",
    "validation_failure": "validation-error-h",
    "plan.minimum_ratio": "1.37",
    "pareto_archive_conflict": "conflict-metric-i",
}
_SINGLE_ROUND = RoundPrompt(
    round_number=7,
    retry=2,
    hypothesis_id="hyp",
    hypothesis="claim",
    task="task",
    pass_criteria=_CRITERIA,
    invariants="invariants-f",
    minimum_ratio=1.37,
    base_revision=_BASE,
    dominated_by_rounds=(31415, 27182),
)
_PLANNER = PlannerPrompt.model_validate(
    {
        "capacity": 7,
        "in_flight": 1,
        "remaining": 13,
        "base_revision": _BASE,
        "base_accuracy_passed": None,
        "offer_snapshot": "snapshot",
        "main_revision": _BASE,
        "mergeable": (MergeableRow(hypothesis_id="mergeable-hyp-d5", revision=_revision("m1")),),
        "buildable": (
            {
                "option_id": "option-c4",
                "hypothesis_id": "buildable-hyp",
                "revision": _revision("b1"),
                "accuracy_evidence": EvidenceId(root="accuracy-ev"),
                "submission_index": 0,
                "latest_verified": True,
                "best_partial": False,
            },
        ),
        "history": (
            {
                "hypothesis_id": "history-hyp-a2",
                "sequence": 1,
                "title": "t",
                "status": "finished",
                "strategy": "available",
            },
        ),
        "baseline": (MetricRow(name="baseline-metric-b3", value=1.0, direction="max"),),
        "evaluations": {
            "pick": "ranked",
            "slots": 2,
            "input_holds_slot": False,
            "waiting": (
                {
                    "work_id": "waiting-work-e6",
                    "kind": WorkKind.IMPLEMENT,
                    "revision": _revision("w1"),
                },
            ),
        },
    }
)
_ISSUE = IssueDetail(
    id=7,
    type="feature",
    status="in_progress",
    title="Implement streaming",
    attempts=1,
    description="acceptance-criteria-k1",
)
_BOARD = (IssueBrief(id=8, type="bug", status="open", title="board-issue-k2", attempts=0),)


def _dynamic_evaluation(evidence_id: str) -> DynamicEvaluation:
    return DynamicEvaluation(
        evidence_id=EvidenceId(root=evidence_id),
        kind=EvidenceKind.CORRECTNESS,
        revision=_revision("cand-rev"),
        status=ObservationStatus.SUCCEEDED,
        passed=True,
    )


def cases() -> dict[str, Case]:
    """The prompts to check, by name."""
    cited = (_EVIDENCE[0],)
    return {
        "multi designer": Case("multi", "orchestrator", _DESIGNER, _DESIGNER_NEEDLES),
        "multi implementer": Case("multi", "implementer", _implement(None), _IMPLEMENT_NEEDLES),
        "multi implementer continuation": Case(
            "multi", "implementer", _implement("step"), _IMPLEMENT_NEEDLES
        ),
        "multi judge": Case(
            "multi",
            "judge",
            JudgePrompt(
                round_number=7,
                retry=2,
                plan=_PLAN,
                claims=ImplementerClaims(
                    summary="summary",
                    expected_behavior="behavior",
                    status="status-claim-j",
                    evidence="claimed-evidence-k",
                    candidate_disposition="unassessed",
                ),
                framework=GateFacts(),
                cited_evidence=cited,
            ),
            {
                "plan.pass_criteria": _CRITERIA,
                "plan.invariants": "invariants-f",
                "claims.status": "status-claim-j",
                "claims.evidence": "claimed-evidence-k",
                "cited_evidence": "e" * 64,
            },
            held=_EVIDENCE,
        ),
        "multi router": Case(
            "multi",
            "router",
            PreRoundPrompt(
                round_number=7,
                base_revision=_revision("base-revision-n"),
                base_round=4,
                base_value=173.25,
                has_history=True,
                regression_kind="regression-kind-l",
                regression_round=6,
                exhaustion_feedback="exhaustion-feedback-m",
                exhaustion_round=5,
            ),
            {
                "round_number": "round 7",
                "base_revision": "base-revision-n",
                "base_value": "173.25",
                "regression_kind": "regression-kind-l",
                "exhaustion_feedback": "exhaustion-feedback-m",
            },
        ),
        "single designer": Case("single", "orchestrator", _DESIGNER, _DESIGNER_NEEDLES),
        "single implementer": Case(
            "single",
            "implementer",
            _SINGLE_ROUND,
            {
                "pass_criteria": _CRITERIA,
                "invariants": "invariants-f",
                "minimum_ratio": "1.37",
                "dominated_by_rounds": "31415, 27182",
            },
        ),
        "dynamic planner": Case(
            "dynamic",
            "orchestrator",
            _PLANNER,
            {
                "capacity": "at most 7 new",
                "remaining": "13 more",
                "buildable": "option-c4",
                "mergeable": "mergeable-hyp-d5",
                "history": "history-hyp-a2",
                "baseline": "baseline-metric-b3",
                "evaluations": "waiting-work-e6",
            },
        ),
        "dynamic implementer": Case(
            "dynamic",
            "implementer",
            DynamicImplementPrompt(
                hypothesis_id="hyp",
                hypothesis="claim",
                task="task",
                pass_criteria=_IMPLEMENT_CRITERIA,
                parent_revision=_BASE,
                evidence=(EvidenceCitation(location="loc-m2"),),
                rejection="rejection-m3",
                prior_failure_tail="failure-tail-m4",
            ),
            {
                "pass_criteria": _IMPLEMENT_CRITERIA,
                "evidence": "loc-m2",
                "rejection": "rejection-m3",
                "prior_failure_tail": "failure-tail-m4",
            },
        ),
        "dynamic judge": Case(
            "dynamic",
            "judge",
            ReviewPrompt(
                hypothesis_id="hyp",
                hypothesis="claim",
                pass_criteria=_REVIEW_CRITERIA,
                candidate=_revision("cand-rev"),
                summary="summary-n2",
                evidence=(EvidenceCitation(location="loc-n3"),),
                evaluations=(_dynamic_evaluation("evaluation-n4"),),
            ),
            {
                "pass_criteria": _REVIEW_CRITERIA,
                "summary": "summary-n2",
                "evidence": "loc-n3",
                "evaluations": "evaluation-n4",
            },
        ),
        "dynamic profiler": Case(
            "dynamic",
            "profiler",
            ProfilePrompt(question="question-p1", required_fields=("field-p2",), target=_BASE),
            {"question": "question-p1", "required_fields": "field-p2"},
        ),
        "dynamic merger": Case(
            "dynamic",
            "merger",
            MergePrompt(
                merge_id="merge",
                main_revision=_BASE,
                candidates=(MergeSource(hypothesis_id="merge-hyp-q1", revision=_revision("c1")),),
                instructions="instructions-q2",
                watch_for="watch-q3",
            ),
            {
                "candidates": "merge-hyp-q1",
                "instructions": "instructions-q2",
                "watch_for": "watch-q3",
            },
        ),
        "dynamic outliner": Case(
            "dynamic",
            "outliner",
            OutlinePrompt(documents=("docs/plan-r1.md",)),
            {"documents": "docs/plan-r1.md"},
        ),
        "evolve judge": Case(
            "evolve",
            "judge",
            EvolveJudgePrompt(candidate=REVISION, cited_evidence=cited),
            {"cited_evidence": "e" * 64},
            held=_EVIDENCE,
        ),
        "issue queue judge": Case(
            "plain",
            "judge",
            IssueReviewPrompt(
                issue=_ISSUE,
                board=_BOARD,
                summary="summary-k4",
                self_check="self-check-k5",
                evaluations=(
                    IssueEvaluation(
                        evidence_id=EvidenceId(root="evaluation-k6"),
                        kind=EvidenceKind.CORRECTNESS,
                        revision=_revision("cand-rev"),
                        status=ObservationStatus.SUCCEEDED,
                        passed=True,
                    ),
                ),
            ),
            {
                "issue.description": "acceptance-criteria-k1",
                "summary": "summary-k4",
                "self_check": "self-check-k5",
                "evaluations": "evaluation-k6",
                "board": "board-issue-k2",
            },
        ),
        "issue queue perf evaluator": Case(
            "plain",
            "perf-evaluator",
            PerformancePrompt(
                iteration=2,
                max_new_issues=3,
                board=_BOARD,
                prior=(
                    PriorPerformance(
                        iteration=1,
                        throughput_trend="improved",
                        latency_trend="mixed",
                        metrics_json="prior-metrics-k3",
                        new_issues=2,
                    ),
                ),
            ),
            {
                "board": "board-issue-k2",
                "max_new_issues": "most 3 non-duplicate",
                "prior": "prior-metrics-k3",
            },
        ),
    }
