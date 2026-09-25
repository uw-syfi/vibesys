"""Default scalar/Pareto selector: today's softmax/Pareto population logic.

Pure functions over ``tuple[Individual, ...]``: no mutation, no I/O. Ported
unchanged in algorithm from the former ``vibesys.loops.evolve.population``.

## Single-objective vs multi-objective modes

- **Scalar** (a space with no configured axes): rank by ``Individual.perf_metric``;
  parent sampling is a softmax over normalized fitness.
- **Multi-objective Pareto** (a space with axes): keep a non-dominated
  *frontier* over ``Individual.metrics``; with probability ``frontier_bias``
  parent selection draws uniformly from the frontier, otherwise it falls back
  to the scalar softmax over the *primary* objective.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from vibesys.evaluators.metrics import Measurement, MetricSpace, Objective
from vibesys.search.population.models import Individual, Proposal

if TYPE_CHECKING:
    import random
    from collections.abc import Sequence

__all__ = [
    "best",
    "frontier",
    "passed_individuals",
    "select",
]

# The axis ``best`` orders on when the task declares no objectives.
_SCALAR_AXIS = Objective(name="perf_metric", direction="max")


def passed_individuals(individuals: Sequence[Individual]) -> list[Individual]:
    """Return the subset eligible as a parent: passed and materialized."""
    return [individual for individual in individuals if individual.passed and individual.commit]


def _headline_space(space: MetricSpace) -> MetricSpace:
    if space.primary is not None:
        return space
    return MetricSpace(objectives=(_SCALAR_AXIS,), relative_noise=space.relative_noise)


def _headline_reading(individual: Individual, headline: MetricSpace) -> Measurement | None:
    primary = headline.primary
    assert primary is not None  # noqa: S101  # guaranteed by _headline_space
    value = individual.metrics.get(primary.name, individual.perf_metric)
    if value is None:
        return None
    return Measurement(metric=primary.name, value=value)


def best(individuals: Sequence[Individual], space: MetricSpace) -> Individual | None:
    """Return the passed individual leading on the run's headline axis.

    Ties (including ties inside the declared tolerance) go to the latest id.
    """
    headline = _headline_space(space)
    newest_first = sorted(passed_individuals(individuals), key=lambda i: i.id, reverse=True)

    def reading(individual: Individual) -> Measurement | None:
        return _headline_reading(individual, headline)

    return headline.best(newest_first, reading)


def frontier(individuals: Sequence[Individual], space: MetricSpace) -> list[Individual]:
    """Return the Pareto-non-dominated subset of passed individuals."""
    passed = passed_individuals(individuals)
    return space.frontier(
        [individual for individual in passed if space.complete(individual.metrics)],
        lambda individual: individual.metrics,
    )


def _scalar_softmax_parent(
    individuals: Sequence[Individual],
    *,
    rng: random.Random,
    temperature: float,
    space: MetricSpace,
) -> Individual | None:
    ranked = [i for i in passed_individuals(individuals) if i.perf_metric is not None]
    if not ranked:
        return None
    if len(ranked) == 1:
        return ranked[0]
    scores = [space.signed_primary(i.perf_metric) for i in ranked if i.perf_metric is not None]
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-12:  # noqa: PLR2004
        return rng.choice(ranked)
    normed = [(score - lo) / (hi - lo) for score in scores]
    t = max(temperature, 1e-6)
    logits = [n / t for n in normed]
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    total = sum(exps)
    r = rng.random() * total
    acc = 0.0
    for ind, w in zip(ranked, exps, strict=True):
        acc += w
        if r <= acc:
            return ind
    return ranked[-1]


def _select_parent(
    individuals: Sequence[Individual],
    *,
    rng: random.Random,
    temperature: float,
    space: MetricSpace,
    frontier_bias: float,
) -> Individual | None:
    if space.objectives:
        front = frontier(individuals, space)
        if front and rng.random() < frontier_bias:
            return rng.choice(front)
    return _scalar_softmax_parent(individuals, rng=rng, temperature=temperature, space=space)


def _select_inspirations(  # noqa: PLR0913  # tracked: #288
    individuals: Sequence[Individual],
    *,
    parent_id: int | None,
    k_top: int,
    k_random: int,
    rng: random.Random,
    space: MetricSpace,
) -> list[Individual]:
    pool = [i for i in passed_individuals(individuals) if i.id != parent_id]
    if not pool:
        return []

    primary = space.primary
    if primary is not None:
        front_ids = {i.id for i in frontier(individuals, space) if i.id != parent_id}
        front_pool = [i for i in pool if i.id in front_ids]
        front_pool.sort(
            key=lambda i: primary.signed(i.metrics.get(primary.name, float("-inf"))),
            reverse=True,
        )
        top = front_pool[:k_top]
        if len(top) < k_top:
            non_front = [i for i in pool if i.id not in front_ids and i.perf_metric is not None]
            non_front.sort(
                key=lambda i: (
                    space.signed_primary(i.perf_metric)
                    if i.perf_metric is not None
                    else float("-inf")
                ),
                reverse=True,
            )
            top.extend(non_front[: k_top - len(top)])
    else:
        ranked = [i for i in pool if i.perf_metric is not None]
        ranked.sort(
            key=lambda i: i.perf_metric if i.perf_metric is not None else float("-inf"),
            reverse=True,
        )
        top = ranked[:k_top]

    top_ids = {i.id for i in top}
    rest = [i for i in pool if i.id not in top_ids]
    rnd = rng.sample(rest, k=min(k_random, len(rest))) if rest else []
    return top + rnd


def select(  # noqa: PLR0913  # tracked: #288
    individuals: Sequence[Individual],
    *,
    rng: random.Random,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    space: MetricSpace,
    frontier_bias: float,
) -> Proposal | None:
    """Sample a parent and its inspirations, mutating only ``rng``'s state."""
    parent = _select_parent(
        individuals,
        rng=rng,
        temperature=selection_temperature,
        space=space,
        frontier_bias=frontier_bias,
    )
    inspirations = _select_inspirations(
        individuals,
        parent_id=parent.id if parent else None,
        k_top=k_top_inspirations,
        k_random=k_random_inspirations,
        rng=rng,
        space=space,
    )
    if parent is None:
        passers = passed_individuals(individuals)
        if not passers:
            return None
        parent = passers[-1]
    return Proposal(parent=parent, inspirations=tuple(inspirations))
