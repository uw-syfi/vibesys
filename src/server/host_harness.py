"""Light contracts for the harness a run starts with: a provider and model pair, and how it reads.

``HarnessChoice`` is the pair a ``run.start`` names. ``HarnessOption`` is a vetted pair the helper's
``harnesses.list`` offers, with ``HarnessDisplay``: the readable names, family and tier agentshim
owns. A client renders these fields and never derives a name from an id. Pydantic and the
standard library only, like ``server.host_api``, which embeds them in the helper's messages.

``Maker`` and ``ModelGeneration`` (with ``generation_conflict``, which checks a list of options groups
without contradiction) say who made a model and which generation of theirs it is, so a
client groups maker, generation and models without parsing a name or an id.

Public surface: ``HarnessChoice``, ``HarnessDisplay``, ``HarnessOption``, ``Maker``,
``ModelGeneration``, ``generation_conflict``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

if TYPE_CHECKING:
    from collections.abc import Iterable


class HarnessChoice(BaseModel):
    """One agent CLI provider and one model it runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str


class Maker(StrEnum):
    """Who made a model: the closed set of makers whose models a provider's CLI runs."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


class ModelGeneration(BaseModel):
    """One generation of a maker's models, and where it ranks among the maker's."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    """Names the generation within its maker; models of one generation share it."""
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    """The generation as people call it, as agentshim names it."""
    order: Annotated[int, Field(ge=1)]
    """The generation's rank among the maker's, 1 the newest; distinct per ``id`` of a maker."""


class HarnessDisplay(BaseModel):
    """How a provider and model pair reads, as agentshim names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    """The provider's CLI as people call it ("Claude Code")."""
    model_name: str | None = None
    """The model alone ("Opus 5.5"); none when agentshim does not name it: show the raw id."""
    family: str | None = None
    """The model line ("Claude", "GPT", "Gemini"); none exactly when ``model_name`` is."""
    tier: Annotated[int, Field(ge=1)] | None = None
    """The model's rank in its family, 1 the smallest and cheapest; none off the ladder."""
    tier_name: str | None = None
    """The tier as people call it ("Opus"); set exactly when ``tier`` is."""
    maker: Annotated[Maker | None, Field(exclude_if=lambda maker: maker is None)] = None
    """Who made the model; sent only to a client that asked for generations, and none when
    agentshim names no maker."""
    generation: Annotated[
        ModelGeneration | None, Field(exclude_if=lambda generation: generation is None)
    ] = None
    """The generation the model belongs to; sent only to a client that asked for generations,
    and none when agentshim names none: group such a model by its ``family`` alone."""

    @model_validator(mode="after")
    def _generation_belongs_to_a_maker(self) -> Self:
        if self.generation is not None and self.maker is None:
            message = "generation is ranked among a maker's, so it needs a maker"
            raise ValueError(message)
        return self


class HarnessOption(BaseModel):
    """A vetted provider and model pair a run may start with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    default: bool
    """The model a run on ``provider`` uses when nothing names one."""
    display: Annotated[HarnessDisplay | None, Field(exclude_if=lambda display: display is None)] = (
        None
    )
    """How the pair reads; a helper that predates it sends none and a client shows the raw ids."""

    def without_generations(self) -> HarnessOption:
        """The option as a client that did not ask for generations reads it: no maker, no generation."""
        if self.display is None:
            return self
        return self.model_copy(
            update={"display": self.display.model_copy(update={"maker": None, "generation": None})}
        )


def generation_conflict(options: Iterable[HarnessOption]) -> str | None:
    """Why ``options`` cannot be grouped by maker and generation, or ``None`` when they can.

    Within one maker a generation id has one name and one order, and two ids never share an
    order, so a client that groups by id and sorts by order never meets a contradiction.
    """
    named: dict[tuple[Maker, str], ModelGeneration] = {}
    owners: dict[tuple[Maker, int], str] = {}
    for option in options:
        display = option.display
        if display is None or display.maker is None or display.generation is None:
            continue
        generation = display.generation
        known = named.setdefault((display.maker, generation.id), generation)
        if known != generation:
            return f"{display.maker.value} generation {generation.id!r} is described two ways"
        rival = owners.setdefault((display.maker, generation.order), generation.id)
        if rival != generation.id:
            return (
                f"{display.maker.value} generations {rival!r} and {generation.id!r} "
                f"share order {generation.order}"
            )
    return None
