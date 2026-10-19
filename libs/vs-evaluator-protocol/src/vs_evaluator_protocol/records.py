"""Record definitions and line parsing for the evaluator result protocol."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from vs_decode.api import Unreadable, UnreadableKind, decode_json
from vs_evaluator_protocol.errors import ReasonCode, reject

# The target for a record line before its `kind` picks the record model.
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
MAX_RECORD_LINE_BYTES = 4 * 1024 * 1024
"""The largest record line an evaluator may write."""

PROTOCOL_VERSION: int = 2


class ProfileField(StrEnum):
    """Measurement fields a profile must provide, never inferred from aggregate cost."""

    HIP_API_TIMING = "hip_api_timing"
    PREFILL_TIMING = "prefill_timing"
    DECODE_TIMING = "decode_timing"


class ErrorKind(StrEnum):
    """Why an evaluator produced no row: the one authoritative closed set of error kinds.

    `TRANSIENT` is the protocol's backward-compatible default: an `error` record
    that names no kind (every record written before kinds existed) says nothing
    about whether measuring again could help, so a reader treats it as it always
    did. `REQUIREMENT_NOT_MET` says the measured revision lacks a capability the
    task requires, so the failure is deterministic and measuring it again cannot
    help; the record names the requirement in `requirement`.
    """

    TRANSIENT = "transient"
    REQUIREMENT_NOT_MET = "requirement_not_met"


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
    anything; absent means nothing was measured. `error_kind` says whether
    measuring again could help (see `ErrorKind`; absent means `TRANSIENT`), and
    `requirement` names the capability a `REQUIREMENT_NOT_MET` failure lacks:
    present exactly for that kind.
    """

    kind: Literal["error"] = "error"
    message: str = Field(min_length=1)
    partial: PartialMeasurement | None = None
    error_kind: ErrorKind = ErrorKind.TRANSIENT
    requirement: _Name | None = None

    @model_validator(mode="after")
    def _requirement_exactly_when_not_met(self) -> ErrorRecord:
        if (self.error_kind is ErrorKind.REQUIREMENT_NOT_MET) != (self.requirement is not None):
            message = "key 'requirement' is required by, and only allowed with, key 'error_kind' = 'requirement_not_met'"
            raise ValueError(message)
        return self


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
    """Parse one non-blank line into the record its `kind` names.

    The line comes from the evaluator, so ``vs_decode`` parses it: bounded in size,
    with nesting bounded by pydantic's parser, where ``json.loads`` recursed per level
    and raised ``RecursionError`` past every caller's handler.
    """
    payload = decode_json(line, _JSON, source=f"line {number}", max_bytes=MAX_RECORD_LINE_BYTES)
    if isinstance(payload, Unreadable):
        if payload.kind is UnreadableKind.TOO_LARGE:
            reject(
                ReasonCode.MALFORMED_LINE,
                f"line {number} is larger than {MAX_RECORD_LINE_BYTES} bytes",
            )
        reject(ReasonCode.MALFORMED_LINE, f"line {number} is not valid JSON")
    if not isinstance(payload, dict):
        reject(ReasonCode.MALFORMED_LINE, f"line {number} is not a JSON object")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _RECORD_TYPES:
        reject(ReasonCode.UNKNOWN_KIND, f"line {number} has unknown record kind {kind!r}")
    record = decode_json(
        line, _RECORD_TYPES[kind], source=f"line {number}", max_bytes=MAX_RECORD_LINE_BYTES
    )
    if isinstance(record, Unreadable):
        _reject_invalid_record(record, line=number, kind=kind)
    return record


def _reject_invalid_record(record: Unreadable, *, line: int, kind: str) -> NoReturn:
    """Translate a validation failure into the reason code the protocol names."""
    problems = record.problems
    unknown_key = next((p for p in problems if p.error_type == "extra_forbidden"), None)
    problem = unknown_key or problems[0]
    if unknown_key is not None:
        reject(
            ReasonCode.UNKNOWN_KEY,
            f"line {line}: {kind} record has unknown key {problem.key_path!r}",
        )
    reject(
        ReasonCode.INVALID_RECORD,
        f"line {line}: {kind} record has invalid key {problem.key_path!r}: {problem.message}",
    )
