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

from vs_proto.api import (
    RUN_LOG_MAGIC,
    RUN_LOG_MAX_RECORD_BYTES,
    EpochStarted,
    LegacyEvent,
    Oneof,
    Record,
)
from vs_runlog.api import (
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
        with FileRunLogWriter(path) as writer:
            for record in expected:
                writer.append(record)
        with path.open("rb") as stream:
            reader = RunLogReader(stream)
            assert list(reader) == expected
            assert reader.stop.reason is StopReason.END
        with FileRunLogWriter(path) as reopened:
            assert reopened.sequence == len(expected)


@given(st.lists(_TEXT, max_size=6), st.data())
def test_any_truncation_is_clean_or_torn(texts: list[str], data: st.DataObject) -> None:
    encoded = RUN_LOG_MAGIC + b"".join(map(encode_frame, records(texts)))
    offset = data.draw(st.integers(min_value=0, max_value=len(encoded)))
    reader = RunLogReader(io.BytesIO(encoded[:offset]))
    list(reader)
    assert reader.stop.reason in (StopReason.END, StopReason.TORN_TAIL)


@given(_TEXT, st.data())
def test_each_payload_byte_flip_is_detected(text: str, data: st.DataObject) -> None:
    record = records([text])[0]
    encoded = bytearray(RUN_LOG_MAGIC + encode_frame(record))
    payload_start = len(encoded) - len(record.to_binary())
    index = data.draw(st.integers(min_value=payload_start, max_value=len(encoded) - 1))
    encoded[index] ^= data.draw(st.integers(min_value=1, max_value=255))
    reader = RunLogReader(io.BytesIO(encoded))
    assert list(reader) == []
    assert reader.stop.reason is StopReason.CORRUPTION
    assert reader.stop.offset == len(RUN_LOG_MAGIC)


def test_shared_fixtures_regenerate_identically() -> None:
    root = Path(__file__).resolve().parents[3] / "proto/testdata/runlog"
    generated = runpy.run_path(str(root / "generate.py"))["fixtures"]()
    for name, expected in generated.items():
        assert (root / name).read_bytes() == expected
    for name, expected in json.loads(generated["manifest.json"]).items():
        reader = RunLogReader(io.BytesIO(generated[name]))
        actual = list(reader)
        for record, fields in zip(actual, expected["records"], strict=True):
            assert record.seq == fields["seq"]
            assert record.epoch == fields["epoch"]
            assert record.at is not None
            assert record.at.seconds == fields["at"]["seconds"]
            assert record.at.nanos == fields["at"]["nanos"]
            assert record.body is not None
            if record.body.field == "legacy_event":
                assert record.body.value == LegacyEvent(**fields["legacy_event"])
            else:
                assert record.body.value == EpochStarted(**fields["epoch_started"])
        assert reader.stop.reason == expected["stop"]
        assert reader.stop.offset == expected["offset"]


def test_cap_and_sequence_errors_leave_log_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    with FileRunLogWriter(path) as writer:
        with pytest.raises(ValueError, match="seq"):
            writer.append(records(["a", "b"])[1])
        with pytest.raises(ValueError, match="exceeds"):
            writer.append(records(["x" * RUN_LOG_MAX_RECORD_BYTES])[0])
    assert path.read_bytes() == RUN_LOG_MAGIC


def test_resume_offset_and_recover_torn_tail(tmp_path: Path) -> None:
    first, second = records(["one", "two"])
    boundary = len(RUN_LOG_MAGIC + encode_frame(first))
    path = tmp_path / "run.log"
    path.write_bytes(RUN_LOG_MAGIC + encode_frame(first) + encode_frame(second)[:-1])
    with FileRunLogWriter(path) as writer:
        assert path.stat().st_size == boundary
        writer.append(second)
    with path.open("rb") as stream:
        assert list(RunLogReader(stream, boundary)) == [second]


@pytest.mark.parametrize(
    "contents", [b"BADMAGIC", RUN_LOG_MAGIC + b"\x81\x80\x40", RUN_LOG_MAGIC + b"\x80" * 10]
)
def test_corruption_is_never_repaired(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "run.log"
    path.write_bytes(contents)
    with pytest.raises(ValueError, match="corrupt"):
        FileRunLogWriter(path)
    assert path.read_bytes() == contents


@given(
    st.lists(
        st.tuples(st.booleans(), _TEXT, st.integers(min_value=0, max_value=2**32 - 1)), max_size=8
    )
)
def test_both_record_kinds_round_trip_with_arbitrary_epochs(
    bodies: list[tuple[bool, str, int]],
) -> None:
    expected = [
        Record(
            seq=i,
            epoch=epoch,
            body=(
                Oneof("epoch_started", EpochStarted(run_id=text, epoch=epoch))
                if started
                else Oneof("legacy_event", LegacyEvent(json=text))
            ),
        )
        for i, (started, text, epoch) in enumerate(bodies, 1)
    ]
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        with FileRunLogWriter(path) as writer:
            for record in expected:
                writer.append(record)
        with path.open("rb") as stream:
            assert list(RunLogReader(stream)) == expected


def test_writer_accepts_exact_cap_and_rejects_one_byte_over(tmp_path: Path) -> None:
    record = records(["x" * RUN_LOG_MAX_RECORD_BYTES])[0]
    overflow = len(record.to_binary()) - RUN_LOG_MAX_RECORD_BYTES
    record.body = Oneof(
        "legacy_event", LegacyEvent(json="x" * (RUN_LOG_MAX_RECORD_BYTES - overflow))
    )
    assert len(record.to_binary()) == RUN_LOG_MAX_RECORD_BYTES
    with FileRunLogWriter(tmp_path / "run.log") as writer:
        writer.append(record)
        record.seq = 2
        record.body.value.json += "x"
        with pytest.raises(ValueError, match="exceeds"):
            writer.append(record)
        assert writer.sequence == 1


def test_closed_writer_cannot_append_and_close_is_idempotent(tmp_path: Path) -> None:
    writer = FileRunLogWriter(tmp_path / "run.log")
    writer.close()
    writer.close()
    with pytest.raises(ValueError, match="closed"):
        writer.append(records(["one"])[0])
    assert writer.sequence == 0


@given(st.lists(_TEXT, max_size=4), st.data())
def test_recovery_at_any_truncation_preserves_committed_prefix(
    texts: list[str],
    data: st.DataObject,
) -> None:
    encoded = RUN_LOG_MAGIC + b"".join(map(encode_frame, records(texts)))
    offset = data.draw(st.integers(min_value=0, max_value=len(encoded)))
    prefix = encoded[:offset]
    reader = RunLogReader(io.BytesIO(prefix))
    committed = list(reader)
    next_record = Record(
        seq=len(committed) + 1, epoch=2, body=Oneof("legacy_event", LegacyEvent(json="new"))
    )
    with TemporaryDirectory() as directory:
        path = Path(directory) / "run.log"
        path.write_bytes(prefix)
        with FileRunLogWriter(path) as writer:
            writer.append(next_record)
        with path.open("rb") as stream:
            recovered = RunLogReader(stream)
            assert list(recovered) == [*committed, next_record]
            assert recovered.stop.reason is StopReason.END
