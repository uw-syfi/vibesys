"""Prompt rendering helpers for registered domains."""

from __future__ import annotations

from pathlib import Path

from vibe_serve.domains.base import DOMAIN_ROLES, DomainDefinition, DomainRole
from vibe_serve.prompts import render_string


def _coerce_role(role: DomainRole | str) -> DomainRole:
    try:
        return role if isinstance(role, DomainRole) else DomainRole(role)
    except ValueError as exc:
        raise ValueError(
            f"Unknown domain role {role!r}. Choose from: "
            f"{', '.join(domain_role.value for domain_role in DOMAIN_ROLES)}."
        ) from exc


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
    ``bench_path``, ``accuracy_checker_path``, ``runtime_notes``; built by
    ``_domain_render_context`` in ``loop.py``) so authors can branch on the run
    from any file.
    ``single_agent`` falls back to ``implementer`` + ``judge`` when the
    directory has no explicit ``single_agent.md`` file. Leading and trailing
    blank lines are stripped — the base template owns the spacing around the
    ``{{ domain_<role> }}`` injection point.
    """
    role_name = _coerce_role(role)
    domain_dir = domain.prompt_dir
    raw = _load_role_file(domain_dir, role_name)
    if raw is None and role_name is DomainRole.SINGLE_AGENT:
        raw = "\n\n".join(
            text
            for text in (
                _load_role_file(domain_dir, DomainRole.IMPLEMENTER),
                _load_role_file(domain_dir, DomainRole.JUDGE),
            )
            if text
        )
    if not raw:
        return ""
    return render_string(raw, **context).strip("\n")
