"""A run's domain pack reaches the role prompts of every strategy, and only that pack does.

Each strategy plans a run through its registered core policy; the test renders the role prompts
(planner, implementer, judge, profiler) with the plan's own run-level variables, the template
roots the policy declares and a context of the strategy's own prompt model. The domain pack
is the only thing that varies, so any text that appears for one domain and not another came
from the pack path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    COMMIT,
    bounds_of,
    resolved,
    run_request,
)

from vibesys.constants import DomainName
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_sections
from vibesys.orchestration import dynamic, evolve, issue_queue, multi, single
from vibesys.orchestration.dynamic.strategy import api as dynamic_api
from vibesys.orchestration.evolve.strategy import api as evolve_api
from vibesys.orchestration.issue_queue.strategy import api as plain_api
from vibesys.orchestration.multi.strategy import api as multi_api
from vibesys.orchestration.single.strategy import api as single_api
from vs_core.api import RevisionId, RevisionRef
from vs_project.api import OrchestrationDescriptor
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import CorePlan, CoreRunContext

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel, JsonValue

    from vibesys.plugin_registration import OrchestrationRegistration
    from vs_runtime.api import OrchestrationPlugin, RunFacts

_CRITERIA = "criteria"
_REV = RevisionRef(revision_id=RevisionId(root="a1"), digest="git-commit:a1")
_ROUND: dict[str, JsonValue] = {
    "interface": "inprocess",
    "judge_every": 1,
    "max_retries_per_round": 1,
}
_ROUNDS: dict[str, JsonValue] = {**_ROUND, "max_rounds": 2, "official_eval_every": 1}
_EVOLVE: dict[str, JsonValue] = {
    "bootstrap_max_attempts": 1,
    "children_per_generation": 1,
    "frontier_bias": 0.5,
    "k_random_inspirations": 1,
    "k_top_inspirations": 1,
    "keep_deployments": False,
    "max_generations": 1,
    "max_parallelism": 1,
    "selection_temperature": 1.0,
}
_PLAIN: dict[str, JsonValue] = {
    "max_attempts_per_issue": 2,
    "max_issues_per_perf_eval": 2,
    "max_rounds": 2,
}
_PLAN = multi_api.HypothesisPlan(
    hypothesis_id="h1", hypothesis="claim", task="task", pass_criteria=_CRITERIA
)
_ISSUE = plain_api.IssueDetail(
    id=1, type="feature", status="open", title="t", attempts=0, description="d"
)


@dataclass(frozen=True)
class _Strategy:
    """One registered strategy, its options, and the role prompts it requests."""

    name: str
    registration: OrchestrationRegistration
    options: dict[str, JsonValue]
    prompts: tuple[tuple[DomainRole, BaseModel], ...]

    @property
    def plugin(self) -> OrchestrationPlugin:
        return self.registration.plugin


_STRATEGIES = (
    _Strategy(
        "single",
        single.REGISTRATION,
        _ROUNDS,
        (
            (
                DomainRole.ORCHESTRATOR,
                single_api.PlanPrompt(round_number=1, max_rounds=2, base_revision=_REV),
            ),
            (
                DomainRole.SINGLE_AGENT,
                single_api.RoundPrompt(
                    round_number=1,
                    retry=1,
                    hypothesis_id="h1",
                    hypothesis="claim",
                    task="task",
                    pass_criteria=_CRITERIA,
                    base_revision=_REV,
                ),
            ),
        ),
    ),
    _Strategy(
        "multi",
        multi.REGISTRATION,
        _ROUNDS,
        (
            (
                DomainRole.ORCHESTRATOR,
                multi_api.PlanPrompt(round_number=1, max_rounds=2, base_revision=_REV),
            ),
            (
                DomainRole.IMPLEMENTER,
                multi_api.ImplementPrompt(
                    round_number=1,
                    retry=1,
                    plan=_PLAN,
                    base_revision=_REV,
                    framework=multi_api.GateFacts(),
                ),
            ),
            (
                DomainRole.JUDGE,
                multi_api.JudgePrompt(
                    round_number=1,
                    retry=1,
                    plan=_PLAN,
                    claims=multi_api.ImplementerClaims(
                        summary="s",
                        expected_behavior="e",
                        hypothesis_outcome="nominated",
                        candidate_disposition="unassessed",
                    ),
                    framework=multi_api.GateFacts(),
                ),
            ),
            (
                DomainRole.PROFILER,
                multi_api.ProfilerPrompt(
                    round_number=1,
                    profiler_kind="torch",
                    profile_focus="hot path",
                    profile_execution="local",
                    profiler_support_name="torch_profiler",
                    profiler_mcp_name="vibesys-torch-profiler",
                    evidence_directory="profile",
                ),
            ),
        ),
    ),
    _Strategy(
        "evolve",
        evolve.REGISTRATION,
        _EVOLVE,
        (
            (
                DomainRole.IMPLEMENTER,
                evolve_api.MutatePrompt(base_revision=_REV, is_cold_start=True),
            ),
            (DomainRole.JUDGE, evolve_api.JudgePrompt(candidate=_REV)),
            (
                DomainRole.PROFILER,
                evolve_api.ProfilerPrompt(
                    candidate=_REV,
                    profiler_kind="torch",
                    profile_execution="local",
                    profiler_support_name="torch_profiler",
                    profiler_mcp_name="vibesys-torch-profiler",
                ),
            ),
        ),
    ),
    _Strategy(
        "dynamic",
        dynamic.REGISTRATION,
        _ROUNDS | {"max_in_flight": 2},
        (
            (
                DomainRole.ORCHESTRATOR,
                dynamic_api.PlannerPrompt(
                    capacity=1,
                    in_flight=0,
                    remaining=2,
                    base_revision=_REV,
                    base_accuracy_passed=None,
                    offer_snapshot="offer",
                ),
            ),
            (
                DomainRole.IMPLEMENTER,
                dynamic_api.ImplementPrompt(
                    hypothesis_id="h1",
                    hypothesis="claim",
                    task="task",
                    pass_criteria=_CRITERIA,
                    parent_revision=_REV,
                ),
            ),
            (
                DomainRole.JUDGE,
                dynamic_api.ReviewPrompt(
                    hypothesis_id="h1",
                    hypothesis="claim",
                    pass_criteria=_CRITERIA,
                    candidate=_REV,
                    summary="s",
                ),
            ),
            (
                DomainRole.PROFILER,
                dynamic_api.ProfilePrompt(question="where is the time going?", target=_REV),
            ),
        ),
    ),
    _Strategy(
        "plain",
        issue_queue.REGISTRATION,
        _PLAIN,
        (
            (DomainRole.IMPLEMENTER, plain_api.ImplementPrompt(issue=_ISSUE)),
            (DomainRole.JUDGE, plain_api.ReviewPrompt(issue=_ISSUE, summary="s", self_check="")),
            (
                DomainRole.PROFILER,
                plain_api.PerformancePrompt(iteration=1, max_new_issues=2),
            ),
        ),
    ),
)
_DOMAINS = tuple(DomainName)


def _sections(domain: DomainName) -> dict[DomainRole, str]:
    """What the pack of ``domain`` renders for the plan's default context."""
    return render_domain_sections(
        resolve_domain(domain),
        modality=None,
        interface="inprocess",
        reference_path=".",
        benchmark_command=None,
        accuracy_command=None,
        runtime_notes="",
        profile_execution="local",
        workspace_sources=(),
    )


