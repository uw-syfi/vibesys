"""Typed store contracts validate at the public boundary and retain wire data."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf import DescFieldValueEnum, DescFieldValueScalar, Message, ScalarType
from protobuf.wkt.google.protobuf.any_pb import Any as ProtoAny
from protobuf.wkt.google.protobuf.descriptor_pb import FileDescriptorSet
from scripts.generate_record_goldens import (
    attrs_examples,
    detail_examples,
    generate,
    record_examples,
)

import vs_proto.api as p

_RECORDS = record_examples()
_ATTRS = attrs_examples()
_DETAILS = detail_examples()
_ROOT = Path(__file__).resolve().parents[3]
_TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), min_size=1, max_size=32)


def walk(message: Message) -> Iterator[Message]:
    """Visit populated store messages through protobuf's public field interface."""
    if message.desc().file.proto.package not in {"vibesys.store.v1", "vibesys.search.v1"}:
        return
    yield message
    for field in message:
        value = message[field]
        if isinstance(value, Message):
            yield from walk(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Message):
                    yield from walk(item)


def message_examples() -> dict[str, Message]:
    examples = {
        message.desc().name: message
        for root in [*_RECORDS.values(), *_ATTRS.values(), *_DETAILS.values()]
        for message in walk(root)
    }
    examples["Failure"] = p.Failure(kind=p.SpanFailureKind.OUTCOME, message="Gate failed")
    return examples


_MESSAGES = message_examples()


def varied(template: Message, suffix: str, factor: int) -> Message:
    """Vary all present text and magnitudes while retaining cross-field relations."""
    result = type(template).from_binary(template.to_binary())
    for message in walk(result):
        for field in message:
            value = message[field]
            if isinstance(field.value, DescFieldValueScalar):
                if field.value.scalar == ScalarType.STRING:
                    # Hashes are constrained hex, and the reply is JSON text.
                    if field.name == "sha256" or field.name.endswith("_schema_hash"):
                        message[field] = f"{factor:064x}"
                    elif field.name == "attrs_type":
                        continue  # The declaration must still name its embedded descriptor.
                    elif field.name == "reply_json":
                        message[field] = '{"kind":"done","value":' + str(factor) + "}"
                    else:
                        message[field] = value + suffix
                elif field.value.scalar in {
                    ScalarType.DOUBLE,
                    ScalarType.UINT32,
                    ScalarType.UINT64,
                }:
                    message[field] = value * factor
    return result


@pytest.mark.parametrize("name", _RECORDS)
@given(suffix=_TEXT, factor=st.integers(min_value=1, max_value=1000))
def test_every_record_body_encodes_decodes_and_validates(
    name: str, suffix: str, factor: int
) -> None:
    record = varied(_RECORDS[name], suffix, factor)
    p.validate(record)
    wire = record.to_binary()
    decoded = p.Record.from_binary(wire)
    p.validate(decoded)
    assert decoded == record
    assert decoded.to_binary() == wire


@pytest.mark.parametrize("name", _ATTRS)
@given(suffix=_TEXT, factor=st.integers(min_value=1, max_value=1000))
def test_every_attrs_type_encodes_decodes_and_validates(
    name: str, suffix: str, factor: int
) -> None:
    attrs = varied(_ATTRS[name], suffix, factor)
    p.validate(attrs)
    decoded = type(attrs).from_binary(attrs.to_binary())
    p.validate(decoded)
    assert decoded == attrs


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


@pytest.mark.parametrize("name", _MESSAGES)
@given(payload=st.binary(max_size=128), number=st.integers(min_value=1000, max_value=2**20))
def test_unknown_fields_survive_decode_encode(name: str, payload: bytes, number: int) -> None:
    message = _MESSAGES[name]
    unknown = varint(number << 3 | 2) + varint(len(payload)) + payload
    wire = message.to_binary() + unknown
    decoded = type(message).from_binary(wire)
    p.validate(decoded)
    assert decoded.to_binary() == wire


