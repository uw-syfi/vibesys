"""``Metric`` and ``Validity``: plain data of a benchmark's outcome and rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, kw_only=True)
class Metric:
    """A metric the benchmark reports (declared in the ``hello`` record).

    Attributes:
        unit: Its unit, for people ("ops/s", "ms").
        direction: Which way is better.
    """

    unit: str
    direction: Literal["max", "min"]

    def __post_init__(self) -> None:
        """Reject a direction the result protocol does not have."""
        if self.direction not in ("max", "min"):
            msg = f"Metric direction is 'max' or 'min', got {self.direction!r}"
            raise ValueError(msg)
        if not isinstance(self.unit, str):
            msg = f"Metric unit is a string, got {self.unit!r}"
            raise TypeError(msg)


@dataclass(frozen=True, kw_only=True)
class Validity:
    """When a phase is a measurement; a phase that breaks a rule fails the sample.

    Attributes:
        max_error_rate: loadgen's errors (transport, timeout, status, capture)
            over the window's operations, at most.
        min_sustained_ratio: Open loop: the offered rate over the target rate, at least.
        min_ops_per_operation: Every operation of the mix completed in the
            window at least this many times.
    """

    max_error_rate: float = 0.0
    min_sustained_ratio: float = 0.95
    min_ops_per_operation: int = 1

    def __post_init__(self) -> None:
        """Reject rules no phase could meet or that mean nothing."""
        if not (0.0 <= self.max_error_rate <= 1.0):
            msg = f"Validity max_error_rate is in [0, 1], got {self.max_error_rate!r}"
            raise ValueError(msg)
        if not (0.0 < self.min_sustained_ratio <= 1.0):
            msg = f"Validity min_sustained_ratio is in (0, 1], got {self.min_sustained_ratio!r}"
            raise ValueError(msg)
        if (
            isinstance(self.min_ops_per_operation, bool)
            or not isinstance(self.min_ops_per_operation, int)
            or self.min_ops_per_operation < 0
        ):
            msg = (
                f"Validity min_ops_per_operation is an int >= 0, got {self.min_ops_per_operation!r}"
            )
            raise ValueError(msg)


def finite(value: float) -> bool:
    """A finite number that is not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
