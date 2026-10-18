"""The names a person reads for VibeSys's own vocabularies, and the ids a screen must never show.

An agent role has an id (``perf-evaluator``) that a program matches on and a person must never be
shown. The role's name is the ``title`` its search declares on its ``Role``; this module gathers
them into the catalog the clients receive (``@vibesys/contracts``, generated from
:class:`DisplayNames`). A client reads a role a plugin declares as the words of its id.

Harness and model names are not here: agentshim owns them and serves them through
``vs_agent.api``. :func:`code_ids` lists only their ids, so the screen checks can fail on any a
screen draws as text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from vibesys.orchestration.dynamic.search.api import dynamic
from vibesys.orchestration.evolve.search.api import evolve
from vibesys.orchestration.issue_queue.search.api import issue_queue
from vibesys.orchestration.multi.search.api import multi
from vibesys.orchestration.single.search.api import single
from vs_agent.api import SHIPPED_PROVIDERS, vetted_models

if TYPE_CHECKING:
    from collections.abc import Sequence


class _RoleOwner(Protocol):
    """What the catalog reads of a search: the roles it declares and how it names their ids."""

    @property
    def roles(self) -> Sequence[_Titled]: ...

    @property
    def ids(self) -> _RoleIds: ...


class _Titled(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def title(self) -> str: ...


class _RoleIds(Protocol):
    def role_id(self, role: str) -> _Rooted: ...


class _Rooted(Protocol):
    @property
    def root(self) -> str: ...


_SEARCHES = (single, dynamic, multi, evolve, issue_queue)


class DisplayNames(BaseModel):
    """Id to the name a person reads, for each vocabulary VibeSys owns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: dict[str, str]
    """An agent role's name, and its id in each built-in search, to its title."""


class CodeIds(BaseModel):
    """The ids of the closed vocabularies a screen must not draw as text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    harnesses: tuple[str, ...]
    models: tuple[str, ...]
    roles: tuple[str, ...]


class ConflictingNamesError(ValueError):
    """Two roles name one id differently, so no screen could know which to show."""

    def __init__(self, key: str, names: tuple[str, str]) -> None:
        """Name the id and both titles."""
        super().__init__(f"role {key!r} is titled both {names[0]!r} and {names[1]!r}")


def display_names(searches: Sequence[_RoleOwner] = _SEARCHES) -> DisplayNames:
    """The title of every role of ``searches`` (the built-in ones), by bare name and by id."""
    roles: dict[str, str] = {}
    for search in searches:
        for role in search.roles:
            for key in (role.name, search.ids.role_id(role.name).root):
                known = roles.setdefault(key, role.title)
                if known != role.title:
                    raise ConflictingNamesError(key, (known, role.title))
    return DisplayNames(roles=roles)


def code_ids() -> CodeIds:
    """Every shipped harness and vetted model id, and every role id of display_names."""
    return CodeIds(
        harnesses=SHIPPED_PROVIDERS,
        models=tuple(model for provider in SHIPPED_PROVIDERS for model in vetted_models(provider)),
        roles=tuple(display_names().roles),
    )
