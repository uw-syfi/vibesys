"""Result protocol 2 records (``sdk/vs-evaluator/PROTOCOL.md``) written to ``--vs-output``.

A ``RecordStream`` writes ``hello`` first and at most one outcome after it,
flushing each line, and refuses any record a reader would reject, so a
mistake in a task surfaces here with its name rather than as an invalid
stream later.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TextIO

from vibesys_evaluator._private.results import finite, valid_metric_name

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vibesys_evaluator._private.results import Metric

PROTOCOL = 2
_MAX_MESSAGE = 4000


class RecordError(ValueError):
    """A record the protocol does not allow (the message names the metric or rule)."""


@dataclass(frozen=True, kw_only=True)
class Partial:
    """What a failed run measured before it stopped (an ``error`` record's ``partial``)."""

    name: str
    value: float
    direction: Literal["max", "min"]
    unit: str = ""


class RecordStream:
    """The record stream of one sample, written as records are made."""

    def __init__(self, path: Path) -> None:
        """Open (truncate) the stream at ``path``."""
        self._path = path
        self._file: TextIO | None = None
        self._metrics: dict[str, Metric] | None = None
        self._outcome: Literal["result", "error"] | None = None

    def _write(self, record: dict[str, object]) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("w", encoding="utf-8")
        self._file.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    @property
    def outcome(self) -> Literal["result", "error"] | None:
        """The outcome written so far, if any."""
        return self._outcome

    def hello(self, metrics: Mapping[str, Metric]) -> None:
        """Declare the metrics (first record).

        Raises:
            RecordError: A second hello, no metrics, or a metric name with whitespace.
        """
        if self._metrics is not None or self._outcome is not None:
            msg = "hello is written once, first"
            raise RecordError(msg)
        if not metrics:
            msg = "declare at least one metric"
            raise RecordError(msg)
        for name in metrics:
            if not valid_metric_name(name):
                msg = f"metric name {name!r} must be non-empty and contain no whitespace"
                raise RecordError(msg)
        self._metrics = dict(metrics)
        specs: dict[str, dict[str, str]] = {}
        for name, metric in metrics.items():
            spec = {"direction": metric.direction}
            if metric.unit:
                spec["unit"] = metric.unit
            specs[name] = spec
        self._write({"kind": "hello", "protocol": PROTOCOL, "metrics": specs})

    def result(self, values: Mapping[str, float]) -> None:
        """Write the measured row.

        Raises:
            RecordError: No hello yet, a second outcome, an undeclared or
                missing metric, or a value that is not a finite number.
        """
        if self._metrics is None:
            msg = "write hello before the result"
            raise RecordError(msg)
        if self._outcome is not None:
            msg = f"the stream already has its outcome ({self._outcome}); report once"
            raise RecordError(msg)
        unknown = sorted(set(values) - set(self._metrics))
        if unknown:
            msg = f"reported {unknown} but declared only {sorted(self._metrics)}"
            raise RecordError(msg)
        missing = sorted(set(self._metrics) - set(values))
        if missing:
            msg = f"declared {missing} but did not report them"
            raise RecordError(msg)
        for name, value in values.items():
            if not finite(value):
                msg = f"metric {name} is {value!r}; report a finite number"
                raise RecordError(msg)
        self._outcome = "result"
        self._write(
            {"kind": "result", "label": "", "values": {k: float(v) for k, v in values.items()}}
        )

    def error(self, message: str, *, partial: Partial | None = None) -> None:
        """Write an error record; it ends the stream (a result before it is ignored).

        Raises:
            RecordError: The stream already ended with an error.
        """
        if self._outcome == "error":
            msg = "the stream already ended with an error"
            raise RecordError(msg)
        text = message.strip() or "the benchmark failed without a message"
        if len(text) > _MAX_MESSAGE:
            text = text[: _MAX_MESSAGE - 3] + "..."
        record: dict[str, object] = {"kind": "error", "message": text}
        if partial is not None and valid_metric_name(partial.name) and finite(partial.value):
            part: dict[str, object] = {
                "name": partial.name,
                "value": float(partial.value),
                "direction": partial.direction,
            }
            if partial.unit:
                part["unit"] = partial.unit
            record["partial"] = part
        self._outcome = "error"
        self._write(record)

    def close(self) -> None:
        """Close the file (idempotent)."""
        if self._file is not None:
            self._file.close()
            self._file = None
