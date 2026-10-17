"""The names a person reads for the closed vocabularies a screen shows.

A harness, a model and an agent role each have an id (``claude``, ``perf-evaluator``, a model's
vendor id) that a program matches on and a person must never be shown. Each fact has
one owner, and this module only gathers them into the one catalog the clients receive
(``@vibesys/contracts``, generated from :class:`DisplayNames`):

- a harness's and a model's name are agentshim's (``ProviderProfile.display_name`` and
  ``model_display_name``), read through ``vs_agent.api``;
- a role's name is the ``title`` its search declares on its ``Role``.

The catalog lists only the ids VibeSys ships. A client falls back to words from the id for a role
a plugin declares, and shows the identifier as such for a model no registry names.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.single.search.api import single
from vs_agent.api import (
    SHIPPED_PROVIDERS,
    model_display_name,
    provider_display_name,
    vetted_models,
)

_SEARCHES = (single, dynamic, multi, evolve, issue_queue)


class DisplayNames(BaseModel):
    """Id to the name a person reads, for each closed vocabulary a screen shows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    harnesses: dict[str, str]
    """A shipped agent CLI provider's id to its name: ``claude`` is ``Claude Code``."""
    models: dict[str, str]
    """A vetted model's id to the name agentshim gives it."""
    roles: dict[str, str]
    """An agent role's name, and its id in each built-in search, to its title."""


class ConflictingNames(ValueError):  # noqa: N818  # a name for what the catalog refuses, not an error family
    """Two sources name one id differently, so no screen could know which to show."""

    def __init__(self, vocabulary: str, key: str, names: tuple[str, str]) -> None:
        """Name the vocabulary, the id and both names."""
        super().__init__(f"{vocabulary} {key!r} is named both {names[0]!r} and {names[1]!r}")


def _add(into: dict[str, str], vocabulary: str, key: str, name: str) -> None:
    known = into.setdefault(key, name)
    if known != name:
        raise ConflictingNames(vocabulary, key, (known, name))


def display_names() -> DisplayNames:
    """The catalog of every shipped harness, vetted model and built-in role."""
    harnesses: dict[str, str] = {}
    models: dict[str, str] = {}
    for provider in SHIPPED_PROVIDERS:
        _add(harnesses, "harness", provider, provider_display_name(provider))
        for model in vetted_models(provider):
            name = model_display_name(provider, model)
            if name is not None:
                _add(models, "model", model, name)
    roles: dict[str, str] = {}
    for search in _SEARCHES:
        for role in search.roles:
            _add(roles, "role", role.name, role.title)
            _add(roles, "role", search.ids.role_id(role.name).root, role.title)
    return DisplayNames(harnesses=harnesses, models=models, roles=roles)