@given(payload=st.binary(max_size=128))
def test_unknown_record_body_survives_for_newer_readers(payload: bytes) -> None:
    # An older reader can retain a future oneof branch without interpreting it.
    wire = b"\x08\x01" + varint(100 << 3 | 2) + varint(len(payload)) + payload
    assert p.Record.from_binary(wire).to_binary() == wire


def test_corpus_covers_every_body_and_builtin_attrs_message() -> None:
    assert {record.body.field for record in _RECORDS.values() if record.body is not None} == {
        field.name for field in p.Record.desc().oneofs[0].fields
    }
    exported = {
        getattr(p, name).desc().name
        for name in p.__all__
        if isinstance(getattr(p, name), type)
        and issubclass(getattr(p, name), Message)
        and getattr(p, name).desc().file.proto.package in {"vibesys.store.v1", "vibesys.search.v1"}
    }
    assert set(_MESSAGES) == exported
    assert {type(attrs).desc().name for attrs in _ATTRS.values()} == {
        name for name in exported if name.endswith("Attrs")
    }


@pytest.mark.parametrize("name", _RECORDS)
def test_record_goldens_are_byte_stable(name: str) -> None:
    wire = (_ROOT / "proto/testdata/records" / (name + ".pb")).read_bytes()
    assert len(wire) <= 1024 * 1024
    assert wire == _RECORDS[name].to_binary()
    p.validate(p.Record.from_binary(wire))


def test_golden_regenerator_reproduces_exact_corpus(tmp_path: Path) -> None:
    generate(tmp_path)
    expected = _ROOT / "proto/testdata/records"
    assert {path.name for path in tmp_path.iterdir()} == {path.name for path in expected.iterdir()}
    for path in tmp_path.iterdir():
        assert path.read_bytes() == (expected / path.name).read_bytes()


def set_field(message: Message, name: str, value: object) -> None:
    if value is None:
        message.clear_field(name)
    else:
        field = next(field for field in message.desc().fields if field.name == name)
        message[field] = value


def assert_invalid(message: Message, field: str) -> None:
    with pytest.raises(p.ValidationError) as failure:
        p.validate(message)
    violations = failure.value.to_proto().violations
    # CEL invariants identify their fields in the rule message; standard rules
    # report the actual field path. Neither relies on ValidationError's summary.
    assert any(
        field in violation.message
        or (
            violation.field is not None
            and field in [element.field_name for element in violation.field.elements]
        )
        for violation in violations
    ), violations


_ENUM_FIELDS = [
    (name, field.name)
    for name, message in _MESSAGES.items()
    for field in message.desc().fields
    if isinstance(field.value, DescFieldValueEnum)
]


@pytest.mark.parametrize(("name", "field"), _ENUM_FIELDS)
@given(invalid=st.one_of(st.just(0), st.integers(min_value=100, max_value=2**31 - 1)))
def test_every_enum_field_rejects_zero_and_unknown_values(
    name: str, field: str, invalid: int
) -> None:
    message = type(_MESSAGES[name]).from_binary(_MESSAGES[name].to_binary())
    set_field(message, field, invalid)
    assert_invalid(message, field)


def field_rules(message: Message, field_name: str) -> Message | None:
    descriptor = message.desc()
    validation = next(
        file for file in descriptor.file.dependencies if file.name == "buf/validate/validate.proto"
    )
    extension = next(ext for ext in validation.extensions if ext.name == "field").type
    field = next(field for field in descriptor.fields if field.name == field_name)
    return field.proto.options[extension] if field.proto.options is not None else None


def rule_value(message: Message | None, name: str) -> object:
    if message is None or not message.has_field(name):
        return None
    field = next(field for field in message.desc().fields if field.name == name)
    return message[field]


def typed_rules(message: Message, field: str, kind: str) -> Message | None:
    value = rule_value(field_rules(message, field), kind)
    return value if isinstance(value, Message) else None


def constrained_fields(rule: str) -> list[tuple[str, str]]:
    return [
        (name, field.name)
        for name, message in _MESSAGES.items()
        for field in message.desc().fields
        if typed_rules(message, field.name, rule) is not None
    ]


_STRING_FIELDS = constrained_fields("string")


