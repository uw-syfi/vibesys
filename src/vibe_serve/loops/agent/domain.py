"""Registered domains — per-problem-space prompt and environment context.

A *domain* tells the agent loop what kind of system it is building and what
"good" means there: the background knowledge the implementer must read, and the
correctness/performance/integrity gates the judge must enforce. It is selected
with ``--domain`` from the repo's registered domains. Each domain has a
prompt directory whose role files are injected into the neutral base prompts,
and optional environment hooks for domain-specific setup:

    _domain/<name>/
    ├── README.md          ← human documentation, ignored by the loop
    ├── implementer.md     ← injected as {{ domain_implementer }}
    ├── judge.md           ← injected as {{ domain_judge }}
    ├── single_agent.md    ← injected as {{ domain_single_agent }}
    ├── orchestrator.md    ← injected as {{ domain_orchestrator }}
    └── profiler.md        ← injected as {{ domain_profiler }}

A missing role file injects nothing. ``single_agent`` is special: if the
directory has no ``single_agent.md`` file, it is *derived* by concatenating
``implementer.md`` and ``judge.md``, so authors don't hand-maintain a third copy.
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

# The roles a domain can contribute to. Each maps to a ``<role>.md`` file in the
# domain prompt directory and a ``{{ domain_<role> }}`` injection point in the
# corresponding base prompt.
DOMAIN_ROLES: tuple[DomainRole, ...] = tuple(DomainRole)

_DOMAINS_DIR = Path(__file__).resolve().parent / "templates" / "_domain"


@dataclass(frozen=True)
class DomainDefinition:
    name: DomainName
    prompt_dir: Path
    environment_hooks: EnvironmentHooks


DOMAINS: dict[DomainName, DomainDefinition] = {
    DomainName.LLM_SERVING: DomainDefinition(
        name=DomainName.LLM_SERVING,
        prompt_dir=_DOMAINS_DIR / "llm-serving",
        environment_hooks=LLMServingEnvironmentHooks(),
    ),
    DomainName.GENERIC: DomainDefinition(
        name=DomainName.GENERIC,
        prompt_dir=_DOMAINS_DIR / "generic",
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
    if not domain.prompt_dir.is_dir():
        raise ValueError(
            f"Registered domain {name.value!r} has no prompt directory: {domain.prompt_dir}"
        )
    return domain


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
