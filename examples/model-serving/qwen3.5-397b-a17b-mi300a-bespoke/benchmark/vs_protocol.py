#!/usr/bin/env python3
"""Writer for the VibeSys evaluator result protocol, version 2.

The protocol is specified in ``sdk/vs-evaluator/PROTOCOL.md``; ``vseval`` in
the same directory is the Go SDK for it. This module is the small Python
equivalent this bundle needs, kept next to the benchmark that emits it so the
bundle stays self-contained (the benchmark is task-owned and runs from the
candidate workspace, which has no VibeSys packages installed).

The stream is one JSON object per line, written to the path VibeSys passes
under ``--vs-output``:

    {"kind":"hello","protocol":2,"metrics":{...}}
    {"kind":"result","label":"","values":{...}}

or, when the run could not be measured, a single ``error`` record. ``hello`` is
written and flushed as soon as the schema is known, before any measuring, so a
crash or a timeout leaves a schema with no outcome ("it started and died")
rather than an empty file ("this producer does not speak the protocol").

A report constructed with ``path=None`` validates every call exactly as a
reporting one does but writes nothing. That is the mode a manual run without
``--vs-output`` uses, so a producer bug surfaces off the cluster too.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Literal

PROTOCOL_VERSION = 2

# The flag VibeSys appends to a benchmark command declaring
# ``result_protocol``. Fixed by the framework for every evaluator; see
# ``PROTOCOL_OUTPUT_FLAG`` in ``src/vibesys/loops/gates.py``.
OUTPUT_FLAG = "--vs-output"


@dataclasses.dataclass(frozen=True, slots=True)
class MetricSpec:
    """How one produced metric is declared in the ``hello`` record.

    ``direction`` is advisory metadata about the metric itself; it does not
    select objectives, which live in the task's ``objectives.toml``.

    ``required=False`` marks a metric the benchmark can only sometimes
    produce, such as a percentile over a sample set that can be empty. An
    optional metric may be left out of the result row, and ``emit`` drops one
    whose value is not finite instead of writing a number the reader must
    reject. A metric named by an objective must stay required: the framework
    refuses to rank on an axis a successful run may omit.
    """

    unit: str | None = None
    direction: Literal["max", "min"] | None = None
    required: bool = True

    def as_record(self) -> dict[str, object]:
        """Render the spec as the wire object, omitting defaults.

        ``required`` is written only when false: the corpus compares records
        semantically and an explicit ``true`` would not match its counterpart.
        """
        record: dict[str, object] = {}
        if self.unit is not None:
            record["unit"] = self.unit
        if self.direction is not None:
            record["direction"] = self.direction
        if not self.required:
            record["required"] = False
        return record


class ProtocolError(RuntimeError):
    """A record the producer tried to write would not satisfy the protocol."""


class ProtocolReport:
    """One evaluator record stream, opened for the life of a benchmark run.

    Use it as a context manager: ``close`` is the only thing that releases the
    file and is idempotent. Exactly one outcome record (``result`` or
    ``error``) may be written, and every record is flushed as it is produced,
    so a stream truncated by a kill still holds everything already reported.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._handle: IO[str] | None = None
        self._metrics: dict[str, MetricSpec] | None = None
        self._outcome_written = False

    def __enter__(self) -> ProtocolReport:
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the output file. Idempotent."""
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    @property
    def reporting(self) -> bool:
        """Whether this report writes a stream anyone will read."""
        return self._path is not None

    def declare(self, metrics: Mapping[str, MetricSpec]) -> None:
        """Write the ``hello`` record. Call once, before measuring anything."""
        if self._metrics is not None:
            raise ProtocolError("hello was already written")
        if not metrics:
            raise ProtocolError("hello must declare at least one metric")
        for name in metrics:
            if not name or any(character.isspace() for character in name):
                raise ProtocolError(f"invalid metric name {name!r}: empty or contains whitespace")
        self._metrics = dict(metrics)
        self._write(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "metrics": {name: spec.as_record() for name, spec in metrics.items()},
            }
        )

    def emit(self, values: Mapping[str, float]) -> None:
        """Write the ``result`` record for a completed measurement.

        Every name must have been declared. A non-finite value is dropped when
        its metric is optional and rejected when it is required, because the
        reader rejects the whole stream over one such number.
        """
        if self._metrics is None:
            raise ProtocolError("result was written before hello")
        if self._outcome_written:
            raise ProtocolError("the outcome record was already written")
        row = self._checked_row(values)
        missing = sorted(
            name for name, spec in self._metrics.items() if spec.required and name not in row
        )
        if missing:
            raise ProtocolError(f"required metrics were never reported: {', '.join(missing)}")
        self._outcome_written = True
        self._write({"kind": "result", "label": "", "values": row})

    def fail(self, message: str) -> None:
        """Write the ``error`` record. Valid with or without a preceding ``hello``."""
        if self._outcome_written:
            raise ProtocolError("the outcome record was already written")
        if not message:
            raise ProtocolError("error.message must be non-empty")
        self._outcome_written = True
        self._write({"kind": "error", "message": message})

    def _checked_row(self, values: Mapping[str, float]) -> dict[str, float]:
        """Validate and filter a measured row against the declared metrics."""
        assert self._metrics is not None  # noqa: S101 -- guarded by emit()
        row: dict[str, float] = {}
        for name, value in values.items():
            spec = self._metrics.get(name)
            if spec is None:
                raise ProtocolError(f"metric {name!r} is not declared in hello")
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ProtocolError(f"metric {name!r} is not a number: {value!r}")
            number = float(value)
            if math.isfinite(number):
                row[name] = number
            elif spec.required:
                raise ProtocolError(f"required metric {name!r} is not finite: {value!r}")
        return row

    def _write(self, record: Mapping[str, object]) -> None:
        if self._handle is None:
            return
        self._handle.write(json.dumps(record) + "\n")
        self._handle.flush()
