"""Population state: individuals, selection, and JSON persistence.

This module is pure logic — no agent invocations, no filesystem IO beyond
loading / saving the population JSON. Keeping it free of vibeserve runtime
imports lets the unit tests run without a GPU or sandbox.

## Single-objective vs multi-objective modes

The module supports two selection modes:

- **Scalar** (objectives=None): rank by ``Individual.perf_metric``;
  parent sampling is a softmax over normalized fitness. Used when the
  caller doesn't provide an objective list (the default for
  back-compatibility with runs that predate Pareto support).

- **Multi-objective Pareto** (objectives=[...]): keep a non-dominated
  *frontier* over ``Individual.metrics``; with probability
  ``frontier_bias`` parent selection draws uniformly from the frontier,
  otherwise it falls back to the scalar softmax over the *primary*
  objective. Inspirations are pulled from the frontier first so the
  mutator sees diverse strategies, not just the throughput champion.

The two modes share data structures: every passing individual stores
both ``perf_metric`` (back-compat scalar) and ``metrics`` (the dict the
profiler reported). Mode is chosen at the call site, not at the data
layer.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Objective spec + dominance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Objective:
    """One axis of the fitness frontier.

    ``name`` is the key in ``Individual.metrics`` the profiler must
    report. ``direction`` is ``"max"`` or ``"min"``; the framework
    flips the sign when comparing min-objectives so dominance logic can
    treat every axis as "higher is better" internally.
    """

    name: str
    direction: str  # "max" or "min"

    def __post_init__(self) -> None:
        if self.direction not in ("max", "min"):
            raise ValueError(
                f"Objective.direction must be 'max' or 'min', got {self.direction!r}"
            )

    def signed(self, value: float) -> float:
        """Return *value* flipped to "higher is better" semantics.

        Used by the dominance helper so callers don't have to branch on
        direction. Min objectives are negated; max objectives pass through.
        """
        return value if self.direction == "max" else -value


def _dominates(a: "Individual", b: "Individual", objectives: list[Objective]) -> bool:
    """True if ``a`` Pareto-dominates ``b`` under *objectives*.

    Convention: a dominates b iff for every objective, a is at-least-as
    good as b, AND for at least one objective, a is strictly better.
    Both must have a value for every objective name; missing values are
    treated as "incomparable" (neither dominates).
    """
    if not objectives:
        return False
    strictly_better_anywhere = False
    for obj in objectives:
        a_val = a.metrics.get(obj.name)
        b_val = b.metrics.get(obj.name)
        if a_val is None or b_val is None:
            return False
        a_eff = obj.signed(a_val)
        b_eff = obj.signed(b_val)
        if a_eff < b_eff:
            return False  # a worse on this axis → can't dominate
        if a_eff > b_eff:
            strictly_better_anywhere = True
    return strictly_better_anywhere


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

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Individual":
        return cls(
            id=int(data["id"]),
            generation=int(data["generation"]),
            parent_id=data.get("parent_id"),
            inspiration_ids=list(data.get("inspiration_ids") or []),
            commit=data.get("commit"),
            perf_metric=data.get("perf_metric"),
            perf_unit=data.get("perf_unit"),
            metrics=dict(data.get("metrics") or {}),
            passed=bool(data.get("passed", False)),
            summary=data.get("summary", ""),
            feedback=data.get("feedback", ""),
        )


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


class Population:
    """A flat archive of individuals, with fitness-weighted parent sampling
    and diversity-aware inspiration sampling.

    Failed individuals (``passed=False``) are kept but excluded from
    selection so the mutator only ever evolves from a working baseline.
    """

    def __init__(self, individuals: list[Individual] | None = None) -> None:
        self._individuals: list[Individual] = list(individuals or [])

    # -- accessors -----------------------------------------------------------

    @property
    def all(self) -> list[Individual]:
        return list(self._individuals)

    @property
    def passed(self) -> list[Individual]:
        return [i for i in self._individuals if i.passed and i.commit]

    def __len__(self) -> int:
        return len(self._individuals)

    def next_id(self) -> int:
        return (max((i.id for i in self._individuals), default=0)) + 1

    def get(self, ind_id: int) -> Individual | None:
        for i in self._individuals:
            if i.id == ind_id:
                return i
        return None

    def add(self, ind: Individual) -> None:
        self._individuals.append(ind)

    def best(self) -> Individual | None:
        """Return the passed individual with the highest ``perf_metric``.

        Single-objective view; for multi-objective use ``frontier``.
        Ties broken by latest id (most recent wins).
        """
        best: Individual | None = None
        for ind in self.passed:
            if ind.perf_metric is None:
                continue
            if best is None or ind.perf_metric > best.perf_metric or (
                ind.perf_metric == best.perf_metric and ind.id > best.id
            ):
                best = ind
        return best

    # -- frontier ------------------------------------------------------------

    def frontier(self, objectives: list[Objective]) -> list[Individual]:
        """Return the Pareto-non-dominated subset of passed individuals.

        Only individuals with values for *every* objective name are
        eligible — a partial-metric individual is incomparable on the
        missing axis and silently dropped from the frontier.
        """
        if not objectives:
            return []
        eligible = [
            i for i in self.passed
            if all(o.name in i.metrics for o in objectives)
        ]
        non_dominated: list[Individual] = []
        for cand in eligible:
            if not any(_dominates(other, cand, objectives) for other in eligible if other.id != cand.id):
                non_dominated.append(cand)
        return non_dominated

    # -- selection -----------------------------------------------------------

    def select_parent(
        self,
        *,
        rng: random.Random,
        temperature: float = 1.0,
        objectives: list[Objective] | None = None,
        frontier_bias: float = 0.7,
    ) -> Individual | None:
        """Sample a parent from passed individuals.

        Two modes:

        - **Pareto** (``objectives`` provided): with probability
          ``frontier_bias`` draw uniformly from the frontier; otherwise
          fall back to the scalar softmax. If the frontier is empty
          (e.g. no individual has all required metrics yet) this also
          falls back to scalar softmax — keeps the loop unblocked
          early in a run before the profiler emits all axes.
        - **Scalar** (``objectives=None``): softmax over normalized
          ``perf_metric`` with ``temperature``. Lower temperature →
          greedy on the best; higher temperature → uniform.
        """
        if objectives:
            front = self.frontier(objectives)
            if front and rng.random() < frontier_bias:
                return rng.choice(front)
        return self._scalar_softmax_parent(rng=rng, temperature=temperature)

    def _scalar_softmax_parent(
        self,
        *,
        rng: random.Random,
        temperature: float,
    ) -> Individual | None:
        ranked = [i for i in self.passed if i.perf_metric is not None]
        if not ranked:
            return None
        if len(ranked) == 1:
            return ranked[0]
        perfs = [i.perf_metric for i in ranked]
        lo, hi = min(perfs), max(perfs)
        if hi - lo < 1e-12:
            return rng.choice(ranked)
        normed = [(p - lo) / (hi - lo) for p in perfs]
        t = max(temperature, 1e-6)
        logits = [n / t for n in normed]
        m = max(logits)
        exps = [math.exp(x - m) for x in logits]
        total = sum(exps)
        r = rng.random() * total
        acc = 0.0
        for ind, w in zip(ranked, exps):
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
        objectives: list[Objective] | None = None,
    ) -> list[Individual]:
        """Pick a small set of peer individuals to show the mutator.

        Two modes:

        - **Pareto** (``objectives`` provided): take up to ``k_top`` from
          the frontier (parent excluded), then ``k_random`` random
          others. Frontier members are sorted by the *primary*
          objective (first in the list) so the strongest example on the
          headline axis comes first; the rest of the frontier still
          shows the mutator alternative axes. If the frontier is too
          small, the slack is filled from off-frontier passers.
        - **Scalar** (``objectives=None``): top-K-by-perf + random-K, as
          before.

        The parent is always excluded; duplicates are removed.
        """
        pool = [i for i in self.passed if i.id != parent_id]
        if not pool:
            return []

        if objectives:
            front_ids = {i.id for i in self.frontier(objectives) if i.id != parent_id}
            front_pool = [i for i in pool if i.id in front_ids]
            primary = objectives[0]
            front_pool.sort(
                key=lambda i: primary.signed(i.metrics.get(primary.name, float("-inf"))),
                reverse=True,
            )
            top = front_pool[:k_top]
            # Backfill from non-frontier if the frontier is smaller than k_top.
            if len(top) < k_top:
                non_front = [i for i in pool if i.id not in front_ids and i.perf_metric is not None]
                non_front.sort(key=lambda i: i.perf_metric, reverse=True)
                top.extend(non_front[: k_top - len(top)])
        else:
            ranked = [i for i in pool if i.perf_metric is not None]
            ranked.sort(key=lambda i: i.perf_metric, reverse=True)
            top = ranked[:k_top]

        top_ids = {i.id for i in top}
        rest = [i for i in pool if i.id not in top_ids]
        rnd = rng.sample(rest, k=min(k_random, len(rest))) if rest else []
        return top + rnd

    # -- persistence ---------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([i.to_json() for i in self._individuals], indent=2))

    @classmethod
    def load(cls, path: Path) -> "Population":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls([Individual.from_json(d) for d in data])
