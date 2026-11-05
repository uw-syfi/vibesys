"""``Rand``: deterministic, labelled random streams (part of the versioned contract).

A stream is SplitMix64 started from a state derived from a seed and labels,
equal to the Go generators' ``newRNG(seed, label, index)`` for one string and
one integer label, so a dataset ported from Go draws the same values. Changing
any algorithm here changes every task's requests: it is a breaking change.

Label folding, from ``state = seed``: a string label ``s`` sets
``pending = state ^ fnv1a64(s)``; an integer label ``n`` then sets
``state = mix(pending, n)`` (or ``mix(state, n)`` when no string is pending);
a string with no integer after it is closed with ``state = finalize(pending)``
(``mix`` with stream ``2**64 - 1``). ``child(*more)`` is the stream of
``Rand(seed, *labels, *more)``: it depends on the parent's labels only, never
on how much the parent has drawn.

Every draw is computed with IEEE 754 double operations that are exact or
correctly rounded (``+ - * /``, ``sqrt``, ``ldexp``, ``frexp``), never with the
platform's ``pow``, ``exp`` or ``log``, so a stream is the same on every
platform and in the Go SDK's ``workload.Rand`` (checked by
``fixtures/rand.json``). A Zipf exponent other than 1, 1/2 and 3/4 uses
``exp(-s log k)`` with the fdlibm algorithms below.
"""

from __future__ import annotations

import bisect
import math
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_MASK = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_UNIT = 1.0 / (1 << 53)
_SQRT3 = math.sqrt(3.0)
_HALF = 0.5
_THREE_QUARTERS = 0.75


def _finalize(value: int) -> int:
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK
    return value ^ (value >> 31)


def _mix(random: int, stream: int) -> int:
    return _finalize((random + _GOLDEN * (stream + 1)) & _MASK)


def _fnv1a(label: str) -> int:
    value = _FNV_OFFSET
    for byte in label.encode():
        value = ((value ^ byte) * _FNV_PRIME) & _MASK
    return value


def _fold(state: int, pending: int | None, labels: Sequence[str | int]) -> tuple[int, int | None]:
    """Fold ``labels`` into ``(state, pending)``; the stream starts at ``_start`` of the result."""
    for label in labels:
        if isinstance(label, bool) or not isinstance(label, str | int):
            msg = f"Rand labels are str or int, got {label!r}"
            raise TypeError(msg)
        if isinstance(label, str):
            if pending is not None:
                state = _finalize(pending)
            pending = state ^ _fnv1a(label)
            continue
        if label < 0:
            msg = f"Rand integer labels are >= 0, got {label}"
            raise ValueError(msg)
        state = _mix(state if pending is None else pending, label & _MASK)
        pending = None
    return state, pending


def _start(state: int, pending: int | None) -> int:
    return state if pending is None else _finalize(pending)


@lru_cache(maxsize=64)
def _zipf_cumulative(n: int, s: float) -> tuple[float, ...]:
    """Normalized cumulative weights rank^-s of ranks 1..n, as the Go ``NewZipf`` builds them."""
    weights = [_zipf_weight(k, s) for k in range(1, n + 1)]
    total = 0.0
    for weight in weights:
        total += weight
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)
    return tuple(cumulative)


def _zipf_weight(rank: int, s: float) -> float:
    # The exponents the Go generators use are computed with square roots only, so the
    # weights are bit-identical to theirs on every platform.
    k = float(rank)
    if s == 1.0:
        return 1.0 / k
    if s == _HALF:
        return 1.0 / math.sqrt(k)
    if s == _THREE_QUARTERS:
        return 1.0 / (math.sqrt(k) * math.sqrt(math.sqrt(k)))
    return _exp(-s * _log(k))


# fdlibm's e_log.c and e_exp.c (as Go's math package ports them), for finite x > 0 and finite x:
# the same operations in the same order in Go's workload.Rand, so the results are bit-identical.
_LN2_HI = 6.93147180369123816490e-01
_LN2_LO = 1.90821492927058770002e-10
_LOG2E = 1.44269504088896338700e00
_SQRT_HALF = 7.07106781186547524401e-01
_L = (
    6.666666666666735130e-01,
    3.999999999940941908e-01,
    2.857142874366239149e-01,
    2.222219843214978396e-01,
    1.818357216161805012e-01,
    1.531383769920937332e-01,
    1.479819860511658591e-01,
)
_P = (
    1.66666666666666657415e-01,
    -2.77777777770155933842e-03,
    6.61375632143793436117e-05,
    -1.65339022054652515390e-06,
    4.13813679705723846039e-08,
)
_EXP_OVERFLOW = 7.09782712893383973096e02
_EXP_UNDERFLOW = -7.45133219101941108420e02
_NEAR_ZERO = 1.0 / (1 << 28)


def _log(x: float) -> float:
    f1, ki = math.frexp(x)
    if f1 < _SQRT_HALF:
        f1 *= 2
        ki -= 1
    f = f1 - 1
    k = float(ki)
    s = f / (2 + f)
    s2 = s * s
    s4 = s2 * s2
    t1 = s2 * (_L[0] + s4 * (_L[2] + s4 * (_L[4] + s4 * _L[6])))
    t2 = s4 * (_L[1] + s4 * (_L[3] + s4 * _L[5]))
    r = t1 + t2
    hfsq = 0.5 * f * f
    return k * _LN2_HI - ((hfsq - (s * (hfsq + r) + k * _LN2_LO)) - f)


