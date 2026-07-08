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


def resolve_domain(name: DomainName) -> DomainDefinition:
    """Resolve a registered domain enum to its definition."""
    if not isinstance(name, DomainName):
        raise TypeError(f"domain must be a DomainName, got {type(name).__name__}.")

    domain = DOMAINS[name]
    if not domain.prompt_dir.is_dir():
        raise ValueError(
            f"Registered domain {name.value!r} has no prompt directory: {domain.prompt_dir}"
        )
    return domain
