"""The decode function's contract: a typed value or a typed ``Unreadable``, never a raise."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vs_decode.api import Unreadable, UnreadableKind, decode_json, read_json
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
