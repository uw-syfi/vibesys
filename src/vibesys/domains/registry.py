"""Explicit registry for repo-defined domains."""

from __future__ import annotations

from vibesys.constants import DomainName
from vibesys.domains import database, generic, llm_serving, microservices
from vibesys.domains.base import DomainDefinition  # noqa: TC001  # tracked: #288

DOMAINS: dict[DomainName, DomainDefinition] = {
    generic.DEFINITION.name: generic.DEFINITION,
    llm_serving.DEFINITION.name: llm_serving.DEFINITION,
    microservices.DEFINITION.name: microservices.DEFINITION,
    database.DEFINITION.name: database.DEFINITION,
}


def registered_domains() -> list[str]:
    """Names of domains registered in this repo."""
    return sorted(domain.value for domain in DOMAINS)


def resolve_domain(name: DomainName) -> DomainDefinition:
    """Resolve a registered domain enum to its definition."""
    if not isinstance(name, DomainName):
        raise TypeError(f"domain must be a DomainName, got {type(name).__name__}.")  # noqa: TRY003  # tracked: #288

    domain = DOMAINS[name]
    if not domain.prompt_dir.is_dir():
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Registered domain {name.value!r} has no prompt directory: {domain.prompt_dir}"
        )
    return domain
