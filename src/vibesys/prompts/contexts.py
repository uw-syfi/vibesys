"""Shared prompt-context marshalling used by more than one role's context model.

Every strategy's ``turns.py`` used to carry its own copy of a
``_domain_context()`` helper that assembles the uniform variable set every
``domains/<domain>/<role>.md`` section renders from (see
``vibesys.domains.rendering.render_domain_section``). This module holds that
one copy; a strategy calls :func:`domain_context` with the primitive values it
already has (from ``ctx.environment``) instead of hand-rolling the dict again.

Only primitive/pydantic-safe values are accepted here (never a live
``RunContext``/environment object): ``vibesys.prompts`` sits low in the
module graph (see ``tach.toml``), so this module must not import
``vibesys.orchestration`` or ``vibesys.domains``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from pydantic import BaseModel, ConfigDict

from vibesys.evaluators.input_manifest import WorkspaceSource

if TYPE_CHECKING:
    from collections.abc import Mapping


class DomainSectionContext(BaseModel):
    """The uniform variable set every ``domains/<domain>/<role>.md`` renders from.

    Field names and meaning mirror ``vibesys.domains.rendering.render_domain_section``'s
    docstring; this is that shared variable set given a type. Pass
    ``.model_dump()`` as the ``**context`` kwargs to ``render_domain_section``.
    """

    model_config = ConfigDict(frozen=True)

    modality: str | None
    interface: str
    reference_path: str
    benchmark_command: str | None
    accuracy_command: str | None
    runtime_notes: str
    profile_execution: str
    workspace_sources: tuple[WorkspaceSource, ...]


def domain_context(  # noqa: PLR0913  # one field per domain-section variable
    *,
    modality: str | None,
    interface: str,
    reference_path: str,
    benchmark_command: str | None,
    accuracy_command: str | None,
    runtime_notes: str,
    profile_execution: str,
    workspace_sources: tuple[WorkspaceSource, ...],
) -> DomainSectionContext:
    """Build the shared domain-section context from a run's primitive facts.

    Replaces every strategy's own ``_domain_context()`` copy (identical in
    ``multi``, ``single``, ``profile_multi``, and ``profile_single``). Callers
    pass ``ctx.environment``'s own fields (``view.paths.benchmark_command``,
    ``view.prompt_notes``, ...); this module never touches ``ctx`` itself.
    """
    return DomainSectionContext(
        modality=modality,
        interface=interface,
        reference_path=reference_path,
        benchmark_command=benchmark_command,
        accuracy_command=accuracy_command,
        runtime_notes=runtime_notes,
        profile_execution=profile_execution,
        workspace_sources=workspace_sources,
    )


class PlanFocusKwargs(TypedDict):
    """The three optional profile-focus fields every plan-role context takes."""

    active_component: str | None
    ledger_text: str | None
    ranked_bottlenecks: list[dict[str, object]]


def plan_focus_kwargs(extra: Mapping[str, object]) -> PlanFocusKwargs:
    """Narrow a strategy's loosely typed ``plan_prompt_context()`` dict.

    A plain strategy's ``plan_prompt_context()`` returns ``{}``; a
    profile-guided strategy's controller returns ``active_component``/
    ``ledger_text``/``ranked_bottlenecks``. Both are ``dict[str, object]``
    statically, so this narrows to the three fields explicitly instead of
    spreading an ``object``-typed mapping into a pydantic constructor call.
    """
    return {
        "active_component": cast("str | None", extra.get("active_component")),
        "ledger_text": cast("str | None", extra.get("ledger_text")),
        "ranked_bottlenecks": cast("list[dict[str, object]]", extra.get("ranked_bottlenecks", [])),
    }


class ImplementerFocusKwargs(TypedDict):
    """The one optional profile-focus field an implementer-role context takes."""

    active_component: str | None


def implementer_focus_kwargs(extra: Mapping[str, object]) -> ImplementerFocusKwargs:
    """Narrow a strategy's loosely typed ``implementer_prompt_context()`` dict.

    See :func:`plan_focus_kwargs` for why this narrows explicitly rather than
    spreading the loosely typed mapping directly.
    """
    return {"active_component": cast("str | None", extra.get("active_component"))}
