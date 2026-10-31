"""A stale replay golden fails at once, naming its first differing record and the fix."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.replay_golden import REGENERATE, assert_replay_golden, first_difference

from vs_proto.api import ClientReplay, ClientReplayBatch, EpochStarted, Lifecycle, Oneof, Record

if TYPE_CHECKING:
    from pathlib import Path


def _record(seq: int, *, hash_: str = "a" * 64) -> bytes:
    body = (
        Oneof("epoch_started", EpochStarted(api_schema_hash=hash_))
        if seq == 1
        else Oneof("lifecycle", Lifecycle())
    )
    return Record(seq=seq, epoch=1, body=body).to_binary()


def _replay(batches: int, per_batch: int, *, stale: tuple[int, int] | None = None) -> ClientReplay:
    """``batches`` batches of ``per_batch`` records; ``stale`` marks one record's bytes as changed."""
    seq = 0
    built = []
    for index in range(batches):
        records = []
        for position in range(per_batch):
            seq += 1
            wire = _record(seq)
            if stale == (index, position):
                wire = (
                    _record(seq, hash_="b" * 64)
                    if seq == 1
                    else Record(seq=seq, epoch=2, body=Oneof("lifecycle", Lifecycle())).to_binary()
                )
            records.append(wire)
        built.append(ClientReplayBatch(source_sequence=index + 1, records=records))
    return ClientReplay(batches=built)


def test_a_current_golden_is_returned_decoded(tmp_path: Path) -> None:
    expected = _replay(3, 2)
    golden = tmp_path / "scenario.pb"
    golden.write_bytes(expected.to_binary())

    assert assert_replay_golden(golden, expected) == expected


@given(
    batches=st.integers(min_value=1, max_value=6),
    per_batch=st.integers(min_value=1, max_value=4),
    data=st.data(),
)
def test_a_stale_record_is_named_by_batch_and_position(
    batches: int, per_batch: int, data: st.DataObject
) -> None:
    index = data.draw(st.integers(min_value=0, max_value=batches - 1))
    position = data.draw(st.integers(min_value=0, max_value=per_batch - 1))

    lines = first_difference(
        _replay(batches, per_batch, stale=(index, position)), _replay(batches, per_batch)
    )

    assert lines[0] == f"batch {index},"
    assert lines[1].startswith(f"record {position} (seq {index * per_batch + position + 1},")
    assert len(lines) <= 7


@pytest.mark.parametrize(
    ("golden", "projected", "named"),
    [
        (_replay(2, 1), _replay(3, 1), "2 batches in the golden, 3 projected"),
        (_replay(1, 2), _replay(1, 3), "2 records in the golden, 3 projected"),
    ],
)
def test_a_missing_batch_or_record_is_counted(
    golden: ClientReplay, projected: ClientReplay, named: str
) -> None:
    assert named in "\n".join(first_difference(golden, projected))


def test_a_stale_stream_names_its_first_differing_byte() -> None:
    golden, projected = (
        ClientReplay(streams={"s": b"x" * 20_000}),
        ClientReplay(streams={"s": b"x" * 5_000 + b"y" * 15_000}),
    )

    lines = first_difference(golden, projected)

    assert lines[0] == "stream 's',"
    assert "  first difference at offset 5000" in lines


def test_the_failure_is_short_and_names_the_regeneration_command(tmp_path: Path) -> None:
    golden = tmp_path / "scenario.pb"
    golden.write_bytes(_replay(2, 2, stale=(0, 0)).to_binary())

    with pytest.raises(AssertionError) as raised:
        assert_replay_golden(golden, _replay(2, 2))

    message = str(raised.value)
    assert message.startswith("scenario.pb is not the current projection: batch 0,")
    assert f"Regenerate: {REGENERATE}" in message
    assert "epoch_started" in message
    assert len(message) < 1_500
