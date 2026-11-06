"""Result protocol 2 records (``sdk/vs-evaluator/PROTOCOL.md``) written to ``--vs-output``.

A ``RecordStream`` appends ``hello`` first and at most one outcome after it, each through the
core (``vibesys-evaluator records write``), which owns the protocol: it flushes every line and
refuses any record a reader would reject, so a mistake in a task surfaces here with its name
rather than as an invalid stream later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from vibesys_evaluator._private import core

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vibesys_evaluator._private.results import Metric

PROTOCOL = 2
_DIRECTIONS = {"max": "DIRECTION_MAX", "min": "DIRECTION_MIN"}


class RecordError(ValueError):
    """A record the protocol does not allow (the message names the metric or rule)."""


@dataclass(frozen=True, kw_only=True)
class Partial:
    """What a failed run measured before it stopped (an ``error`` record's ``partial``)."""

    name: str
    value: float
    direction: Literal["max", "min"]
    unit: str = ""


def _number(value: object) -> float | str:
    """A JSON number for the core; a non-finite one travels as text, a non-number as NaN."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "NaN"
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    return number


class RecordStream:
    """The record stream of one sample, written as records are made."""

    def __init__(self, path: Path) -> None:
        """Name the stream's file; it is created (truncated) with the first record."""
        self._path = path
        self._fresh = True
        self._outcome: Literal["result", "error"] | None = None

    def _write(self, record: dict[str, Any]) -> None:
        request = {"path": str(self._path), "fresh": self._fresh, **record}
        try:
            response = core.call("records", "write", request)
        except core.CoreDomainError as error:
            if error.kind == "record":
                raise RecordError(error.message) from error
            raise
        self._fresh = False
        outcome = response.get("outcome", "")
        if outcome == "result":
            self._outcome = "result"
        elif outcome == "error":
            self._outcome = "error"

    @property
    def outcome(self) -> Literal["result", "error"] | None:
        """The outcome written so far, if any."""
        return self._outcome

    def hello(self, metrics: Mapping[str, Metric]) -> None:
        """Declare the metrics (first record).

        Raises:
            RecordError: A second hello, no metrics, or a metric name with whitespace.
        """
        for name in metrics:
            if not isinstance(name, str):
                msg = f"metric name {name!r} must be non-empty and contain no whitespace"
                raise RecordError(msg)
        specs = [
            {"name": name, "unit": metric.unit, "direction": _DIRECTIONS[metric.direction]}
            for name, metric in metrics.items()
        ]
        self._write({"hello": {"metrics": specs}})

    def result(self, values: Mapping[str, float]) -> None:
        """Write the measured row.

        Raises:
            RecordError: No hello yet, a second outcome, an undeclared or
                missing metric, or a value that is not a finite number.
        """
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                msg = f"metric {name} is {value!r}; report a finite number"
                raise RecordError(msg)
        self._write(
            {"result": {"values": [{"name": k, "value": _number(v)} for k, v in values.items()]}}
        )

    def error(self, message: str, *, partial: Partial | None = None) -> None:
        """Write an error record; it ends the stream (a result before it is ignored).

        Raises:
            RecordError: The stream already ended with an error.
        """
        record: dict[str, Any] = {"message": message}
        if partial is not None:
            record["partial"] = {
                "name": partial.name,
                "value": _number(partial.value),
                "direction": _DIRECTIONS[partial.direction],
                "unit": partial.unit,
            }
        self._write({"error": record})

    def close(self) -> None:
        """Nothing stays open between records; kept so a stream is used like a file."""
