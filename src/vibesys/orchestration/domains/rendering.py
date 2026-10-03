"""Prompt rendering helpers for registered domains."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.domains.base import DOMAIN_ROLES, DomainDefinition, DomainRole
from vibesys.orchestration.prompts import PROMPTS_DIR, render_string, render_template

if TYPE_CHECKING:
    from pathlib import Path


def _coerce_role(role: DomainRole | str) -> DomainRole:
    try:
        return role if isinstance(role, DomainRole) else DomainRole(role)
    except ValueError as exc:
        message = f"Unknown domain role {role!r}. Choose from: {', '.join(domain_role.value for domain_role in DOMAIN_ROLES)}."
        raise ValueError(message) from exc


def _load_role_file(domain_dir: Path, role: DomainRole) -> str | None:
    role_file = domain_dir / f"{role.value}.md"
    if not role_file.is_file():
        return None
    return role_file.read_text().strip("\n")


def render_domain_section(
    domain: DomainDefinition,
    role: DomainRole | str,
    **context: object,
) -> str:
    """Render a domain directory's ``<role>.md`` file, or ``""`` if absent/empty.

    The role file is rendered through Jinja with ``context`` — the same uniform
    variable set for every role (``modality``, ``interface``, ``reference_path``,
    ``benchmark_command``, ``accuracy_command``, ``runtime_notes``,
    ``profile_execution``, and ``workspace_sources``; built by
    ``_domain_render_context`` in ``loop.py``)
    so authors can branch on the run from any file.
    ``single_agent`` falls back to the rendered ``implementer`` and ``judge``
    sections, blank-line separated, when the directory has no explicit
    ``single_agent.md`` file. Leading and trailing
    blank lines are stripped — the base template owns the spacing around the
    ``{{ domain_<role> }}`` injection point.
    """
    role_name = _coerce_role(role)
    raw = _load_role_file(domain.prompt_dir, role_name)
    if raw is None and role_name is DomainRole.SINGLE_AGENT:
        return render_template(
            "_domain/single_agent_fallback.j2",
            template_dir=PROMPTS_DIR / "shared",
            sections=tuple(
                render_domain_section(domain, fallback, **context)
                for fallback in (DomainRole.IMPLEMENTER, DomainRole.JUDGE)
            ),
        )
    if not raw:
        return ""
    return render_string(raw, **context).strip("\n")