def _plan(strategy: _Strategy, domain: DomainName, root: Path) -> CorePlan:
    request = run_request(root)
    run = resolved(root)
    facts: RunFacts = run.facts.model_copy(update={"domain_id": domain.value})
    descriptor = OrchestrationDescriptor(
        id=strategy.plugin.id,
        config_version=strategy.plugin.config_version,
        options=strategy.options,
    )
    bounds = bounds_of(request)
    assert strategy.plugin.core is not None
    return strategy.plugin.core.plan(
        CoreRunContext(
            run_id="policy",
            options=strategy.registration.parse_options(descriptor),
            facts=facts,
            baseline=RevisionRef.of_git_commit(COMMIT),
            evaluation_plan=run.evaluation_plan,
            environment=replace(
                run.environment,
                env_kind="docker",
                cli_sandboxed=True,
                share_agent_session=False,
            ),
            artifacts=run.artifacts,
            evaluation_capacity=1,
            queue_allowance_seconds=bounds.queue_allowance_seconds,
            observe_interval_seconds=bounds.observe_interval_seconds,
            observe_backoff_cap_seconds=bounds.observe_backoff_cap_seconds,
            max_run_seconds=bounds.max_run_seconds,
        )
    )


def _render(strategy: _Strategy, plan: CorePlan, context: BaseModel) -> str:
    core = strategy.plugin.core
    assert core is not None
    renderer = TemplateRenderer(core.prompt_templates, fallback_roots=core.prompt_fallback_roots)
    fields = {name: getattr(context, name) for name in type(context).model_fields}
    template = str(fields.pop("template"))
    return renderer.render_template(f"{template}.j2", **plan.prompt_variables, **fields)


