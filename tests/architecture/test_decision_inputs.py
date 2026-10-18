"""A decision field of an agent's reply names its inputs, and its prompt carries them.

Asking an agent to decide from facts its prompt does not show is the failure class behind
#3047: a plan bar over a base nobody supplied, a verdict over evidence nobody rendered. A reply
model marks each decision field with ``DecidedFrom`` (``vibesys.hypothesis``), naming the
fields of the role's prompt context the decision needs.

Three checks hold the declaration:

- every field of a covered reply model that makes a choice (a flag, a number, a closed set)
  declares inputs, unless it is a measurement the agent only reports (``_REPORTED``);
- every declared input is a field of the prompt context the role is asked with;
- each prompt template of the role, rendered with a context that fills every field with a
  distinctive value, contains the value of every input its reply declares.

Another reply model joins ``_COVERED`` when its fields declare inputs; the models the strategies
register and ``_COVERED`` omits are the ones still to do.
"""

from __future__ import annotations

import enum
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Union, get_args, get_origin

import pytest
from pydantic import BaseModel
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS
from tests.vibesys.reply_variables import context_variables

import vibesys.orchestration.multi.core_policy.prompts as multi_templates
from vibesys.hypothesis import OrchestratorPlan, declared_inputs
from vibesys.orchestration.multi.core_policy.api import run_prompt_variables
from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse, PreRoundDecision
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
    multi,
)
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR
from vibesys.serial_search.api import PROMPTS_DIR as SERIAL_SEARCH_PROMPTS
from vibesys.serial_search.api import PassedIdea, PlanPrompt, RoundRow
from vs_core.api import RevisionId, RevisionRef
from vs_issue_tracker.api import RoadmapItem, RoadmapItemKind, RoadmapStatus
from vs_prompts.api import TemplateRenderer, reply_fields

if TYPE_CHECKING:
    from vs_core.api.search import RolePromptContext

