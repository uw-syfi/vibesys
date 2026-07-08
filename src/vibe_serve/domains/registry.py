"""Explicit registry for repo-defined domains."""

from __future__ import annotations

from vibe_serve.domains import generic, llm_serving
from vibe_serve.domains.base import DomainDefinition, DomainName

DOMAINS: dict[DomainName, DomainDefinition] = {
    generic.DEFINITION.name: generic.DEFINITION,
    llm_serving.DEFINITION.name: llm_serving.DEFINITION,
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
