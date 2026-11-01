"""A run's domain pack reaches the role prompts of every strategy, and only that pack does.

Each strategy plans a run through its registered core policy; the test renders the role prompts
(planner, implementer, judge, profiler) with the plan's own run-level variables, the template
roots the policy declares and a context of the strategy's own prompt model. The domain pack
is the only thing that varies, so any text that appears for one domain and not another came
from the pack path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cache
from typing import TYPE_CHECKING

import pytest
from hypothesis import find
from tests.support.prompt_context import context_variables
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    CONTEXTS,
    RENDERER,
    TEMPLATE_FILES,
)
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    context_of,
    plan_of,
    resolved,
    write_project,
)
from tests.vibesys.orchestration.dynamic.prompt_variables import (
    context_variables as dynamic_variables,
)

from vibesys.constants import DomainName
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_sections
from vibesys.orchestration import dynamic, evolve, issue_queue, multi, single
from vibesys.orchestration.dynamic.search.api import PromptTemplate
from vibesys.orchestration.evolve.search import api as evolve_api
from vibesys.orchestration.issue_queue.search import api as plain_api
from vibesys.orchestration.multi.search import api as multi_api
from vibesys.orchestration.single.search import api as single_api
from vibesys.serial_search.api import PlanPrompt
from vs_core.api import RevisionId, RevisionRef
from vs_project.api import OrchestrationDescriptor
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel, JsonValue

    from vibesys.plugin_registration import OrchestrationRegistration
    from vs_runtime.api import CorePlan, OrchestrationPlugin, RunFacts

# The dynamic search's contexts come from hypothesis `find`, 1 to 4 s each and cached per process,
# so spreading this file's tests over the shard's workers repeats them in each: four workers cost
# four times the one that runs the whole file (30 s of CPU against 7 s).
pytestmark = pytest.mark.xdist_group("domain-sections-in-prompts")

_RolePrompts = tuple[tuple[DomainRole, "BaseModel"], ...]
_CRITERIA = "criteria"
_REV = RevisionRef(revision_id=RevisionId(root="a1"), digest="git-commit:a1")
_ROUND: dict[str, JsonValue] = {
    "interface": "inprocess",
    "judge_every": 1,
    "max_retries_per_round": 1,
}
_ROUNDS: dict[str, JsonValue] = {**_ROUND, "max_rounds": 2}
_SINGLE: dict[str, JsonValue] = {k: v for k, v in _ROUNDS.items() if k != "judge_every"}
_EVOLVE_PROFILER = {
    "profiler_kind": "torch",
    "profile_execution": "local",
    "profiler_support_name": "torch_profiler",
    "profiler_mcp_name": "vibesys-torch-profiler",
}
_EVOLVE: dict[str, JsonValue] = {
    "bootstrap_max_attempts": 1,
    "children_per_generation": 1,
    "frontier_bias": 0.5,
    "k_random_inspirations": 1,
    "k_top_inspirations": 1,
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
    # Eager, or a callable run on first use: the dynamic search's contexts come from
    # hypothesis `find`, which takes seconds, and a module-level call would be paid by
    # every xdist worker while it collects, before the first test starts.
    role_prompts: _RolePrompts | Callable[[], _RolePrompts]

    @property
    def prompts(self) -> _RolePrompts:
        return self.role_prompts if isinstance(self.role_prompts, tuple) else self.role_prompts()

    @property
    def plugin(self) -> OrchestrationPlugin:
        return self.registration.plugin


@cache
def _example(template: PromptTemplate) -> BaseModel:
    """The simplest context the dynamic search generates for ``template``."""
    return find(CONTEXTS[template], lambda _: True)


_STRATEGIES = (
    _Strategy(
        "single",
        single.REGISTRATION,
        _SINGLE,
        (
            (
                DomainRole.ORCHESTRATOR,
                PlanPrompt(round_number=1, max_rounds=2, base_revision=_REV),
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
                PlanPrompt(round_number=1, max_rounds=2, base_revision=_REV),
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
                        status="implemented",
                        candidate_disposition="unassessed",
                    ),
                    framework=multi_api.GateFacts(),
                ),
            ),
            (
                DomainRole.PROFILER,
                multi_api.ProfilerPrompt(
                    round_number=1,
                    profile_focus="hot path",
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
                evolve_api.ProfilerPrompt(candidate=_REV),
            ),
        ),
    ),
    _Strategy(
        "dynamic",
        dynamic.REGISTRATION,
        _ROUNDS | {"max_in_flight": 2},
        lambda: (
            (DomainRole.ORCHESTRATOR, _example(PromptTemplate.PORTFOLIO)),
            (DomainRole.IMPLEMENTER, _example(PromptTemplate.IMPLEMENT)),
            (DomainRole.JUDGE, _example(PromptTemplate.REVIEW)),
            (DomainRole.PROFILER, _example(PromptTemplate.PROFILE_REQUEST)),
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


def _sections(domain: DomainName, profiler_id: str = "none") -> dict[DomainRole, str]:
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
        profiler_id=profiler_id,
        workspace_sources=(),
    )


def _plan(strategy: _Strategy, domain: DomainName, root: Path) -> CorePlan:
    write_project(root)
    if strategy.name == "dynamic":
        run = resolved(root)
        return plan_of(replace(run, facts=run.facts.model_copy(update={"domain_id": domain.value})))
    run = resolved(root, profiler_id="torch" if strategy.name == "multi" else "none")
    facts: RunFacts = run.facts.model_copy(update={"domain_id": domain.value})
    descriptor = OrchestrationDescriptor(
        id=strategy.plugin.id,
        config_version=strategy.plugin.config_version,
        options=strategy.options,
    )
    assert strategy.plugin.core is not None
    context = context_of(replace(run, facts=facts))
    return strategy.plugin.core.plan(
        replace(context, options=strategy.registration.parse_options(descriptor))
    )


def _render(strategy: _Strategy, plan: CorePlan, context: BaseModel) -> str:
    if strategy.name == "dynamic":
        template = PromptTemplate(context.model_dump()["template"])
        return RENDERER.render_template(
            TEMPLATE_FILES[template], **plan.prompt_variables, **dynamic_variables(context)
        )
    core = strategy.plugin.core
    assert core is not None
    renderer = TemplateRenderer(core.prompt_templates, fallback_roots=core.prompt_fallback_roots)
    searches = {
        "plain": plan.search,
        "single": single_api.single,
        "multi": multi_api.multi,
        "evolve": evolve_api.evolve,
    }
    schemas = (
        searches[strategy.name].reply_schemas
        if strategy.name in searches
        else plan.search.reply_schemas
    )
    fields = context_variables(context, schemas)
    # The host supplies the run's profiler to every prompt of the evolve search (`evolve_core`).
    host = _EVOLVE_PROFILER if strategy.name == "evolve" else {}
    return renderer.render_template(
        f"{context.model_dump()['template']}.j2", **host, **plan.prompt_variables, **fields
    )


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
    profiler_id = "torch" if strategy.name == "multi" else "none"
    own = _sections(domain, profiler_id)
    foreign = {
        _own_text(text)
        for other in _DOMAINS
        if other is not domain
        for text in _sections(other, profiler_id).values()
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
