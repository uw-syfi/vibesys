"""Seeded randomness that stays reproducible when a consumer is added or removed."""

from __future__ import annotations

import hashlib
import random
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import MutableSequence, Sequence


def derive_seed(root: int, label: str) -> int:
    """A seed for the stream called ``label`` under ``root``; the same pair always gives the same seed."""
    digest = hashlib.sha256(f"{root}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class RandomSource(Protocol):
    """Where product code and Fakes draw random choices from."""

    def random(self) -> float:
        """A float in ``[0, 1)``."""
        ...

    def randint(self, low: int, high: int) -> int:
        """An integer in ``[low, high]``."""
        ...

    def choice[T](self, items: Sequence[T]) -> T:
        """One of ``items``; raises ``IndexError`` when it is empty."""
        ...

    def shuffle(self, items: MutableSequence[Any]) -> None:
        """Reorder ``items`` in place."""
        ...

    def fork(self, label: str) -> RandomSource:
        """An independent source for ``label``; drawing from it does not move this one."""
        ...


class SeededRandom:
    """A reproducible source: the same seed and the same draws give the same values."""

    def __init__(self, seed: int) -> None:
        """Start the stream at ``seed``."""
        self.seed = seed
        self._random = random.Random(seed)  # noqa: S311  # LW-163801 [S311]; a reproducible simulation stream, not a security value.

    def random(self) -> float:
        """A float in ``[0, 1)``."""
        return self._random.random()

    def randint(self, low: int, high: int) -> int:
        """An integer in ``[low, high]``."""
        return self._random.randint(low, high)

    def choice[T](self, items: Sequence[T]) -> T:
        """One of ``items``."""
        return self._random.choice(items)

    def shuffle(self, items: MutableSequence[Any]) -> None:
        """Reorder ``items`` in place."""
        self._random.shuffle(items)

    def fork(self, label: str) -> SeededRandom:
        """An independent stream whose seed depends only on this seed and ``label``."""
        return SeededRandom(derive_seed(self.seed, label))


class SystemRandomSource:
    """The operating system's entropy: not reproducible, for production wiring."""

    def __init__(self) -> None:
        """Open the system source."""
        self._random = random.SystemRandom()

    def random(self) -> float:
        """A float in ``[0, 1)``."""
        return self._random.random()

    def randint(self, low: int, high: int) -> int:
        """An integer in ``[low, high]``."""
        return self._random.randint(low, high)

    def choice[T](self, items: Sequence[T]) -> T:
        """One of ``items``."""
        return self._random.choice(items)

    def shuffle(self, items: MutableSequence[Any]) -> None:
        """Reorder ``items`` in place."""
        self._random.shuffle(items)

    def fork(self, label: str) -> SystemRandomSource:  # noqa: ARG002  # LW-163802 [ARG002]; the label is part of the RandomSource contract and a system source has one stream.
        """The system source has one stream, so a fork is another handle on it."""
        return self
