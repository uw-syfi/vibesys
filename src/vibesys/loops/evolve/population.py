"""In-memory evolutionary population and selection behavior.

This module is pure logic with no agent invocations or filesystem I/O. The
loop-owned state adapter converts populations to strict persisted contracts.

## Single-objective vs multi-objective modes

The module supports two selection modes:

- **Scalar** (a space with no configured axes): rank by
  ``Individual.perf_metric``; parent sampling is a softmax over normalized
  fitness. Used when the task declares no objectives (the default for
  back-compatibility with runs that predate Pareto support).

- **Multi-objective Pareto** (a space with axes): keep a non-dominated
  *frontier* over ``Individual.metrics``; with probability
  ``frontier_bias`` parent selection draws uniformly from the frontier,
  otherwise it falls back to the scalar softmax over the *primary*
  objective. Inspirations are pulled from the frontier first so the
  mutator sees diverse strategies, not just the throughput champion.

The two modes share data structures: every passing individual stores
both ``perf_metric`` (back-compat scalar) and ``metrics`` (the dict the
profiler reported). Mode is chosen at the call site, not at the data
layer.

## Comparison versus ranking

Every question of the form "is this candidate better?" -- dominance, frontier
membership, and the scalar champion -- is answered by the run's
:class:`~vibesys.loops.metrics.MetricSpace`, so the task's declared measurement
tolerance applies to selection. Parent sampling deliberately does not: the
softmax over normalized fitness ranks the whole archive rather than comparing
two candidates, and squashing near-ties there would flatten the very gradient
the sampler exists to follow.
"""

from __future__ import annotations

import math
import random  # noqa: TC003  # tracked: #288
from dataclasses import dataclass, field

from vibesys.loops.metrics import Measurement, MetricSpace, Objective

__all__ = [
    "Individual",
    "Population",
]

# The axis ``best`` orders on when the task declares no objectives. Scalar mode
# predates configurable objectives and has always read ``perf_metric`` as a
# maximization axis; naming it here as an axis lets the run's tolerance apply
# in that mode too, without any caller passing a direction.
_SCALAR_AXIS = Objective(name="perf_metric", direction="max")

# ---------------------------------------------------------------------------
# Individual
# ---------------------------------------------------------------------------


@dataclass
class Individual:
    """One candidate program in the population.

    The ``commit`` field is a git SHA in the workspace repo; the framework
    checks it out to materialize the individual's code on disk. Failed
    offspring are still retained (``passed=False``, ``commit=None``) so
    future mutators can read their judge feedback and avoid the same dead
    ends.

    Fitness has two complementary representations kept in sync:

    - ``perf_metric`` / ``perf_unit``: scalar primary metric (back-compat;
      used by single-objective selection and ``Population.best``).
    - ``metrics``: full dict reported by the profiler. Used for Pareto
      frontier computation when objectives are configured. Empty for
      single-objective runs.
    """

    id: int
    generation: int
    parent_id: int | None
    inspiration_ids: list[int] = field(default_factory=list)
    commit: str | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    passed: bool = False
    summary: str = ""
    feedback: str = ""
    policy_parent_id: str | None = None
    policy_target_island: int | None = None

    def __post_init__(self) -> None:  # noqa: D105  # tracked: #288
        self.validate_fitness()

    def validate_fitness(self) -> None:
        """Recheck mutable fitness fields before they affect selection."""
        _require_finite_metric(self.perf_metric, "perf_metric")
        for name, value in self.metrics.items():
            _require_finite_metric(value, f"metrics[{name!r}]")


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


