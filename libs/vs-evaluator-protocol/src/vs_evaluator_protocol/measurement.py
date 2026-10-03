"""Cross-record validation of an evaluator record stream."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_evaluator_protocol.errors import ReasonCode, reject
from vs_evaluator_protocol.records import (
    PROTOCOL_VERSION,
    ErrorRecord,
    Hello,
    Result,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Set

    from pydantic import JsonValue

    from vs_evaluator_protocol.records import MetricSpec, PartialMeasurement, Record


@dataclass(frozen=True, slots=True)
class Measurement:
    """One validated evaluator outcome.

    Exactly one of `values` (the measured row, keyed by declared metric name)
    and `failure` (why the evaluator produced no row) is set.

    `metrics` is the declaration from `hello`, or `None` when the evaluator
    failed before it declared anything: an `error` is the one record the
    protocol lets arrive with no preceding `hello`, so a failed measurement
    need not carry a declaration. A measured row always does, since the row
    was validated against it.

    `partial` is what a failed run measured before it stopped, exactly as its
    `error` record reported it; it is never set on a measured row.
    """

    metrics: Mapping[str, MetricSpec] | None
    values: Mapping[str, float] | None = None
    failure: str | None = None
    partial: PartialMeasurement | None = None

    def __post_init__(self) -> None:
        """Reject an outcome that is neither exactly a row nor a failure."""
        if (self.values is None) == (self.failure is None):
            message = "a measurement carries either values or a failure message"
            raise ValueError(message)
        if self.values is not None and self.metrics is None:
            message = "a measured row carries the metric declaration it was validated against"
            raise ValueError(message)
        if self.partial is not None and self.failure is None:
            message = "only a failed measurement carries a partial measurement"
            raise ValueError(message)

    @property
    def failed(self) -> bool:
        """Whether the evaluator reported a failure instead of a row."""
        return self.failure is not None


def read_measurement(records: Iterable[Record]) -> Measurement:
    """Validate a record stream and return the measurement it reports.

    Consumes *records* in order and never looks ahead, so the same function
    serves a stream delivered incrementally by a future transport.

    Raises:
        ProtocolError: when the stream violates a reader obligation, with a
            message naming the offending record and key.
    """
    hello: Hello | None = None
    values: Mapping[str, float] | None = None

    for position, record in enumerate(records, start=1):
        if isinstance(record, Hello):
            hello = _read_hello(record, hello=hello, position=position)
        elif hello is None:
            if isinstance(record, ErrorRecord) and position == 1:
                # An error is the one record that may replace hello: an
                # evaluator whose metric identity comes from configuration
                # cannot declare anything until that configuration loads, and
                # a failure before then must still reach the framework as a
                # reason rather than as a missing file.
                return Measurement(metrics=None, failure=record.message, partial=record.partial)
            # A hello may still arrive; whether this is HELLO_NOT_FIRST or
            # MISSING_HELLO is only decided once the stream is exhausted.
            continue
        elif isinstance(record, ErrorRecord):
            return Measurement(
                metrics=hello.metrics, failure=record.message, partial=record.partial
            )
        else:
            values = _read_values(record, hello=hello, values=values, position=position)

    if hello is None:
        reject(ReasonCode.MISSING_HELLO, "stream has no hello record")
    if values is None:
        reject(ReasonCode.NO_OUTCOME, "stream has neither a result nor an error record")
    return Measurement(metrics=hello.metrics, values=values)


def check_objectives(hello: Hello, objective_names: Set[str]) -> None:
    """Check that the evaluator declares every metric the task optimizes for.

    An objective must be declared *and* declared required. An optional metric
    may be absent from a successful run, so a task cannot rank on one: the
    frontier would silently lose the round.

    Separate from `read_measurement` on purpose: objectives belong to the
    task, not to the evaluator, which never learns which metrics are
    optimized.

    Raises:
        ProtocolError: when an objective is absent from `hello.metrics` or is
            declared there as optional.
    """
    undeclared = sorted(name for name in objective_names if name not in hello.metrics)
    optional = sorted(
        name
        for name in objective_names
        if name in hello.metrics and not hello.metrics[name].required
    )
    if not undeclared and not optional:
        return
    faults: list[str] = []
    if undeclared:
        faults.append(f"objectives {_names(undeclared)} are not declared by the evaluator")
    if optional:
        faults.append(
            f"objectives {_names(optional)} are declared optional, so a successful run may "
            "omit them and the task cannot rank on them"
        )
    reject(
        ReasonCode.MISSING_METRIC,
        f"{'; '.join(faults)}; hello record declares {_names(hello.metrics)}",
    )


def _read_hello(record: Hello, *, hello: Hello | None, position: int) -> Hello:
    """Validate the opening record and the metric names it declares."""
    if hello is not None:
        reject(ReasonCode.DUPLICATE_HELLO, f"record {position} is a second hello record")
    if position != 1:
        reject(ReasonCode.HELLO_NOT_FIRST, f"record {position} is a hello but is not first")
    if record.protocol != PROTOCOL_VERSION:
        reject(
            ReasonCode.UNSUPPORTED_PROTOCOL,
            f"hello record has key 'protocol' = {record.protocol}, "
            f"but this reader implements version {PROTOCOL_VERSION}",
        )
    if not record.metrics:
        reject(ReasonCode.EMPTY_METRICS, "hello record has an empty key 'metrics'")
    for name in record.metrics:
        if not name or any(character.isspace() for character in name):
            reject(
                ReasonCode.INVALID_METRIC_NAME,
                f"hello record declares metric {name!r}: metric names must be non-empty "
                "and contain no whitespace",
            )
    return record


def _read_values(
    record: Result,
    *,
    hello: Hello,
    values: Mapping[str, float] | None,
    position: int,
) -> Mapping[str, float]:
    """Validate one measured row against the declared metrics.

    Every reported name must be declared, and every metric declared required
    must be reported. A declared optional metric may be absent.
    """
    if values is not None:
        reject(
            ReasonCode.INVALID_RECORD,
            f"record {position} is a second result record, "
            f"but protocol {PROTOCOL_VERSION} carries at most one operating point",
        )
    if record.label:
        reject(
            ReasonCode.UNSUPPORTED_LABEL,
            f"record {position} has key 'label' = {record.label!r}, "
            f"but protocol {PROTOCOL_VERSION} accepts only the default label",
        )
    unknown = sorted(name for name in record.values if name not in hello.metrics)
    if unknown:
        reject(
            ReasonCode.UNKNOWN_METRIC,
            f"record {position} has key 'values' with undeclared metrics "
            f"{_names(unknown)}; hello record declares {_names(hello.metrics)}",
        )
    missing = sorted(
        name for name, spec in hello.metrics.items() if spec.required and name not in record.values
    )
    if missing:
        reject(
            ReasonCode.MISSING_METRIC,
            f"record {position} has key 'values' missing required metrics {_names(missing)}",
        )
    return {
        name: _read_value(value, name=name, position=position)
        for name, value in record.values.items()
    }


def _read_value(value: JsonValue, *, name: str, position: int) -> float:
    """Validate one measured value as a finite JSON number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        reject(
            ReasonCode.NON_NUMERIC_VALUE,
            f"record {position} has key 'values.{name}' = {value!r}, which is not a number",
        )
    if not math.isfinite(value):
        reject(
            ReasonCode.NON_FINITE_VALUE,
            f"record {position} has key 'values.{name}' = {value!r}, which is not finite",
        )
    return float(value)


def _names(names: Iterable[str]) -> str:
    """Render metric names for an error message in a stable order."""
    return ", ".join(repr(name) for name in sorted(names))
