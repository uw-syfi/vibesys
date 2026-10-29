"""Seeded choices that depend on nothing but their inputs.

Parallel workstreams ask the scripted agents in any order, and a resumed run asks again, so a
choice cannot come from a generator whose state moves with each call. Each choice is a pure
function of the seed and the names it concerns: the same seed and names give the same answer in
every order, on every machine, in every process.
"""

from __future__ import annotations

import hashlib

_BITS = 64


def unit(seed: int, *names: str) -> float:
    """A number in ``[0, 1)`` fixed by ``seed`` and ``names``."""
    text = "\x00".join((str(seed), *names))
    digest = hashlib.sha256(text.encode()).digest()
    return int.from_bytes(digest[: _BITS // 8], "big") / 2**_BITS


def index(seed: int, size: int, *names: str) -> int:
    """An index in ``range(size)`` fixed by ``seed`` and ``names``."""
    if size < 1:
        message = "cannot choose from nothing"
        raise ValueError(message)
    return min(int(unit(seed, *names) * size), size - 1)
