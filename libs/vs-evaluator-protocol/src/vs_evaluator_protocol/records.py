"""Record definitions and line parsing for the evaluator result protocol."""

from __future__ import annotations

import json
from typing import Annotated, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from vs_evaluator_protocol.errors import ReasonCode, reject

PROTOCOL_VERSION: int = 2


class _StrictRecord(BaseModel):
    """Base for records that reject unknown keys and type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


class MetricSpec(_StrictRecord):
    """Declaration of one metric the evaluator produces.

    `unit` and `direction` are advisory and select nothing: `unit` is
    human-facing and `direction` states the metric's intrinsic
    better-direction. `required` is not advisory: it says whether every
    successful run must report the metric. An optional metric is still
    declared, so it can never be reported under a name the reader has not
    seen, but it may be absent from a result row.
    """

    unit: str | None = None
    direction: Literal["max", "min"] | None = None
    required: bool = True


class Hello(_StrictRecord):
    """Opening record declaring the protocol version and produced metrics."""

    kind: Literal["hello"] = "hello"
    protocol: int
    metrics: dict[str, MetricSpec]


class Result(_StrictRecord):
    """Measured row for one operating point.

    `values` is intentionally untyped beyond JSON: rejecting non-numbers,
    booleans, and non-finite numbers is the reader's job and carries its own
    reason codes.
    """

    kind: Literal["result"] = "result"
    label: str = ""
    values: dict[str, JsonValue]


# A finite JSON number. Integers are numbers too; booleans are not.
_FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
_Name = Annotated[str, Field(pattern=r"^\S+$")]


class Progress(_StrictRecord):
    """Work a stopped run completed, out of the work a passing run completes.

    `unit` names one unit of work (for example `rounds` or `requests`).
    """

    completed: int = Field(ge=0)
    required: int = Field(gt=0)
    unit: _Name


class PartialMeasurement(_StrictRecord):
    """What a run that failed or was stopped early measured before it stopped.

    It is reported only on an `error` record, so it never counts as a result:
    it lets a reader compare failed runs by how close each came. `name` is the
    measured quantity; it need not be a metric declared in `hello`, because a
    run cut short usually measures a different quantity (for example the rate
    of a warmup phase) than the one its result row would hold. `value` is what
    was measured, `target` the value a passing run needs (absent when the
    evaluator has no single bar), and `direction` which way is better, so two
    partial measurements of the same quantity can be ranked.
    """

    name: _Name
    value: _FiniteNumber
    direction: Literal["max", "min"]
    unit: str | None = Field(default=None, min_length=1)
    target: _FiniteNumber | None = None
    progress: Progress | None = None


class ErrorRecord(_StrictRecord):
    """Terminating record reporting that the evaluator produced no row.

    `partial` is what the run measured before it failed, when it measured
    anything; absent means nothing was measured.
    """

    kind: Literal["error"] = "error"
    message: str = Field(min_length=1)
    partial: PartialMeasurement | None = None


Record = Hello | Result | ErrorRecord

_RECORD_TYPES: dict[str, type[Record]] = {
    "hello": Hello,
    "result": Result,
    "error": ErrorRecord,
}


def parse_records(text: str) -> list[Record]:
    """Parse a record stream into typed records, one per non-blank line.

    Validates each line on its own: JSON shape, record kind, key set, and
    field types. Cross-record obligations belong to `read_measurement`.
    Lines end at a line feed only: a JSON string may hold other Unicode line
    separators (U+0085, U+2028), which `str.splitlines` would split on.

    Raises:
        ProtocolError: when a line is not a record of a known kind, carries an
            unknown key, or has a field that violates the record definition.
    """
    return [
        _parse_line(line, number)
        for number, line in enumerate(text.split("\n"), start=1)
        if line.strip()
    ]


def _parse_line(line: str, number: int) -> Record:
    """Parse one non-blank line into the record its `kind` names."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        reject(ReasonCode.MALFORMED_LINE, f"line {number} is not valid JSON")
    if not isinstance(payload, dict):
        reject(ReasonCode.MALFORMED_LINE, f"line {number} is not a JSON object")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _RECORD_TYPES:
        reject(ReasonCode.UNKNOWN_KIND, f"line {number} has unknown record kind {kind!r}")
    try:
        return _RECORD_TYPES[kind].model_validate_json(line)
    except ValidationError as error:
        _reject_invalid_record(error, line=number, kind=kind)


def _reject_invalid_record(error: ValidationError, *, line: int, kind: str) -> NoReturn:
    """Translate a pydantic failure into the reason code the protocol names."""
    details = error.errors()
    unknown_key = next((detail for detail in details if detail["type"] == "extra_forbidden"), None)
    detail = unknown_key or details[0]
    key = ".".join(str(part) for part in detail["loc"])
    if unknown_key is not None:
        reject(ReasonCode.UNKNOWN_KEY, f"line {line}: {kind} record has unknown key {key!r}")
    reject(
        ReasonCode.INVALID_RECORD,
        f"line {line}: {kind} record has invalid key {key!r}: {detail['msg']}",
    )
