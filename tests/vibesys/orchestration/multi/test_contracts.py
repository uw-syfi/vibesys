"""Properties of the multi policy's typed turn and prompt contracts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.orchestration.multi.contracts import (
    ImplementerContext,
    ImplementerContinuationContext,
    ImplementerResponse,
    JudgeContext,
    JudgeResponse,
    PlanContext,
    PreRoundContext,
    PreRoundDecision,
    ProfilerContext,
)
from vibesys.orchestration.multi.prompts import (
    PROMPT_DIR,
    render_continuation_prompt,
    render_implementer_prompt,
    render_judge_prompt,
    render_plan_prompt,
    render_pre_round_prompt,
    render_profiler_prompt,
)
from vibesys.prompts import PROMPTS_DIR
from vs_prompts.api import resolve_free_variables

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

_SEARCH_ROOTS = (PROMPT_DIR, PROMPTS_DIR / "shared", PROMPTS_DIR)
_MODALITY_ROOT = PROMPTS_DIR / "shared" / "_modality"
_DYNAMIC_MODALITY_RE = re.compile(r'_modality/"\s*~\s*modality\s*~\s*"/(\w+)\.j2')

_REPLY_TYPES = (ImplementerResponse, JudgeResponse, PreRoundDecision)
_CONTEXT_TEMPLATES: dict[type[BaseModel], tuple[str, ...]] = {
    PlanContext: ("orchestrator_plan_prompt.j2",),
    ImplementerContext: ("implementer_prompt.j2",),
    ImplementerContinuationContext: ("implementer_continuation_prompt.j2",),
    JudgeContext: ("judge_prompt.j2",),
    PreRoundContext: ("orchestrator_pre_round_prompt.j2",),
    ProfilerContext: tuple(
        path.relative_to(PROMPT_DIR).as_posix()
        for path in sorted((PROMPT_DIR / "profilers").glob("*.j2"))
    ),
}
_RENDERERS: dict[type[BaseModel], Callable[[BaseModel], str]] = {
    PlanContext: cast("Callable[[BaseModel], str]", render_plan_prompt),
    ImplementerContext: cast("Callable[[BaseModel], str]", render_implementer_prompt),
    ImplementerContinuationContext: cast("Callable[[BaseModel], str]", render_continuation_prompt),
    JudgeContext: cast("Callable[[BaseModel], str]", render_judge_prompt),
    PreRoundContext: cast("Callable[[BaseModel], str]", render_pre_round_prompt),
    ProfilerContext: cast(
        "Callable[[BaseModel], str]",
        lambda context: render_profiler_prompt("nsys", context),
    ),
}


def _free_variables(template: str) -> frozenset[str]:
    path = PROMPT_DIR / template
    free, unresolved = resolve_free_variables(path, search_roots=_SEARCH_ROOTS)
    result = set(free)
    if unresolved:
        source = path.read_text()
        for match in _DYNAMIC_MODALITY_RE.finditer(source):
            suffix = match.group(1)
            for variant in sorted(_MODALITY_ROOT.glob(f"*/{suffix}.j2")):
                variant_free, _ = resolve_free_variables(variant, search_roots=_SEARCH_ROOTS)
                result.update(variant_free)
    return frozenset(result)


def _context_strategy(model: type[BaseModel]) -> st.SearchStrategy[BaseModel]:
    overrides: dict[str, st.SearchStrategy[object]] = {}
    if model is PlanContext:
        overrides["profiler_summary"] = st.none() | st.builds(
            ProfilerSummary,
            metrics=st.just({}),
        )
    if "modality" in model.model_fields:
        overrides["modality"] = st.none()
    return st.builds(model, **overrides)


@pytest.mark.parametrize("reply_type", _REPLY_TYPES, ids=lambda value: value.__name__)
def test_reply_json_round_trip(reply_type: type[BaseModel]) -> None:
    @given(reply=st.builds(reply_type))
    @settings(max_examples=50, deadline=None)
    def check(reply: BaseModel) -> None:
        assert reply_type.model_validate_json(reply.model_dump_json()) == reply

    check()


def test_context_fields_exactly_match_template_free_variables() -> None:
    for context_type, templates in _CONTEXT_TEMPLATES.items():
        required = frozenset().union(*(_free_variables(template) for template in templates))
        assert frozenset(context_type.model_fields) == required, context_type.__name__


@pytest.mark.parametrize(
    "context_type",
    _CONTEXT_TEMPLATES,
    ids=lambda value: value.__name__,
)
def test_valid_context_renders_through_public_prompt_api(
    context_type: type[BaseModel],
) -> None:
    @given(context=_context_strategy(context_type))
    @settings(max_examples=10, deadline=None)
    def check(context: BaseModel) -> None:
        assert _RENDERERS[context_type](context).strip()

    check()
