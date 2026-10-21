"""Framing properties and shared Go/Python fixtures through the public API."""

from __future__ import annotations

import io
import json
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_proto.api import LegacyEvent, Oneof, Record
from vs_runlog.api import (
    MAGIC,
    MAX_RECORD_BYTES,
    FileRunLogWriter,
    RunLogReader,
    StopReason,
    encode_frame,
)

_TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=256)


def records(texts: list[str]) -> list[Record]:
    """Build dense records from arbitrary legacy JSON strings."""
    return [
        Record(seq=i, epoch=1, body=Oneof("legacy_event", LegacyEvent(json=text)))
        for i, text in enumerate(texts, 1)
    ]


@given(st.lists(_TEXT, max_size=16))
def test_round_trip(texts: list[str]) -> None:
    expected = records(texts)
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        writer = FileRunLogWriter(path)
        for record in expected:
            writer.append(record)
        with path.open("rb") as stream:
            reader = RunLogReader(stream)
            assert list(reader) == expected
            assert reader.stop.reason is StopReason.END
        assert FileRunLogWriter(path).sequence == len(expected)


@given(st.lists(_TEXT, max_size=6), st.data())
def test_any_truncation_is_clean_or_torn(texts: list[str], data: st.DataObject) -> None:
    encoded = MAGIC + b"".join(map(encode_frame, records(texts)))
    offset = data.draw(st.integers(min_value=0, max_value=len(encoded)))
    reader = RunLogReader(io.BytesIO(encoded[:offset]))
    list(reader)
    assert reader.stop.reason in (StopReason.END, StopReason.TORN_TAIL)


@given(_TEXT, st.data())
def test_each_payload_byte_flip_is_detected(text: str, data: st.DataObject) -> None:
    record = records([text])[0]
    encoded = bytearray(MAGIC + encode_frame(record))
    payload_start = len(encoded) - len(record.to_binary())
    index = data.draw(st.integers(min_value=payload_start, max_value=len(encoded) - 1))
    encoded[index] ^= data.draw(st.integers(min_value=1, max_value=255))
    reader = RunLogReader(io.BytesIO(encoded))
    assert list(reader) == []
    assert reader.stop.reason is StopReason.CORRUPTION
    assert reader.stop.offset == len(MAGIC)


def test_shared_fixtures_regenerate_identically() -> None:
    root = Path(__file__).resolve().parents[3] / "proto/testdata/runlog"
    generated = runpy.run_path(str(root / "generate.py"))["fixtures"]()
    for name, expected in generated.items():
        assert (root / name).read_bytes() == expected
    for name, expected in json.loads(generated["manifest.json"]).items():
        reader = RunLogReader(io.BytesIO(generated[name]))
        actual = list(reader)
        assert [record.seq for record in actual] == [
            record["seq"] for record in expected["records"]
        ]
        assert reader.stop.reason == expected["stop"]
        assert reader.stop.offset == expected["offset"]


def test_cap_and_sequence_errors_leave_log_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    writer = FileRunLogWriter(path)
    with pytest.raises(ValueError, match="seq"):
        writer.append(records(["a", "b"])[1])
    with pytest.raises(ValueError, match="exceeds"):
        writer.append(records(["x" * MAX_RECORD_BYTES])[0])
    assert path.read_bytes() == MAGIC


def test_resume_offset_and_recover_torn_tail(tmp_path: Path) -> None:
    first, second = records(["one", "two"])
    boundary = len(MAGIC + encode_frame(first))
    path = tmp_path / "run.log"
    path.write_bytes(MAGIC + encode_frame(first) + encode_frame(second)[:-1])
    writer = FileRunLogWriter(path)
    assert path.stat().st_size == boundary
    writer.append(second)
    with path.open("rb") as stream:
        assert list(RunLogReader(stream, boundary)) == [second]


@pytest.mark.parametrize("contents", [b"BADMAGIC", MAGIC + b"\x81\x80\x40", MAGIC + b"\x80" * 10])
def test_corruption_is_never_repaired(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "run.log"
    path.write_bytes(contents)
    with pytest.raises(ValueError, match="corrupt"):
        FileRunLogWriter(path)
    assert path.read_bytes() == contents