@pytest.mark.parametrize(("name", "field"), _STRING_FIELDS)
@given(character=st.sampled_from(["a", "\N{SNOWMAN}"]))
def test_inline_text_over_64_kib_is_rejected(name: str, field: str, character: str) -> None:
    message = type(_MESSAGES[name]).from_binary(_MESSAGES[name].to_binary())
    rules = typed_rules(message, field, "string")
    assert rules is not None
    if not rules.has_field("max_bytes"):
        # The SHA-256 digest has an exact hex pattern rather than a content cap.
        return
    set_field(message, field, character * (65536 // len(character.encode()) + 1))
    assert_invalid(message, field)


_ID_FIELDS = [
    (name, field)
    for name, field in _STRING_FIELDS
    if rule_value(typed_rules(_MESSAGES[name], field, "string"), "min_len")
]


@pytest.mark.parametrize(("name", "field"), _ID_FIELDS)
def test_nonempty_identifiers_cannot_be_empty(name: str, field: str) -> None:
    message = type(_MESSAGES[name]).from_binary(_MESSAGES[name].to_binary())
    set_field(message, field, "")
    assert_invalid(message, field)


_NUMBER_FIELDS = constrained_fields("double")


@pytest.mark.parametrize(("name", "field"), _NUMBER_FIELDS)
@given(invalid=st.sampled_from([float("nan"), float("inf"), -float("inf")]))
def test_numbers_must_be_finite(name: str, field: str, invalid: float) -> None:
    message = type(_MESSAGES[name]).from_binary(_MESSAGES[name].to_binary())
    set_field(message, field, invalid)
    assert_invalid(message, field)


_NONNEGATIVE_FIELDS = [
    (name, field)
    for name, field in _NUMBER_FIELDS
    if rule_value(typed_rules(_MESSAGES[name], field, "double"), "gte") == 0.0
]


@pytest.mark.parametrize(("name", "field"), _NONNEGATIVE_FIELDS)
@given(invalid=st.floats(max_value=-0.000001, allow_infinity=False, allow_nan=False))
def test_nonnegative_measurements_reject_negative_values(
    name: str, field: str, invalid: float
) -> None:
    message = type(_MESSAGES[name]).from_binary(_MESSAGES[name].to_binary())
    set_field(message, field, invalid)
    assert_invalid(message, field)


_REQUIRED_FIELDS = [
    (name, field.name)
    for name, message in _MESSAGES.items()
    for field in message.desc().fields
    if rule_value(field_rules(message, field.name), "required")
]


@pytest.mark.parametrize(("name", "field"), _REQUIRED_FIELDS)
def test_required_messages_cannot_be_absent(name: str, field: str) -> None:
    message = type(_MESSAGES[name]).from_binary(_MESSAGES[name].to_binary())
    message.clear_field(field)
    assert_invalid(message, field)


@pytest.mark.parametrize("message", [p.Record(), p.Lifecycle(), p.Hold()])
def test_required_oneofs_cannot_be_absent(message: Message) -> None:
    assert_invalid(message, message.desc().oneofs[0].name)


def changed(message: Message, **updates: object) -> Message:
    result = type(message).from_binary(message.to_binary())
    for name, value in updates.items():
        set_field(result, name, value)
    return result


_CROSS_FIELD_FAILURES = [
    (lambda: changed(_MESSAGES["EpochStarted"], descriptors=FileDescriptorSet()), "span_kinds"),
    (lambda: changed(_MESSAGES["EpochStarted"], series=[]), "objectives"),
    (
        lambda: changed(
            _MESSAGES["EpochStarted"],
            series=[
                p.MetricSeriesDecl(
                    name="throughput", direction=p.Direction.MIN, role=p.SeriesRole.OBJECTIVE
                )
            ],
        ),
        "objectives",
    ),
    (lambda: changed(_MESSAGES["EpochStarted"], api_schema_hash="invalid"), "api_schema_hash"),
    (lambda: changed(_MESSAGES["EpochStarted"], store_schema_hash="invalid"), "store_schema_hash"),
    (lambda: changed(_MESSAGES["EpochStarted"], run_id=""), "run_id"),
    (lambda: changed(_MESSAGES["EpochStarted"], epoch=0), "epoch"),
    (lambda: changed(_MESSAGES["EpochStarted"], tree_hash=""), "tree_hash"),
    (lambda: changed(_MESSAGES["EpochStarted"], vibesys_version=""), "vibesys_version"),
    (lambda: changed(_MESSAGES["EpochStarted"], descriptors=None), "span_kinds"),
    (lambda: changed(_RECORDS["epoch_started"], epoch=2), "epoch"),
    (lambda: changed(_RECORDS["epoch_started"], seq=0), "seq"),
    (lambda: changed(_RECORDS["epoch_started"], at=None), "at"),
    (
        lambda: changed(_MESSAGES["EpochStarted"], span_kinds=[_MESSAGES["SpanKindDecl"]] * 2),
        "span_kinds",
    ),
    (
        lambda: changed(_MESSAGES["EpochStarted"], series=[_MESSAGES["MetricSeriesDecl"]] * 2),
        "series",
    ),
    (
        lambda: changed(_MESSAGES["RunDescription"], objectives=[_MESSAGES["Objective"]] * 2),
        "objectives",
    ),
    (lambda: changed(_RECORDS["metric"], seq=0), "seq"),
    (lambda: changed(_RECORDS["metric"], epoch=0), "epoch"),
    (lambda: changed(_RECORDS["metric"], at=None), "at"),
    (lambda: p.Progress(done=1, total=0), "total"),
    (lambda: p.Progress(done=3, total=2), "total"),
    (
        lambda: p.Starting(
            stage=p.StartupStage.BUILDING_IMAGE, state=p.StageState.WAITING, elapsed_seconds=1
        ),
        "condition",
    ),
    (
        lambda: p.Starting(
            stage=p.StartupStage.BUILDING_IMAGE,
            state=p.StageState.RUNNING,
            condition=p.WaitCondition.QUEUED,
        ),
        "condition",
    ),
    (
        lambda: p.Starting(stage=p.StartupStage.BUILDING_IMAGE, state=p.StageState.FINISHED),
        "elapsed_seconds",
    ),
    (
        lambda: p.Starting(
            stage=p.StartupStage.BUILDING_IMAGE, state=p.StageState.RUNNING, elapsed_seconds=1
        ),
        "elapsed_seconds",
    ),
    (
        lambda: p.Starting(
            stage=p.StartupStage.BUILDING_IMAGE,
            state=p.StageState.WAITING,
            condition=p.WaitCondition.QUEUED,
            elapsed_seconds=1,
            progress=p.Progress(done=1),
        ),
        "progress",
    ),
    (
        lambda: p.Ended(outcome=p.Outcome.FINISHED, interruption_reason="Signal"),
        "interruption_reason",
    ),
    (lambda: p.Ended(outcome=p.Outcome.FAILED), "failure"),
    (
        lambda: p.Ended(
            outcome=p.Outcome.FINISHED, failure=p.RunFailure(kind=p.RunFailureKind.NO_RESULT)
        ),
        "failure",
    ),
    (lambda: p.Ended(outcome=p.Outcome.INTERRUPTED, signal=0), "signal"),
    (lambda: p.Ended(outcome=p.Outcome.FINISHED, signal=15), "signal"),
    (lambda: changed(_MESSAGES["SpanRecord"], change=p.Change.STARTED), "ended_at"),
    (
        lambda: p.SpanRecord(
            change=p.Change.ENDED,
            span=p.Span(
                id="op:a",
                runtime_type=p.RuntimeType.OPERATION,
                name="a",
                status=p.SpanStatus.RUNNING,
            ),
        ),
        "ended_at",
    ),
    (lambda: changed(_MESSAGES["Span"], ended_at=0), "ended_at"),
    (lambda: changed(_MESSAGES["Span"], ended_at=None), "ended_at"),
    (lambda: changed(_MESSAGES["Span"], status=p.SpanStatus.RUNNING), "ended_at"),
    (lambda: p.Attempt(n=3, of=2), "n"),
    (lambda: p.Attempt(n=0, of=1), "n"),
    (lambda: p.Attempt(n=1, of=0), "of"),
    (lambda: p.Attempt(n=1, of=2, started_at=3, ended_at=2), "ended_at"),
    (
        lambda: p.StreamRef(
            id="a", kind=p.StreamKind.TRANSCRIPT, span="op:a", state=p.StreamState.ENDED, length=1
        ),
        "length",
    ),
    (lambda: p.StreamRef(id="a", kind=p.StreamKind.TRANSCRIPT, state=p.StreamState.OPENED), "span"),
    (
        lambda: p.StreamRef(id="a", kind=p.StreamKind.LOG, state=p.StreamState.OPENED, length=0),
        "length",
    ),
    (lambda: p.StreamRef(id="a", kind=p.StreamKind.LOG, state=p.StreamState.ENDED), "length"),
    (lambda: changed(_ATTRS["gate"], subject=p.GateSubject.CANDIDATE), "input_failure"),
    (lambda: p.FailedCheck(check=p.EvidenceKind.CORRECTNESS, completed=3, required=2), "completed"),
    (lambda: p.InputFailure(kind=p.InputFailureKind.NO_EVIDENCE, attempts=0), "attempts"),
    (lambda: p.AdoptionAttrs(selection=p.Selection.CANDIDATE), "candidate"),
    (lambda: p.Objective(series="x", direction=p.Direction.NONE), "direction"),
    (lambda: changed(_ATTRS["comparison"], direction=p.Direction.NONE), "direction"),
    (lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.QUEUED), "job_id"),
    (lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.QUEUED, job_id="a"), "waited_seconds"),
    (lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.PROVEN, job_id="a"), "job_id"),
    (lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.FAILED), "failure"),
    (lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.PROVEN, failure="a"), "failure"),
    (lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.RETRYING, failure="a"), "attempt"),
    (
        lambda: p.EnvironmentProbeAttrs(
            state=p.ProbeState.RETRYING, failure="a", attempt=1, retry_in_seconds=0
        ),
        "attempt",
    ),
    (
        lambda: p.EnvironmentProbeAttrs(state=p.ProbeState.PROVEN, retry_in_seconds=0),
        "retry_in_seconds",
    ),
    (
        lambda: changed(_ATTRS["environment_probe"], queue_timeout_seconds=0.5),
        "queue_timeout_seconds",
    ),
    (lambda: changed(_ATTRS["capacity_wait"], fallback_provider=None), "fallback_provider"),
    (lambda: changed(_ATTRS["capacity_wait"], fallback_model=None), "fallback_model"),
    (
        lambda: changed(_ATTRS["capacity_wait"], fallback_provider=None, fallback_model=None),
        "fallback_provider",
    ),
    (
        lambda: p.IntentState(
            control_id="a", action=p.IntentAction.STOP, step=p.IntentStep.REFUSED
        ),
        "reason",
    ),
    (
        lambda: p.IntentState(
            control_id="a", action=p.IntentAction.STOP, step=p.IntentStep.REFUSED, reason=""
        ),
        "reason",
    ),
    (lambda: p.SteerTarget(kind=p.SteerTargetKind.RUN, id="a"), "id"),
    (lambda: p.SteerTarget(kind=p.SteerTargetKind.ROLE), "id"),
]