def _own_text(text: str) -> str:
    """A pack section's first line: enough to recognise the section in a prompt."""
    return text.strip().splitlines()[0]


def test_the_cross_product_is_not_vacuous() -> None:
    """Every role the strategies render is addressed by a pack that exists and says something."""
    assert {s.name for s in _STRATEGIES} == {
        "single",
        "multi",
        "evolve",
        "dynamic",
        "plain",
    }
    serving = _sections(DomainName.LLM_SERVING)
    assert all(serving[role] for role in (DomainRole.IMPLEMENTER, DomainRole.JUDGE))
    assert not any(_sections(DomainName.GENERIC).values())


@pytest.mark.parametrize("strategy", _STRATEGIES, ids=lambda item: item.name)
@pytest.mark.parametrize("domain", _DOMAINS, ids=lambda item: item.value)
def test_every_role_prompt_carries_its_domains_section_and_no_other_domains_text(
    strategy: _Strategy, domain: DomainName, tmp_path: Path
) -> None:
    plan = _plan(strategy, domain, tmp_path)
    own = _sections(domain)
    foreign = {
        _own_text(text)
        for other in _DOMAINS
        if other is not domain
        for text in _sections(other).values()
        if text
    } - {_own_text(text) for text in own.values() if text}
    for role, context in strategy.prompts:
        prompt = _render(strategy, plan, context)
        if own[role]:
            assert own[role] in prompt, f"{strategy.name} {role}: {domain.value} section missing"
        for text in foreign:
            assert text not in prompt, f"{strategy.name} {role}: foreign text {text!r}"


@pytest.mark.parametrize("strategy", _STRATEGIES, ids=lambda item: item.name)
def test_a_domain_without_prompt_files_contributes_no_section(
    strategy: _Strategy, tmp_path: Path
) -> None:
    plan = _plan(strategy, DomainName.GENERIC, tmp_path)
    assert plan.prompt_variables["domain_sections"] == {role.value: "" for role in DomainRole}


def test_an_llm_serving_plain_run_gives_its_implementer_the_serving_contract(
    tmp_path: Path,
) -> None:
    strategy = next(item for item in _STRATEGIES if item.name == "plain")
    plan = _plan(strategy, DomainName.LLM_SERVING, tmp_path)
    prompt = _render(strategy, plan, plain_api.ImplementPrompt(issue=_ISSUE))
    assert "LLM-serving implementation invariants" in prompt