class Population:
    """A flat archive of individuals, with fitness-weighted parent sampling
    and diversity-aware inspiration sampling.

    Failed individuals (``passed=False``) are kept but excluded from
    selection so the mutator only ever evolves from a working baseline.
    """  # noqa: D205  # tracked: #288

    def __init__(self, individuals: list[Individual] | None = None) -> None:  # noqa: D107  # tracked: #288
        self._individuals: list[Individual] = list(individuals or [])

    # -- accessors -----------------------------------------------------------

    @property
    def all(self) -> list[Individual]:  # noqa: D102  # tracked: #288
        return list(self._individuals)

    @property
    def passed(self) -> list[Individual]:  # noqa: D102  # tracked: #288
        passed = [i for i in self._individuals if i.passed and i.commit]
        for individual in passed:
            individual.validate_fitness()
        return passed

    def __len__(self) -> int:  # noqa: D105  # tracked: #288
        return len(self._individuals)

    def next_id(self) -> int:  # noqa: D102  # tracked: #288
        return (max((i.id for i in self._individuals), default=0)) + 1

    def get(self, ind_id: int) -> Individual | None:  # noqa: D102  # tracked: #288
        for i in self._individuals:
            if i.id == ind_id:
                return i
        return None

    def add(self, ind: Individual) -> None:  # noqa: D102  # tracked: #288
        ind.validate_fitness()
        self._individuals.append(ind)

    def best(self, space: MetricSpace) -> Individual | None:
        """Return the passed individual leading on the run's headline axis.

        Single-objective view; for multi-objective use ``frontier``. The axis
        is the space's primary objective, or ``perf_metric`` when the task
        declares none. Ties, including ties inside the declared tolerance, go
        to the latest id (most recent wins), which the reversed iteration order
        expresses to ``MetricSpace.best``.
        """
        headline = _headline_space(space)
        newest_first: list[Individual] = sorted(
            self.passed, key=lambda individual: individual.id, reverse=True
        )

        def reading(individual: Individual) -> Measurement | None:
            return _headline_reading(individual, headline)

        return headline.best(newest_first, reading)

    # -- frontier ------------------------------------------------------------

    def frontier(self, space: MetricSpace) -> list[Individual]:
        """Return the Pareto-non-dominated subset of passed individuals.

        Only individuals with values for *every* configured axis are eligible;
        a partial-metric individual is incomparable on the missing axis and is
        silently dropped from the frontier. Dominance is decided by *space*, so
        a candidate within the declared tolerance of a frontier member is not
        dominated by it.
        """
        return space.frontier(
            [individual for individual in self.passed if space.complete(individual.metrics)],
            lambda individual: individual.metrics,
        )

    # -- selection -----------------------------------------------------------

    def select_parent(
        self,
        *,
        rng: random.Random,
        temperature: float = 1.0,
        space: MetricSpace,
        frontier_bias: float = 0.7,
    ) -> Individual | None:
        """Sample a parent from passed individuals.

        Two modes:

        - **Pareto** (*space* has axes): with probability ``frontier_bias``
          draw uniformly from the frontier; otherwise fall back to the scalar
          softmax. If the frontier is empty (e.g. no individual has all
          required metrics yet) this also falls back to scalar softmax — keeps
          the loop unblocked early in a run before the profiler emits all axes.
        - **Scalar** (*space* has no axes): softmax over normalized
          ``perf_metric`` with ``temperature``. Lower temperature →
          greedy on the best; higher temperature → uniform.
        """
        if space.objectives:
            front = self.frontier(space)
            if front and rng.random() < frontier_bias:
                return rng.choice(front)
        return self._scalar_softmax_parent(rng=rng, temperature=temperature, space=space)

    def _scalar_softmax_parent(
        self,
        *,
        rng: random.Random,
        temperature: float,
        space: MetricSpace,
    ) -> Individual | None:
        ranked = [i for i in self.passed if i.perf_metric is not None]
        if not ranked:
            return None
        if len(ranked) == 1:
            return ranked[0]
        scores = [space.signed_primary(i.perf_metric) for i in ranked if i.perf_metric is not None]
        lo, hi = min(scores), max(scores)
        if hi - lo < 1e-12:  # noqa: PLR2004  # tracked: #288
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

    def select_inspirations(
        self,
        *,
        parent_id: int | None,
        k_top: int,
        k_random: int,
        rng: random.Random,
        space: MetricSpace,
    ) -> list[Individual]:
        """Pick a small set of peer individuals to show the mutator.

        Two modes:

        - **Pareto** (*space* has axes): take up to ``k_top`` from the frontier
          (parent excluded), then ``k_random`` random others. Frontier members
          are sorted by the *primary* objective so the strongest example on the
          headline axis comes first; the rest of the frontier still shows the
          mutator alternative axes. If the frontier is too small, the slack is
          filled from off-frontier passers. This ordering is a ranking, not a
          comparison, so it reads signed axis values directly.
        - **Scalar** (*space* has no axes): top-K-by-perf + random-K, as
          before.

        The parent is always excluded; duplicates are removed.
        """
        pool = [i for i in self.passed if i.id != parent_id]
        if not pool:
            return []

        primary = space.primary
        if primary is not None:
            front_ids = {i.id for i in self.frontier(space) if i.id != parent_id}
            front_pool = [i for i in pool if i.id in front_ids]
            front_pool.sort(
                key=lambda i: primary.signed(i.metrics.get(primary.name, float("-inf"))),
                reverse=True,
            )
            top = front_pool[:k_top]
            # Backfill from non-frontier if the frontier is smaller than k_top.
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


def _headline_space(space: MetricSpace) -> MetricSpace:
    """Return the space ``best`` orders in: the run's, or the scalar fallback.

    A task that declares objectives names its headline axis first. One that
    declares none is still measured with the run's tolerance, on the
    back-compat ``perf_metric`` axis.
    """
    if space.primary is not None:
        return space
    return MetricSpace(objectives=(_SCALAR_AXIS,), relative_noise=space.relative_noise)


def _headline_reading(individual: Individual, headline: MetricSpace) -> Measurement | None:
    """Read *individual* on the headline axis of an already-resolved space."""
    primary = headline.primary
    assert primary is not None  # noqa: S101  # guaranteed by _headline_space
    value = individual.metrics.get(primary.name, individual.perf_metric)
    if value is None:
        return None
    return Measurement(metric=primary.name, value=value)


def _require_finite_metric(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number")  # noqa: TRY003  # tracked: #288