_COVERED: tuple[type[BaseModel], ...] = (
    OrchestratorPlan,
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
_REPORTED: dict[tuple[str, str], str] = {
    ("ImplementerResponse", "perf_metric"): (
        "a number the agent copies from a measurement it ran; nothing is decided from facts"
    ),
}

# What each declared input shows in a prompt, by the dotted path the declaration names.
_NEEDLES: dict[str, str] = {
    "input_value": "3001.5",
    "history": "history-idea-a",
    "passed": "passed-idea-b",
    "base_revision": "base-rev-c",
    "roadmap": "roadmap-item-d",
    "plan.pass_criteria": "criteria-e",
    "plan.invariants": "invariants-f",
    "plan.minimum_ratio": "1.37",
    "feedback": "feedback-g",
    "validation_failure": "validation-error-h",
    "pareto_archive_conflict": "conflict-metric-i",
    "claims.status": "status-claim-j",
    "claims.evidence": "claimed-evidence-k",
    "round_number": "round 7",
    "regression_kind": "regression-kind-l",
    "exhaustion_feedback": "exhaustion-feedback-m",
}

_REVISION = RevisionRef(
    revision_id=RevisionId(root=_NEEDLES["base_revision"]), digest="git-commit:base"
)
_PLAN = HypothesisPlan(
    hypothesis_id="hyp",
    hypothesis="claim",
    task="task",
    pass_criteria=_NEEDLES["plan.pass_criteria"],
    invariants=_NEEDLES["plan.invariants"],
    minimum_ratio=float(_NEEDLES["plan.minimum_ratio"]),
)
_FEEDBACK = _NEEDLES["feedback"]
_VALIDATION = ValidationFailure(recipe="r", error=_NEEDLES["validation_failure"])
_CONFLICT = ArchiveConflictView(
    dominators=(
        Dominator(
            round_number=2,
            metrics=(
                ConflictMetric(
                    name=_NEEDLES["pareto_archive_conflict"], value=1.0, direction="max"
                ),
            ),
        ),
    )
)


def _implement(continuation_step: str | None) -> ImplementPrompt:
    return ImplementPrompt(
        round_number=7,
        retry=2,
        plan=_PLAN,
        base_revision=_REVISION,
        framework=GateFacts(),
        continuation_step=continuation_step,
        feedback=_FEEDBACK,
        validation_failure=_VALIDATION,
        pareto_archive_conflict=_CONFLICT,
    )


_CASES: dict[str, tuple[str, RolePromptContext, type[BaseModel]]] = {
    "plan": (
        "plan",
        PlanPrompt(
            round_number=7,
            max_rounds=9,
            base_revision=_REVISION,
            input_value=float(_NEEDLES["input_value"]),
            history=(
                RoundRow(
                    round_number=1,
                    hypothesis_id=_NEEDLES["history"],
                    passed=True,
                    official=True,
                    judge_verdict="pass",
                ),
            ),
            passed=(PassedIdea(round_number=1, idea=_NEEDLES["passed"]),),
            roadmap=(
                RoadmapItem(
                    id="major-1",
                    kind=RoadmapItemKind.MAJOR,
                    title=_NEEDLES["roadmap"],
                    status=RoadmapStatus.TODO,
                ),
            ),
        ),
        OrchestratorPlan,
    ),
    "implement": ("implement", _implement(None), ImplementerResponse),
    "implement a continuation": (
        "implement",
        _implement("step"),
        ImplementerResponse,
    ),
    "judge": (
        "judge",
        JudgePrompt(
            round_number=7,
            retry=2,
            plan=_PLAN,
            claims=ImplementerClaims(
                summary="summary",
                expected_behavior="behavior",
                status=_NEEDLES["claims.status"],
                evidence=_NEEDLES["claims.evidence"],
                candidate_disposition="unassessed",
            ),
            framework=GateFacts(),
        ),
        JudgeResponse,
    ),
    "pre-round": (
        "pre_round",
        PreRoundPrompt(
            round_number=7,
            has_history=True,
            regression_kind=_NEEDLES["regression_kind"],
            regression_round=6,
            exhaustion_feedback=_NEEDLES["exhaustion_feedback"],
            exhaustion_round=5,
        ),
        PreRoundDecision,
    ),
}

_RENDERER = TemplateRenderer(
    Path(multi_templates.__file__).parent,
    fallback_roots=(SERIAL_SEARCH_PROMPTS, CORE_PROTOCOL_DIR, PROMPTS_DIR / "shared"),
)


def _render(template: str, context: RolePromptContext) -> str:
    variables = context_variables(context, multi.reply_schemas)
    run = run_prompt_variables(
        domain_sections=EMPTY_DOMAIN_SECTIONS,
        objective="Raise throughput.",
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
    return str(_RENDERER.render_template(f"{template}.j2", **run, **variables))


def _model_of(annotation: object) -> type[BaseModel] | None:
    """The one model class an annotation names, looking through ``| None`` and ``Annotated``."""
    if get_origin(annotation) is Annotated:
        return _model_of(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        models = [model for arg in get_args(annotation) if (model := _model_of(arg))]
        return models[0] if len(models) == 1 else None
    return (
        annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None
    )


def _resolves(context: type[BaseModel], path: str) -> bool:
    """Whether the dotted ``path`` names a field of ``context``, through nested models."""
    head, _, rest = path.partition(".")
    field = context.model_fields.get(head)
    if field is None:
        return False
    if not rest:
        return True
    nested = _model_of(field.annotation)
    return nested is not None and _resolves(nested, rest)


def _is_a_choice(annotation: object) -> bool:
    """Whether a field of this type picks a flag, a number or a member of a closed set."""
    if get_origin(annotation) is Annotated:
        return _is_a_choice(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        return any(_is_a_choice(arg) for arg in get_args(annotation) if arg is not type(None))
    return isinstance(annotation, type) and issubclass(annotation, bool | int | float | enum.Enum)


@pytest.mark.parametrize("model", _COVERED, ids=lambda model: model.__name__)
def test_every_decision_field_of_a_covered_reply_declares_its_inputs(
    model: type[BaseModel],
) -> None:
    declared = declared_inputs(model)
    missing = [
        name
        for name, field in model.model_fields.items()
        if _is_a_choice(field.annotation)
        and name not in declared
        and (model.__name__, name) not in _REPORTED
    ]

    assert not missing, f"{model.__name__} decision fields declare no inputs: {missing}"


def test_a_reported_measurement_still_exists() -> None:
    models = {model.__name__: model for model in _COVERED}

    assert all(name in models[owner].model_fields for owner, name in _REPORTED)


@pytest.mark.parametrize("name", _CASES)
def test_a_declared_input_is_a_field_of_the_prompt_context(name: str) -> None:
    _, context, reply = _CASES[name]
    declared = declared_inputs(reply)
    unknown = {
        field: path
        for field, paths in declared.items()
        for path in paths
        if not _resolves(type(context), path)
    }

    assert not unknown, f"inputs that are no field of {type(context).__name__}: {unknown}"


@pytest.mark.parametrize("name", _CASES)
def test_the_rendered_prompt_carries_every_declared_input(name: str) -> None:
    template, context, reply = _CASES[name]
    text = _render(template, context)
    absent = {
        field: path
        for field, paths in declared_inputs(reply).items()
        for path in paths
        if _NEEDLES[path] not in text
    }

    assert not absent, f"the {name} prompt does not show declared inputs {absent}"


def test_every_declared_input_has_a_probe() -> None:
    declared = {
        path for model in _COVERED for paths in declared_inputs(model).values() for path in paths
    }

    assert declared <= _NEEDLES.keys()


@pytest.mark.parametrize("name", _CASES)
def test_the_case_asks_the_role_whose_reply_it_checks(name: str) -> None:
    _, context, reply = _CASES[name]
    assert context.reply_schema is not None
    offered = set(reply_fields(multi.reply_schemas[context.reply_schema].model_json_schema()))

    assert set(reply.model_fields) <= offered


def test_every_covered_reply_has_a_case() -> None:
    assert {reply for _, _, reply in _CASES.values()} == set(_COVERED)
