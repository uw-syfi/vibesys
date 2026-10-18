"""Light contracts for the harness a run starts with: a provider and model pair, and how it reads.

``HarnessChoice`` is the pair a ``run.start`` names. ``HarnessOption`` is a vetted pair the helper's
``harnesses.list`` offers, with ``HarnessDisplay``: the readable names, family and tier agentshim
owns. A client renders these fields and never derives a name from an id. Pydantic and the
standard library only, like ``server.host_api``, which embeds them in the helper's messages.

Public surface: ``HarnessChoice``, ``HarnessDisplay``, ``HarnessOption``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class HarnessChoice(BaseModel):
    """One agent CLI provider and one model it runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str


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
