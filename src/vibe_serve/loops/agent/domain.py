"""Registered domains — per-problem-space prompt and environment context.

A *domain* tells the agent loop what kind of system it is building and what
"good" means there: the background knowledge the implementer must read, and the
correctness/performance/integrity gates the judge must enforce. It is selected
with ``--domain`` from the repo's registered domains. Each domain has a
Markdown prompt file whose ``##`` role sections are injected into the neutral
base prompts, and optional environment hooks for domain-specific setup:

    _domain/<name>.md
    ├── (free-form prose / description — ignored by the loop)
    ├── ## implementer    ← injected as {{ domain_implementer }}
    ├── ## judge          ← injected as {{ domain_judge }}
    ├── ## single_agent   ← injected as {{ domain_single_agent }}
    ├── ## orchestrator   ← injected as {{ domain_orchestrator }}
    └── ## profiler       ← injected as {{ domain_profiler }}

The section heading *is* the address: a line that is exactly ``## <role>`` (for a
role in :data:`DOMAIN_ROLES`) starts that role's section, which runs until the
next role heading. The section body is normal Markdown — it may use its own
``##`` sub-headings (those never match a role name) and may use ``{% if %}``
Jinja to branch on the run's context. Any prose before the first role heading is
human documentation and is not injected.

A missing role section injects nothing. ``single_agent`` is special: if the file
has no ``## single_agent`` section, it is *derived* by concatenating the
``implementer`` and ``judge`` sections, so authors don't hand-maintain a third
copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibe_serve.environment_hooks import (
    EnvironmentHooks,
    LLMServingEnvironmentHooks,
    NoopEnvironmentHooks,
)
from vibe_serve.prompts import render_string


class DomainName(StrEnum):
    LLM_SERVING = "llm-serving"
    GENERIC = "generic"


class DomainRole(StrEnum):
    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    SINGLE_AGENT = "single_agent"
    ORCHESTRATOR = "orchestrator"
    PROFILER = "profiler"


DEFAULT_DOMAIN = DomainName.LLM_SERVING

# The roles a domain pack can contribute to. Each maps to a ``## <role>`` section
# in the domain file and a ``{{ domain_<role> }}`` injection point in the
# corresponding base prompt.
DOMAIN_ROLES: tuple[DomainRole, ...] = tuple(DomainRole)

_DOMAINS_DIR = Path(__file__).resolve().parent / "templates" / "_domain"


@dataclass(frozen=True)
class DomainDefinition:
    name: DomainName
    prompt_path: Path
    environment_hooks: EnvironmentHooks


DOMAINS: dict[DomainName, DomainDefinition] = {
    DomainName.LLM_SERVING: DomainDefinition(
        name=DomainName.LLM_SERVING,
        prompt_path=_DOMAINS_DIR / "llm-serving.md",
        environment_hooks=LLMServingEnvironmentHooks(),
    ),
    DomainName.GENERIC: DomainDefinition(
        name=DomainName.GENERIC,
        prompt_path=_DOMAINS_DIR / "generic.md",
        environment_hooks=NoopEnvironmentHooks(),
    ),
}


def registered_domains() -> list[str]:
    """Names of domains registered in this repo."""
    return sorted(domain.value for domain in DOMAINS)


def resolve_domain(spec: str | DomainName) -> DomainDefinition:
    """Resolve a ``--domain`` value to a registered domain definition."""
    try:
        name = spec if isinstance(spec, DomainName) else DomainName(str(spec))
    except ValueError as exc:
        raise ValueError(
            f"Unknown domain {spec!r}. Choose from: {', '.join(registered_domains())}."
        ) from exc

    domain = DOMAINS[name]
    if not domain.prompt_path.is_file():
        raise ValueError(f"Registered domain {name.value!r} has no prompt file: {domain.prompt_path}")
    return domain


def _coerce_role(role: DomainRole | str) -> DomainRole:
    try:
        return role if isinstance(role, DomainRole) else DomainRole(role)
    except ValueError as exc:
        raise ValueError(
            f"Unknown domain role {role!r}. Choose from: "
            f"{', '.join(domain_role.value for domain_role in DOMAIN_ROLES)}."
        ) from exc


def _domain_prompt_path(domain: DomainDefinition | Path) -> Path:
    return domain.prompt_path if isinstance(domain, DomainDefinition) else domain


def _role_heading(line: str) -> DomainRole | None:
    """Return the role name if ``line`` is exactly a ``## <role>`` heading.

    Only headings whose text matches a name in :data:`DOMAIN_ROLES` delimit a
    section, so a body's own ``## Required: …`` sub-headings are left intact.
    """
    stripped = line.strip()
    for role in DOMAIN_ROLES:
        if stripped == f"## {role.value}":
            return role
    return None


def _load_sections(domain_file: Path) -> dict[DomainRole, str]:
    """Parse a domain file into ``{role: raw_section_text}``.

    Lines before the first role heading (description prose) are ignored.
    """
    sections: dict[DomainRole, list[str]] = {}
    current: DomainRole | None = None
    for line in domain_file.read_text().splitlines():
        heading = _role_heading(line)
        if heading is not None:
            current = heading
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {role: "\n".join(lines).strip("\n") for role, lines in sections.items()}


def render_domain_section(
    domain: DomainDefinition | Path,
    role: DomainRole | str,
    **context: object,
) -> str:
    """Render a domain file's ``## <role>`` section, or ``""`` if absent/empty.

    The section is rendered through Jinja with ``context`` — the same uniform
    variable set for every role (``modality``, ``interface``, ``reference_path``,
    ``bench_path``, ``accuracy_checker_path``, ``runtime_notes``; built by
    ``_domain_render_context`` in ``loop.py``) so authors can branch on the run
    from any section.
    ``single_agent`` falls back to ``implementer`` + ``judge`` when the
    file has no explicit ``## single_agent`` section. Leading and trailing blank
    lines are stripped — the base template owns the spacing around the
    ``{{ domain_<role> }}`` injection point.
    """
    role_name = _coerce_role(role)
    sections = _load_sections(_domain_prompt_path(domain))
    raw = sections.get(role_name)
    if raw is None and role_name is DomainRole.SINGLE_AGENT:
        raw = "\n\n".join(
            text
            for text in (
                sections.get(DomainRole.IMPLEMENTER),
                sections.get(DomainRole.JUDGE),
            )
            if text
        )
    if not raw:
        return ""
    return render_string(raw, **context).strip("\n")
