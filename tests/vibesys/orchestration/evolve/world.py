"""A deterministic scripted world for evolve: what every candidate would do, keyed by its name.

The same world drives the legacy loop (when the trajectories in ``fixtures`` were recorded) and the
core strategy, so the population a given ``--seed`` produces can be compared across them. A
candidate is named by the slot that evaluates it: ``bootstrap.<k>`` for the k-th cold-start attempt
and ``g<generation>.c<slot>`` for a child. What it does (the judge's verdict, the trusted benchmark
row, the profiler's extra metric and the mutator's summary) is a pure function of the world's seed
and the name, so it does not depend on the order in which candidates complete.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Child:
    """What one candidate does under the world."""

    name: str
    judge_passes: bool
    throughput: float
    ttft_ms: float
    cache_hit: float
    summary: str


def _unit(seed: int, name: str, salt: str) -> float:
    """A reproducible number in [0, 1) for this seed, candidate and purpose."""
    digest = hashlib.sha256(f"{seed}:{name}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def bootstrap_name(attempt: int) -> str:
    """The name of the ``attempt``-th cold-start attempt (counted from 1)."""
    return f"bootstrap.{attempt}"


def child_name(generation: int, slot: int) -> str:
    """The name of the child in ``slot`` of ``generation`` (both counted from 1)."""
    return f"g{generation}.c{slot}"


def child(seed: int, name: str) -> Child:
    """The candidate called ``name`` under the world with this seed."""
    cold = name.startswith("bootstrap")
    passing_share = 0.45 if cold else 0.8
    return Child(
        name=name,
        judge_passes=_unit(seed, name, "judge") < passing_share,
        throughput=round(40.0 + 120.0 * _unit(seed, name, "throughput"), 3),
        ttft_ms=round(10.0 + 90.0 * _unit(seed, name, "ttft"), 3),
        cache_hit=round(_unit(seed, name, "cache"), 3),
        summary=f"candidate {name} of world {seed}",
    )


def bootstrap_names(seed: int, attempts: int) -> list[str]:
    """The cold-start attempts the world runs: up to ``attempts``, ending at the first pass."""
    names: list[str] = []
    for number in range(1, attempts + 1):
        names.append(bootstrap_name(number))
        if child(seed, names[-1]).judge_passes:
            break
    return names


def generation_names(generations: int, children: int) -> list[str]:
    """Every child of generations 1 to ``generations`` in slot order."""
    return [
        child_name(generation, slot)
        for generation in range(1, generations + 1)
        for slot in range(1, children + 1)
    ]


def serial_order(seed: int, *, attempts: int, generations: int, children: int) -> list[str]:
    """The names in the order a serial run evaluates them."""
    names = bootstrap_names(seed, attempts)
    if not child(seed, names[-1]).judge_passes:
        return names
    return [*names, *generation_names(generations, children)]
