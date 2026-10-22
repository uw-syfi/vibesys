"""Epoch identity and lifecycle have one projection across records, replay and keyframes."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import EpochStarted, Lifecycle, Oneof, Record, Running
from vs_runlog.api import current_epoch, fold_skeleton


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