def _exp(x: float) -> float:
    if x > _EXP_OVERFLOW:
        return math.inf
    if x < _EXP_UNDERFLOW:
        return 0.0
    if -_NEAR_ZERO < x < _NEAR_ZERO:
        return 1 + x
    k = int(_LOG2E * x - 0.5) if x < 0 else int(_LOG2E * x + 0.5)
    hi = x - k * _LN2_HI
    lo = k * _LN2_LO
    r = hi - lo
    t = r * r
    c = r - t * (_P[0] + t * (_P[1] + t * (_P[2] + t * (_P[3] + t * _P[4]))))
    y = 1 - ((lo - (r * c) / (2 - c)) - hi)
    return math.ldexp(y, k)


class Rand:
    """A deterministic random stream named by a seed and labels.

    ``Rand(seed, "user", 3)`` is the same stream in every process and version.
    Draws advance the stream; ``child`` derives an independent stream from
    the labels alone.
    """

    __slots__ = ("_key", "_labels", "_seed", "_state")

    def __init__(self, seed: int, *labels: str | int) -> None:
        """Start the stream of ``seed`` and ``labels``.

        Args:
            seed: Any integer; it is taken modulo 2**64.
            *labels: Strings and non-negative integers naming the stream.
        """
        if isinstance(seed, bool) or not isinstance(seed, int):
            msg = f"Rand seed is an int, got {seed!r}"
            raise TypeError(msg)
        self._seed = seed
        self._labels = labels
        self._key = _fold(seed & _MASK, None, labels)
        self._state = _start(*self._key)

    def __repr__(self) -> str:
        """The stream's name."""
        return f"Rand({', '.join(repr(x) for x in (self._seed, *self._labels))})"

    def child(self, *labels: str | int) -> Rand:
        """The stream ``Rand(seed, *self_labels, *labels)``, whatever this one has drawn.

        Args:
            *labels: More labels.

        Returns:
            A fresh stream.
        """
        child = Rand.__new__(Rand)
        child._seed = self._seed
        child._labels = (*self._labels, *labels)
        child._key = _fold(*self._key, labels)
        child._state = _start(*child._key)
        return child

    def _next(self) -> int:
        self._state = (self._state + _GOLDEN) & _MASK
        return _finalize(self._state)

    def random(self) -> float:
        """A float uniform in [0, 1), from 53 bits of one draw."""
        return (self._next() >> 11) * _UNIT

    def below(self, n: int) -> int:
        """An integer in [0, n): one draw modulo ``n`` (as the Go generators' ``intn``).

        Args:
            n: The bound, at least 1.

        Returns:
            The integer.
        """
        if n < 1:
            msg = f"Rand.below needs n >= 1, got {n}"
            raise ValueError(msg)
        return self._next() % n

    def weighted(self, weights: Sequence[float]) -> int:
        """An index drawn with probability proportional to its weight.

        Integer weights draw ``below(sum)`` and walk the weights (as the Go
        generators' ``pick``); other weights draw ``random() * sum``.

        Args:
            weights: Non-negative weights with a positive sum.

        Returns:
            The index; never one of weight 0.
        """
        if not weights or any(w < 0 or not math.isfinite(w) for w in weights):
            msg = f"Rand.weighted needs finite non-negative weights, got {list(weights)!r}"
            raise ValueError(msg)
        if all(float(w).is_integer() for w in weights):
            total = int(sum(int(w) for w in weights))
            if total < 1:
                msg = "Rand.weighted needs a positive sum of weights"
                raise ValueError(msg)
            draw = self.below(total)
            for index, weight in enumerate(weights):
                if draw < weight:
                    return index
                draw -= int(weight)
            return len(weights) - 1  # pragma: no cover - the walk always ends above
        total_f = math.fsum(weights)
        if total_f <= 0:
            msg = "Rand.weighted needs a positive sum of weights"
            raise ValueError(msg)
        point = self.random() * total_f
        last = max(i for i, w in enumerate(weights) if w > 0)
        for index, weight in enumerate(weights):
            if point < weight:
                return index
            point -= weight
        return last

    def normal(self) -> float:
        """An approximately standard normal value: Irwin-Hall of four uniforms, within 2 sqrt(3)."""
        return (self.random() + self.random() + self.random() + self.random() - 2.0) * _SQRT3

    def zipf(self, n: int, s: float) -> int:
        """A rank in [0, n) with weight (rank + 1)^-s, by inverse CDF of one uniform draw.

        Args:
            n: The number of ranks, at least 1.
            s: The exponent, at least 0.

        Returns:
            The rank; 0 is the most likely.
        """
        if n < 1 or not (s >= 0 and math.isfinite(s)):
            msg = f"Rand.zipf needs n >= 1 and a finite s >= 0, got n={n}, s={s}"
            raise ValueError(msg)
        cumulative = _zipf_cumulative(n, float(s))
        index = bisect.bisect_left(cumulative, self.random() * cumulative[-1])
        return min(index, n - 1)
