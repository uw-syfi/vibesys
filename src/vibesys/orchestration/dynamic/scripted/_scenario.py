"""What happens to one hypothesis, decided once from the seed and the hypothesis's name.

The planner writes a hypothesis down, the implementer works on it and the judge reviews it, in
separate turns that share no memory. Each of them asks this module, so they agree: the same seed and
the same hypothesis id give the same recipe, the same way for the turn to go wrong (if any), and the
same verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from vibesys.orchestration.dynamic.scripted._choice import index, unit
from vibesys.orchestration.dynamic.scripted._recipes import EditKind, Recipe, catalog

_ID = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<ordinal>\d+)$")


class Fault(StrEnum):
    """How an implementer turn can go wrong without its edit being at fault."""

    GIVES_UP = "gives_up"
    """It replies that the implementation failed, having changed nothing."""
    NO_EDIT = "no_edit"
    """It nominates a candidate it never changed."""
    BAD_REPLY = "bad_reply"
    """Its reply does not match the reply schema, on every attempt."""
    LOST_TURN = "lost_turn"
    """The turn ends without a reply, on every attempt."""
    BLOCKED = "blocked"
    """It replies that it cannot go on, and why."""


# Cumulative shares of the unit interval: the first fault whose bound exceeds the draw.
_FAULTS = (
    (0.07, Fault.GIVES_UP),
    (0.12, Fault.NO_EDIT),
    (0.15, Fault.BAD_REPLY),
    (0.17, Fault.LOST_TURN),
    (0.21, Fault.BLOCKED),
)
_REJECT_SHARE = 0.12
"""The share of reviewed candidates a judge rejects whatever they measured."""
_REJECT_REGRESSION_SHARE = 0.6
"""The share of candidates built from a regressing edit that a judge rejects."""


def hypothesis_id(recipe: Recipe, ordinal: int) -> str:
    """The id of the ``ordinal``-th idea, which names its recipe so every turn finds it again."""
    return f"{recipe.slug}-{ordinal}"


def recipe_named(recipes: tuple[Recipe, ...], identifier: str) -> Recipe | None:
    """The recipe a hypothesis id names, or ``None`` for an id this script did not write."""
    match = _ID.match(identifier)
    slug = match.group("slug") if match else identifier
    return next((recipe for recipe in recipes if recipe.slug == slug), None)


def ordinal_of(identifier: str) -> int | None:
    """The ordinal a hypothesis id carries, or ``None`` when it has none."""
    match = _ID.match(identifier)
    return int(match.group("ordinal")) if match else None


def idea(seed: int, recipes: tuple[Recipe, ...], ordinal: int) -> Recipe:
    """The recipe the ``ordinal``-th idea of a run pitches."""
    weighted = catalog(recipes)
    return weighted[index(seed, len(weighted), "idea", str(ordinal))]


@dataclass(frozen=True, slots=True)
class Scenario:
    """How one hypothesis goes: its recipe, how its implementer turn fails (if it does), its verdict."""

    recipe: Recipe | None
    fault: Fault | None
    rejected: bool


def scenario(seed: int, recipes: tuple[Recipe, ...], identifier: str) -> Scenario:
    """The scenario of the hypothesis ``identifier`` under ``seed``."""
    recipe = recipe_named(recipes, identifier)
    draw = unit(seed, "fault", identifier)
    fault = next((fault for bound, fault in _FAULTS if draw < bound), None)
    verdict = unit(seed, "verdict", identifier)
    share = (
        _REJECT_REGRESSION_SHARE
        if recipe is not None and recipe.kind is EditKind.REGRESSES
        else _REJECT_SHARE
    )
    return Scenario(recipe=recipe, fault=fault, rejected=verdict < share)