@pytest.mark.parametrize(("make", "field"), _CROSS_FIELD_FAILURES)
def test_cross_field_invariants_reject_invalid_states(
    make: Callable[[], Message], field: str
) -> None:
    assert_invalid(make(), field)


@pytest.mark.parametrize("name", _MESSAGES)
def test_all_new_enums_are_closed_and_text_is_bounded(name: str) -> None:
    message = _MESSAGES[name]
    for field in message.desc().fields:
        rules = field_rules(message, field.name)
        if isinstance(field.value, DescFieldValueEnum):
            assert field.value.enum.values[0].name.endswith("_UNSPECIFIED")
            enum = typed_rules(message, field.name, "enum")
            assert rule_value(enum, "defined_only")
            disallowed = rule_value(enum, "not_in")
            assert isinstance(disallowed, list)
            assert 0 in disallowed
        if name in {"Record", "EpochStarted", "LegacyEvent"}:
            continue  # Phase 1 field validation remains compatible.
        if (
            isinstance(field.value, DescFieldValueScalar)
            and field.value.scalar == ScalarType.STRING
        ):
            assert rules is not None
            string = typed_rules(message, field.name, "string")
            assert rule_value(string, "max_bytes") == 65536 or field.name == "sha256"


@pytest.mark.parametrize(("name", "field"), constrained_fields("repeated"))
def test_identifier_lists_reject_empty_items(name: str, field: str) -> None:
    assert_invalid(changed(_MESSAGES[name], **{field: [""]}), field)


