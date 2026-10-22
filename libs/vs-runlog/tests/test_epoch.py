"""Epoch identity and lifecycle have one projection across records, replay and keyframes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import RUN_LOG_MAGIC, EpochStarted, Lifecycle, Oneof, Record, RunHead, Running
from vs_runlog.api import current_epoch, encode_frame, fold_skeleton, read_current_epoch

from .test_run_writers import scenarios

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.regression
@example(epochs=[1, 5], amendments=2)
@given(
    epochs=st.lists(st.integers(1, 100), min_size=1, max_size=5, unique=True).map(sorted),
    amendments=st.integers(0, 5),
)
def test_current_epoch_survives_amendments_and_replay_without_inheriting_lifecycle(
    epochs: list[int],
    amendments: int,
) -> None:
    records: list[Record] = []
    for epoch in epochs:
        records.append(
            Record(
                seq=len(records) + 1,
                epoch=epoch,
                body=Oneof("epoch_started", EpochStarted(epoch=epoch)),
            )
        )
        assert current_epoch(records).number == epoch
        assert current_epoch(records).lifecycle is None
        records.append(
            Record(
                seq=len(records) + 1,
                epoch=epoch,
                body=Oneof("lifecycle", Lifecycle(state=Oneof("running", Running()))),
            )
        )
        expected = current_epoch(records)
        for index in range(amendments):
            records.append(
                Record(
                    seq=len(records) + 1,
                    epoch=epoch,
                    body=(
                        Oneof("declaration_amended", EpochStarted(epoch=epoch))
                        if index % 2
                        else Oneof("epoch_started", EpochStarted(epoch=epoch))
                    ),
                )
            )
            assert current_epoch(records) == expected
    before = [record.to_binary() for record in records]
    full = current_epoch(records)
    assert current_epoch(fold_skeleton(records, log_offset=8)) == full
    for cut in range(len(records) + 1):
        assert current_epoch(records[cut:], current_epoch(records[:cut])) == full
    assert [record.to_binary() for record in records] == before


@pytest.mark.regression
@pytest.mark.parametrize("committed", [0, 1, 2, 3, 4])
def test_epoch_readers_ignore_a_complete_uncommitted_successor(
    tmp_path: Path,
    committed: int,
) -> None:
    first = scenarios()["core"][0][:2]
    successor = Record.from_binary(first[0].to_binary())
    successor.seq = 3
    successor.epoch = 2
    assert successor.body is not None
    assert successor.body.field == "epoch_started"
    successor.body.value.epoch = 2
    running = Record.from_binary(first[1].to_binary())
    running.seq = 4
    running.epoch = 2
    assert running.body is not None
    assert running.body.field == "lifecycle"
    records = [*first, successor, running]
    path = tmp_path / "run.log"
    path.write_bytes(RUN_LOG_MAGIC + b"".join(encode_frame(record) for record in records))
    expected_epoch = (0, 1, 1, 2, 2)[committed]
    lifecycle = running.body.value if committed in (0, 2, 4) else None
    head = RunHead(
        strategy="example",
        committed_seq=committed,
        epoch=expected_epoch,
        lifecycle=lifecycle,
        awaiting_lifecycle=lifecycle is None,
    )
    (tmp_path / "run.head").write_bytes(head.to_binary())
    facts = read_current_epoch(path)
    assert facts is not None
    assert facts.number == expected_epoch
    assert facts.lifecycle == (running.body.value if committed in (2, 4) else None)


def test_a_typed_header_without_a_committed_head_cannot_confirm_an_epoch(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    record = scenarios()["core"][0][0]
    path.write_bytes(RUN_LOG_MAGIC + encode_frame(record))
    facts = read_current_epoch(path)
    assert facts is not None
    assert facts.number == 0
    assert facts.lifecycle is None


def test_missing_and_truncated_committed_epoch_sources_fail_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    assert read_current_epoch(path) is None
    head = RunHead(strategy="example", committed_seq=1, epoch=1, awaiting_lifecycle=True)
    (tmp_path / "run.head").write_bytes(head.to_binary())
    with pytest.raises(ValueError, match="without run"):
        read_current_epoch(path)
    path.write_bytes(RUN_LOG_MAGIC)
    with pytest.raises(ValueError, match="exceeds log sequence"):
        read_current_epoch(path)
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="bad magic"):
        read_current_epoch(path)


def test_legacy_epoch_headers_keep_the_complete_record_rule(tmp_path: Path) -> None:
    record = scenarios()["core"][0][0]
    assert record.body is not None
    assert record.body.field == "epoch_started"
    record.body.value.clear_field("run")
    path = tmp_path / "run.log"
    path.write_bytes(RUN_LOG_MAGIC + encode_frame(record))
    facts = read_current_epoch(path)
    assert facts is not None
    assert facts.number == 1
