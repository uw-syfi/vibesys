"""Contract test: every ``Role``'s context model matches its template's free variables.

Two correctness properties, both from ``vs_prompts.api.resolve_free_variables``:

- **Under-supply**: a template variable no caller ever passes. ``StrictUndefined``
  catches this at render time already; this test catches it earlier, statically.
- **Over-supply**: a context model field a template never references, so its
  content silently never reaches the rendered prompt. Jinja itself cannot see
  this direction (unused kwargs are just discarded); only comparing the
  model's declared fields against the template's real free variables can.

Several roles share one context model across sibling templates (e.g. every
``MULTI_PROFILERS`` kind, or the three strategies' plan roles): the model's
field set is checked against the *union* of free variables across every
template that role's context type serves, which is exactly right for both the
single-role and shared-role cases.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys import roles
from vibesys.prompts import PROMPTS_DIR
from vs_prompts.api import resolve_free_variables

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.runtime import Role

# Auto-injected by ``Prompt.render`` (backend compute fragments) for the
# roles that render through it (today: issue_queue's system prompts); never
# part of a Role's own context model.
_BACKEND_FRAGMENT_NAMES = frozenset(
    {"device_dtype", "judge_device_correctness", "profiling_workflow"}
)
_MODALITY_ROOT = PROMPTS_DIR / "shared" / "_modality"
# Matches `{% include "_modality/" ~ modality ~ "/<role>.j2" %}`, the one
# dynamic-include pattern any template here uses.
_DYNAMIC_MODALITY_RE = re.compile(r'_modality/"\s*~\s*modality\s*~\s*"/(\w+)\.j2')


def _split_template(template: str) -> tuple[Path, str]:
    """Mirror ``vibesys.orchestration.agents._split_template``."""
    parts = Path(template).parts
    assert len(parts) >= 3, f"{template!r} needs a strategy folder"
    return PROMPTS_DIR / parts[0] / parts[1], "/".join(parts[2:])


def _resolve_template_path(template: str) -> tuple[Path, tuple[Path, ...]]:
    template_dir, name = _split_template(template)
    roots = (template_dir, PROMPTS_DIR / "shared", PROMPTS_DIR)
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate, roots
    raise AssertionError(f"no template file found for {template!r}")  # noqa: TRY003


def _free_variables(template: str) -> frozenset[str]:
    """Return the free variables ``template`` needs, resolving known dynamic includes."""
    path, roots = _resolve_template_path(template)
    free, unresolved = resolve_free_variables(path, search_roots=roots)
    result = set(free) - _BACKEND_FRAGMENT_NAMES
    if unresolved:
        source = path.read_text()
        for match in _DYNAMIC_MODALITY_RE.finditer(source):
            suffix = match.group(1)
            for variant in sorted(_MODALITY_ROOT.glob(f"*/{suffix}.j2")):
                variant_free, _ = resolve_free_variables(variant, search_roots=roots)
                result |= set(variant_free) - _BACKEND_FRAGMENT_NAMES
    return frozenset(result)


def _roles_by_context_model() -> dict[type[BaseModel], list[Role]]:
    grouped: dict[type[BaseModel], list[Role]] = {}
    for role in roles.ALL_ROLES:
        grouped.setdefault(role.context, []).append(role)
    return grouped


def test_every_role_declares_a_context_model() -> None:
    for role in roles.ALL_ROLES:
        assert role.context is not None, f"{role.id}: {role.template} has no context model"


# TODO(stack PR 08): remove. At this commit, `loops/evolve/profilers/*.j2`  # noqa: FIX002  # tracked: #288
# does not exist yet as its own files (they land with the evolve strategy
# migration); the `CandidateProfilerContext` role's template resolves
# through the shared/ fallback root instead, to `shared/profilers/*.j2`,
# which does not read this field yet. `loops/multi/profilers/*.j2` wrappers
# landed with the multi migration (stack PR 07), so `ProfilerContext` is no
# longer exempt. PR 08 adds the analogous `loops/evolve/profilers/<kind>.j2`
# wrapper that includes the shared fragment and reads its Pareto-objectives
# addendum field.
_EXPECTED_EXTRA_FIELDS_BEFORE_STRATEGY_MIGRATION = {
    "CandidateProfilerContext": frozenset({"pareto_objectives_addendum"}),
}


def test_context_model_fields_match_template_free_variables() -> None:
    """Assert exact equality per context-model group (see module docstring)."""
    violations: list[str] = []
    for context_model, group in _roles_by_context_model().items():
        model_fields = frozenset(context_model.model_fields)
        required = frozenset()
        for role in group:
            required |= _free_variables(role.template)
        allowed_extra = _EXPECTED_EXTRA_FIELDS_BEFORE_STRATEGY_MIGRATION.get(
            context_model.__name__, frozenset()
        )
        if model_fields - allowed_extra != required:
            missing = required - model_fields
            extra = model_fields - allowed_extra - required
            templates = sorted({role.template for role in group})
            detail = f"{context_model.__name__} (templates: {templates})"
            if missing:
                detail += f": model is missing fields the template(s) need: {sorted(missing)}"
            if extra:
                detail += f": model has fields no template in the group reads: {sorted(extra)}"
            violations.append(detail)
    assert not violations, "\n".join(violations)
