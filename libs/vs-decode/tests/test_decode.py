"""The decode function's contract: a typed value or a typed ``Unreadable``, never a raise."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vs_decode.api import (
    Problem,
    Unreadable,
    UnreadableKind,
    ValidationOptions,
    decode_json,
    extract_unique_object_strings,
    read_json,
)
from vs_sim.api.testing import examples, hostile_json_frames

if TYPE_CHECKING:
    from pathlib import Path

_CAP = 2 * 1024 * 1024


class _Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    inner: _Inner


class _Lenient(BaseModel):
    name: str


_VALID = json.dumps({"name": "n", "inner": {"count": 1}}).encode()


_FRAMES = hostile_json_frames((_VALID,))


# The case name is the test id: a frame is up to megabytes, too large to print as one.
@pytest.mark.parametrize(("case", "frame"), _FRAMES, ids=[case for case, _ in _FRAMES])
def test_every_hostile_frame_is_a_value_or_unreadable(case: str, frame: bytes) -> None:
    decoded = decode_json(frame, _Doc, source=case, max_bytes=_CAP)
    if not isinstance(decoded, _Doc):
        assert decoded.source == case
        assert decoded.problems


@settings(max_examples=examples(pr=50, nightly=500))
@given(data=st.binary(max_size=256) | st.text(max_size=256).map(str.encode))
def test_arbitrary_bytes_never_raise(data: bytes) -> None:
    decoded = decode_json(data, _Doc, source="peer", max_bytes=_CAP)
    assert isinstance(decoded, _Doc | Unreadable)


@settings(max_examples=examples(pr=20, nightly=200))
@given(name=st.text(max_size=40), count=st.integers())
def test_a_valid_document_round_trips(name: str, count: int) -> None:
    document = _Doc(name=name, inner=_Inner(count=count))
    data = document.model_dump_json()
    assert decode_json(data, _Doc, source="s", max_bytes=_CAP) == document
    assert decode_json(data.encode(), _Doc, source="s", max_bytes=_CAP) == document


@settings(max_examples=examples(pr=20, nightly=200))
@given(key=st.text(min_size=1, max_size=20).filter(lambda k: k != "count"))
def test_an_unknown_key_is_invalid_and_named(key: str) -> None:
    data = json.dumps({"name": "n", "inner": {"count": 1, key: 0}})
    decoded = decode_json(data, _Doc, source="s", max_bytes=_CAP)
    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.INVALID
    assert decoded.key_path == f"inner.{key}"


@pytest.mark.parametrize("input_kind", ["text", "bytes"])
def test_a_duplicate_root_key_is_invalid_and_does_not_echo_its_values(input_kind: str) -> None:
    text = '{"name":"do-not-echo-first","name":"do-not-echo-second","inner":{"count":1}}'
    frame = text.encode() if input_kind == "bytes" else text

    decoded = decode_json(frame, _Doc, source="peer", max_bytes=_CAP)

    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.INVALID
    assert decoded.problems == (
        Problem(("name",), "Object member appears more than once", "duplicate_key"),
    )
    assert "do-not-echo" not in str(decoded)


@settings(max_examples=examples(pr=30, nightly=300))
@given(key=st.text(max_size=40), as_bytes=st.booleans())
def test_a_duplicate_key_at_any_nested_array_object_path_is_invalid(
    key: str, *, as_bytes: bool
) -> None:
    encoded_key = json.dumps(key)
    text = f'{{"items":[{{{encoded_key}:1,{encoded_key}:2}}]}}'
    frame = text.encode() if as_bytes else text

    decoded = decode_json(
        frame,
        TypeAdapter(dict[str, list[dict[str, int]]]),
        source="peer",
        max_bytes=_CAP,
    )

    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.INVALID
    assert decoded.problems[0].location == ("items", 0, key)
    assert decoded.problems[0].error_type == "duplicate_key"


def test_escaped_and_literal_spellings_of_the_same_key_are_duplicates() -> None:
    decoded = decode_json(
        '{"name":"n","inner":{"co\\u0075nt":1,"count":2}}',
        _Doc,
        source="peer",
        max_bytes=_CAP,
    )

    assert isinstance(decoded, Unreadable)
    assert decoded.problems[0].location == ("inner", "count")
    assert decoded.problems[0].error_type == "duplicate_key"


def test_escaped_surrogate_pair_and_literal_key_are_duplicates() -> None:
    decoded = decode_json(
        '{"𝄞":1,"\\ud834\\udd1e":2}',
        TypeAdapter(dict[str, int]),
        source="peer",
        max_bytes=_CAP,
    )

    assert isinstance(decoded, Unreadable)
    assert decoded.problems[0].location == ("𝄞",)
    assert decoded.problems[0].error_type == "duplicate_key"


def test_unescaped_key_fast_path_finds_a_large_duplicate() -> None:
    key = "key" * 20_000
    frame = f"{{{json.dumps(key)}:1,{json.dumps(key)}:2}}"

    decoded = decode_json(frame, TypeAdapter(dict[str, int]), source="peer", max_bytes=_CAP)

    assert isinstance(decoded, Unreadable)
    assert decoded.problems[0].location == (key,)


def test_escaped_key_chunks_decode_to_the_same_member_name() -> None:
    frame = '{"prefix\\nmid\\u0073uffix":1,"prefix\\nmidsuffix":2}'

    decoded = decode_json(frame, TypeAdapter(dict[str, int]), source="peer", max_bytes=_CAP)

    assert isinstance(decoded, Unreadable)
    assert decoded.problems[0].location == ("prefix\nmidsuffix",)


def test_large_non_key_string_is_skipped_before_a_later_duplicate() -> None:
    payload = "value" * 200_000
    frame = f'{{"payload":{json.dumps(payload)},"count":1,"count":2}}'

    decoded = decode_json(frame, TypeAdapter(dict[str, str | int]), source="peer", max_bytes=_CAP)

    assert isinstance(decoded, Unreadable)
    assert decoded.problems[0].location == ("count",)


@pytest.mark.parametrize(
    "frame",
    [
        b'{"key":"\\ud800"}',
        b'{"key":"\\udfff"}',
        b'{"\\ud800":"value"}',
        b'{"\\udfff":"value"}',
    ],
)
def test_lone_surrogate_escapes_are_not_json(frame: bytes) -> None:
    decoded = decode_json(frame, TypeAdapter(object), source="peer", max_bytes=_CAP)

    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.NOT_JSON


@pytest.mark.parametrize(("opening", "closing"), [("[", "]"), ('{"k":', "}")])
@pytest.mark.parametrize(("depth", "valid"), [(200, True), (201, False)])
def test_duplicate_scan_obeys_the_json_parser_nesting_bound(
    opening: str, closing: str, depth: int, *, valid: bool
) -> None:
    frame = opening * depth + "0" + closing * depth

    decoded = decode_json(frame, TypeAdapter(object), source="peer", max_bytes=_CAP)

    if valid:
        assert not isinstance(decoded, Unreadable)
    else:
        assert isinstance(decoded, Unreadable)
        assert decoded.kind is UnreadableKind.NOT_JSON


@pytest.mark.parametrize("input_kind", ["text", "bytes"])
def test_unique_root_strings_can_correlate_a_document_rejected_for_another_duplicate(
    input_kind: str,
) -> None:
    text = (
        '{"type":"query.history","type":"command.stop",'
        '"request_id":"request-7","client_id":"client-2"}'
    )
    frame = text.encode() if input_kind == "bytes" else text

    assert extract_unique_object_strings(
        frame,
        frozenset({"request_id", "client_id"}),
        max_bytes=_CAP,
        max_value_characters=128,
    ) == {"request_id": "request-7", "client_id": "client-2"}


def test_repeated_requested_root_string_is_not_extracted() -> None:
    frame = '{"request_id":"first","request_id":"second","client_id":"client-2"}'

    assert extract_unique_object_strings(
        frame,
        frozenset({"request_id", "client_id"}),
        max_bytes=_CAP,
        max_value_characters=128,
    ) == {"client_id": "client-2"}


@pytest.mark.parametrize(
    "encoded_value",
    [json.dumps("x" * 129), '"' + "\\u0078" * 129 + '"'],
    ids=["unescaped", "escaped"],
)
def test_extracted_object_strings_are_bounded_after_decoding(encoded_value: str) -> None:
    frame = f'{{"request_id":{encoded_value},"client_id":"client-2"}}'

    assert extract_unique_object_strings(
        frame,
        frozenset({"request_id", "client_id"}),
        max_bytes=_CAP,
        max_value_characters=128,
    ) == {"client_id": "client-2"}


def test_extracted_escaped_object_string_at_the_bound_is_preserved() -> None:
    frame = '{"request_id":"' + "\\u0078" * 128 + '"}'

    assert extract_unique_object_strings(
        frame,
        frozenset({"request_id"}),
        max_bytes=_CAP,
        max_value_characters=128,
    ) == {"request_id": "x" * 128}


@pytest.mark.parametrize("code_point", [0xD800, 0xDFFF], ids=["high", "low"])
@pytest.mark.parametrize("placement", ["requested-value", "object-key"])
def test_raw_surrogates_make_object_string_extraction_fail_closed(
    code_point: int, placement: str
) -> None:
    surrogate = chr(code_point)
    if placement == "requested-value":
        frame = '{"request_id":"' + surrogate + '","client_id":"client-2"}'
    else:
        frame = '{"request_id":"request-7","' + surrogate + '":"value"}'

    assert (
        extract_unique_object_strings(
            frame,
            frozenset({"request_id", "client_id"}),
            max_bytes=_CAP,
            max_value_characters=128,
        )
        == {}
    )


def test_deep_nesting_is_not_json_rather_than_a_recursion_error() -> None:
    decoded = decode_json(b"[" * 100_000, TypeAdapter(object), source="s", max_bytes=_CAP)
    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.NOT_JSON


@settings(max_examples=examples(pr=20, nightly=200))
@given(cap=st.integers(min_value=0, max_value=len(_VALID) - 1))
def test_input_past_the_cap_is_too_large(cap: int) -> None:
    decoded = decode_json(_VALID, _Doc, source="s", max_bytes=cap)
    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.TOO_LARGE
    assert decode_json(_VALID, _Doc, source="s", max_bytes=len(_VALID)) == _Doc(
        name="n", inner=_Inner(count=1)
    )


class _HoldsLenient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_Lenient]


_LENIENT_TARGETS = [_Lenient, _HoldsLenient, TypeAdapter(dict[str, _Lenient])]


@settings(max_examples=examples(pr=20, nightly=200))
@given(
    target=st.sampled_from(_LENIENT_TARGETS),
    data=st.binary(max_size=64),
    max_bytes=st.integers(min_value=0, max_value=64),
)
def test_a_target_that_drops_unknown_keys_at_any_depth_is_refused_whatever_the_input(
    target: type[BaseModel] | TypeAdapter[object], data: bytes, max_bytes: int
) -> None:
    with pytest.raises(TypeError, match="_Lenient must set extra='forbid'"):
        decode_json(data, target, source="s", max_bytes=max_bytes)


@pytest.mark.parametrize("target", _LENIENT_TARGETS)
def test_read_json_refuses_a_lenient_target_before_reading(
    tmp_path: Path, target: type[BaseModel] | TypeAdapter[object]
) -> None:
    with pytest.raises(TypeError, match="_Lenient must set extra='forbid'"):
        read_json(tmp_path / "absent.json", target, max_bytes=_CAP)


class _OpaqueSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    resolved: BaseModel | None = Field(default=None, exclude=True)


def test_an_opaque_model_slot_resolved_by_its_owner_is_not_a_lenient_shape() -> None:
    assert decode_json(b'{"name": "n"}', _OpaqueSlot, source="s", max_bytes=_CAP) == _OpaqueSlot(
        name="n"
    )


def test_a_type_adapter_decodes_values_that_are_not_models() -> None:
    adapter = TypeAdapter(list[int])
    assert decode_json(b"[1, 2]", adapter, source="s", max_bytes=_CAP) == [1, 2]
    decoded = decode_json(b'[1, "x"]', adapter, source="s", max_bytes=_CAP)
    assert isinstance(decoded, Unreadable)
    assert decoded.key_path == "1"


def test_unreadable_never_echoes_the_input() -> None:
    marker = "do-not-echo-this-value"
    decoded = decode_json(
        json.dumps({"name": 1, "inner": {"count": marker}}), _Doc, source="s", max_bytes=_CAP
    )
    assert isinstance(decoded, Unreadable)
    assert marker not in str(decoded)


def test_read_json_reports_missing_unreadable_and_oversized_files(tmp_path: Path) -> None:
    missing = read_json(tmp_path / "absent.json", _Doc, max_bytes=_CAP)
    assert isinstance(missing, Unreadable)
    assert missing.kind is UnreadableKind.MISSING
    assert missing.source == str(tmp_path / "absent.json")

    directory = read_json(tmp_path, _Doc, max_bytes=_CAP)
    assert isinstance(directory, Unreadable)
    assert directory.kind is UnreadableKind.NOT_READABLE

    document = tmp_path / "doc.json"
    document.write_bytes(_VALID)
    assert read_json(document, _Doc, max_bytes=len(_VALID)) == _Doc(name="n", inner=_Inner(count=1))
    oversized = read_json(document, _Doc, max_bytes=len(_VALID) - 1)
    assert isinstance(oversized, Unreadable)
    assert oversized.kind is UnreadableKind.TOO_LARGE
    assert read_json(document, TypeAdapter(dict[str, object]), max_bytes=_CAP) == json.loads(_VALID)


def test_read_json_refuses_what_is_not_a_regular_file_without_blocking(tmp_path: Path) -> None:
    # Opening a FIFO for reading blocks until a writer appears; the reader must not wait.
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    decoded = read_json(fifo, _Doc, max_bytes=_CAP)
    assert isinstance(decoded, Unreadable)
    assert decoded.kind is UnreadableKind.NOT_READABLE
    assert decoded.source == str(fifo)


def test_read_json_without_following_symlinks_refuses_a_symlinked_file(tmp_path: Path) -> None:
    document = tmp_path / "doc.json"
    document.write_bytes(_VALID)
    link = tmp_path / "link.json"
    link.symlink_to(document)
    assert read_json(link, _Doc, max_bytes=_CAP) == _Doc(name="n", inner=_Inner(count=1))
    refused = read_json(link, _Doc, max_bytes=_CAP, follow_symlinks=False)
    assert isinstance(refused, Unreadable)
    assert refused.kind is UnreadableKind.NOT_READABLE


def test_a_problem_names_its_location_and_error_type() -> None:
    decoded = decode_json(
        json.dumps({"name": "n", "inner": {"count": 1, "extra": 2}, "more": [1]}),
        _Doc,
        source="s",
        max_bytes=_CAP,
    )
    assert isinstance(decoded, Unreadable)
    found = {(problem.location, problem.error_type) for problem in decoded.problems}
    assert found == {(("inner", "extra"), "extra_forbidden"), (("more",), "extra_forbidden")}
    listed = decode_json(
        json.dumps([{"count": "x"}]), TypeAdapter(list[_Inner]), source="s", max_bytes=_CAP
    )
    assert isinstance(listed, Unreadable)
    assert listed.problems[0].location == (0, "count")
    assert listed.key_path == "0.count"
    too_large = decode_json(b"{}", _Doc, source="s", max_bytes=1)
    assert isinstance(too_large, Unreadable)
    assert too_large.problems[0].error_type is None


class _StrictCount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: int


@given(st.integers())
@settings(max_examples=25)
def test_a_strict_model_stays_strict_unless_the_caller_relaxes_it(count: int) -> None:
    coerced = json.dumps({"count": str(count)})
    refused = decode_json(coerced, _StrictCount, source="doc", max_bytes=1024)
    relaxed = decode_json(
        coerced, _StrictCount, source="doc", max_bytes=1024, options=ValidationOptions(strict=False)
    )

    assert isinstance(refused, Unreadable)
    assert refused.kind is UnreadableKind.INVALID
    assert relaxed == _StrictCount(count=count)
