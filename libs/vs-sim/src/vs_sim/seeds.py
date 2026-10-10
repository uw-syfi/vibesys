"""Seed plumbing: every simulated test has one seed, and one flag replays it."""

from __future__ import annotations

from vs_sim.randomness import SeededRandom, derive_seed

SEED_OPTION = "--sim-seed"


def seed_for_test(test_id: str, override: int | None = None) -> int:
    """The seed ``test_id`` runs under: ``override`` when given, otherwise a pure function of the id.

    A fixed default keeps a green suite green; exploring other seeds is an explicit choice
    (``override``), and a failing run prints the seed that replays it.
    """
    return override if override is not None else derive_seed(0, test_id)


def replay_hint(seed: int) -> str:
    """The command-line option that runs a test under ``seed`` again."""
    return f"{SEED_OPTION}={seed}"


def random_for(seed: int, label: str) -> SeededRandom:
    """The random stream ``label`` draws from under ``seed``; independent of every other label."""
    return SeededRandom(derive_seed(seed, label))