@given(
    digest=st.one_of(
        st.just("A" * 64),
        st.text(alphabet="ghxyz", min_size=1, max_size=64),
        st.text(alphabet="0123456789abcdef", max_size=63),
    )
)
def test_blob_hash_is_exactly_64_lowercase_hex_digits(digest: str) -> None:
    assert_invalid(p.BlobRef(sha256=digest, media_type="text/plain"), "sha256")


@given(size=st.sampled_from([0, 1, 65535, 65536]))
def test_inline_content_cap_is_bytes_and_includes_the_boundary(size: int) -> None:
    for text in ["a" * size, "\N{SNOWMAN}" * (size // 3) + "a" * (size % 3)]:
        assert len(text.encode()) == size
        p.validate(p.AgentTurnAttrs(agent="a", role="r", stage="s", reply_json=text))


@given(state=st.sampled_from(list(p.StreamState)[1:]))
def test_transcript_has_one_stream_and_final_length_only_on_turn(state: p.StreamState) -> None:
    p.validate(
        p.StreamRef(id="transcript-a", kind=p.StreamKind.TRANSCRIPT, span="op:turn", state=state)
    )
    attrs = p.AgentTurnAttrs(agent="a", role="r", stage="s", transcript_length=0)
    assert attrs.has_field("transcript_length")
    p.validate(attrs)


@given(value=st.integers(min_value=0, max_value=2**64 - 1))
def test_unreported_usage_stays_absent_and_reported_zero_is_present(value: int) -> None:
    absent = p.TokenUsage.from_binary(p.TokenUsage().to_binary())
    reported = p.TokenUsage.from_binary(p.TokenUsage(input=value).to_binary())
    assert not absent.has_field("input")
    assert reported.has_field("input")
    assert reported.input == value
    p.validate(reported)


@given(
    kind=st.sampled_from(
        [p.ProbeState.QUEUED, p.ProbeState.RETRYING, p.ProbeState.PROVEN, p.ProbeState.FAILED]
    )
)
def test_probe_states_have_only_their_required_details(kind: p.ProbeState) -> None:
    probes = {
        p.ProbeState.QUEUED: p.EnvironmentProbeAttrs(state=kind, job_id="a", waited_seconds=0),
        p.ProbeState.RETRYING: p.EnvironmentProbeAttrs(
            state=kind, failure="Queue ended", attempt=2, retry_in_seconds=0
        ),
        p.ProbeState.PROVEN: p.EnvironmentProbeAttrs(state=kind),
        p.ProbeState.FAILED: p.EnvironmentProbeAttrs(state=kind, failure="Root unavailable"),
    }
    p.validate(probes[kind])


@given(outcome=st.sampled_from(list(p.Outcome)[1:]), resumable=st.booleans())
def test_all_lifecycle_outcomes_validate(outcome: p.Outcome, *, resumable: bool) -> None:
    failure = p.RunFailure(
        kind=p.RunFailureKind.CONFIGURATION,
        reason="Invalid config",
        code="invalid_key",
        stage="parse",
        message="Unknown key",
        exit_code=2,
    )
    ended = p.Ended(
        outcome=outcome,
        resumable=resumable,
        failure=failure if outcome == p.Outcome.FAILED else None,
        signal=15 if outcome == p.Outcome.INTERRUPTED else None,
        interruption_reason="Signal received" if outcome == p.Outcome.INTERRUPTED else None,
    )
    p.validate(p.Lifecycle(state=p.Oneof("ended", ended)))


@given(state=st.sampled_from(list(p.StageState)[1:]), done=st.integers(min_value=0, max_value=100))
def test_startup_state_details_validate(state: p.StageState, done: int) -> None:
    starting = p.Starting(
        stage=p.StartupStage.BUILDING_IMAGE,
        state=state,
        condition=p.WaitCondition.QUEUED if state == p.StageState.WAITING else None,
        elapsed_seconds=None if state == p.StageState.RUNNING else done,
        progress=p.Progress(done=done, total=done + 1) if state == p.StageState.RUNNING else None,
    )
    p.validate(starting)


def test_updated_spans_can_replace_already_ended_comparisons() -> None:
    p.validate(changed(_MESSAGES["SpanRecord"], change=p.Change.UPDATED))


def test_unknown_span_kind_is_descriptor_driven() -> None:
    # Core labels stay extensible; there is intentionally no closed span-kind enum.
    record = _RECORDS["span"]
    record = p.Record.from_binary(record.to_binary())
    body = record.body
    assert body is not None
    assert isinstance(body.value, p.SpanRecord)
    span = body.value.span
    assert span is not None
    assert span.label is not None
    span.label.kind = "future_generation"
    span.label.attrs = ProtoAny(type_url="example.com/future.Generation", value=b"\x08\x01")
    p.validate(record)
    assert p.Record.from_binary(record.to_binary()) == record


def test_store_schema_cannot_import_search_strategy_contracts() -> None:
    pending = [message.desc().file for message in _MESSAGES.values()]
    seen: set[str] = set()
    while pending:
        file = pending.pop()
        if file.name in seen:
            continue
        seen.add(file.name)
        if file.proto.package == "vibesys.store.v1":
            assert not any(
                dependency.name.startswith("vibesys/search/") for dependency in file.dependencies
            ), f"{file.name}: store must carry search types through descriptor-backed Any"
        pending.extend(file.dependencies)
    store_files = {
        str(path.relative_to(_ROOT / "proto"))
        for path in (_ROOT / "proto/vibesys/store/v1").glob("*.proto")
    }
    assert store_files <= seen, "Export store contracts so every proto dependency is checked"


@pytest.mark.parametrize("name", _DETAILS)
@given(suffix=_TEXT, factor=st.integers(min_value=1, max_value=1000))
def test_search_detail_round_trips_without_a_store_dependency(
    name: str, suffix: str, factor: int
) -> None:
    detail = varied(_DETAILS[name], suffix, factor)
    p.validate(detail)
    packed = ProtoAny(
        type_url="type.googleapis.com/" + detail.desc().type_name, value=detail.to_binary()
    )
    decoded = type(detail).from_binary(ProtoAny.from_binary(packed.to_binary()).value)
    p.validate(decoded)
    assert decoded == detail


def test_epoch_describes_every_search_detail_type() -> None:
    body = _RECORDS["epoch_started"].body
    assert body is not None
    assert isinstance(body.value, p.EpochStarted)
    descriptors = body.value.descriptors
    assert descriptors is not None
    described = {
        f"{file.package}.{message.name}"
        for file in descriptors.file
        for message in file.message_type
    }
    assert {detail.desc().type_name for detail in _DETAILS.values()} <= described


def test_core_run_contracts_contain_no_search_fields() -> None:
    assert {field.name for field in p.RunFailure.desc().fields} == {
        "kind",
        "reason",
        "code",
        "stage",
        "message",
        "exit_code",
        "detail",
    }
    assert "max_workstreams" not in {field.name for field in p.RunDescription.desc().fields}
    assert not {"reason", "uncompared"} & {field.name for field in p.Ended.desc().fields}


@pytest.mark.parametrize("descriptors", [None, FileDescriptorSet()])
def test_search_run_details_require_epoch_descriptors(
    descriptors: FileDescriptorSet | None,
) -> None:
    header = changed(_MESSAGES["EpochStarted"], span_kinds=[], descriptors=descriptors)
    assert_invalid(header, "descriptors")
