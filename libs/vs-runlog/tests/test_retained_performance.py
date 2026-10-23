"""All observations upgrade a previous writer's decisions once per committed prefix."""

import io
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import RUN_LOG_MAGIC, Direction, EpochStarted, IdeaOutcome, Oneof, Record, RunHead
from vs_runlog.api import HeadObserver, RunLogReader, encode_frame

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained/64c1a8b7"


@pytest.mark.regression
@example(input_value=100, candidate=102, minimize=False, reads=["head", "epoch", "head"])
@given(
    input_value=st.integers(0, 1000),
    candidate=st.integers(0, 2000),
    minimize=st.booleans(),
    reads=st.lists(st.sampled_from(["head", "epoch"]), min_size=1, max_size=10),
)
def test_previous_performance_is_shared_by_every_observation(
    input_value: int, candidate: int, reads: list[str], *, minimize: bool
) -> None:
    """Generate directions, zero and signed relative improvements, and arbitrary read order."""
    records = list(RunLogReader(io.BytesIO((CORPUS / "run.log").read_bytes())))
    for record in records:
        body = record.body
        if body is None:
            continue
        if isinstance(body.value, EpochStarted):
            assert body.value.run is not None
            for objective in body.value.run.objectives:
                objective.direction = Direction.MIN if minimize else Direction.MAX
            for series in body.value.series:
                series.direction = Direction.MIN if minimize else Direction.MAX
        elif body.field == "metric":
            body.value.value = input_value if body.value.span == "input-gate" else candidate
    data = RUN_LOG_MAGIC + b"".join(encode_frame(record) for record in records)
    raw = (CORPUS / "run.head").read_bytes()
    with TemporaryDirectory() as temporary:
        log = Path(temporary) / "run.log"
        log.write_bytes(data)
        log.with_name("run.head").write_bytes(raw)
        reader = HeadObserver()
        for kind in [*reads, "head"]:
            if kind == "epoch":
                assert reader.current_epoch(log) is not None
            else:
                head = reader.read(log)
                assert head.performance_stated
                assert head.objective is not None
                best = min(input_value, candidate) if minimize else max(input_value, candidate)
                assert head.objective.best_value == best
                assert head.performance[0].best_value == best
                assert all(
                    p.outcome == IdeaOutcome.KEPT for p in head.performance[0].points if p.new_best
                )
                assert head.process == RunHead.from_binary(raw).process
                head.performance[0].best_value = -1
        assert reader.read(log).performance[0].best_value == best
        assert reader.scans == 1
        assert log.read_bytes() == data
        assert log.with_name("run.head").read_bytes() == raw


@example(gaps=[5, 6])
@given(gaps=st.lists(st.integers(1, 8), min_size=1, max_size=5))
def test_phase_one_zero_and_sparse_epochs_remain_observable(gaps: list[int]) -> None:
    """The read upgrade retains every historical framing, including unheaded phase one."""
    sequence = 0
    records = [Record(seq=0, epoch=1, body=Oneof("epoch_started", EpochStarted(epoch=1)))]
    for epoch, gap in enumerate(gaps, start=2):
        sequence += gap
        records.append(
            Record(
                seq=sequence, epoch=epoch, body=Oneof("epoch_started", EpochStarted(epoch=epoch))
            )
        )
    with TemporaryDirectory() as temporary:
        log = Path(temporary) / "run.log"
        log.write_bytes(RUN_LOG_MAGIC + b"".join(encode_frame(record) for record in records))
        observer = HeadObserver()
        for _ in range(3):
            epoch = observer.current_epoch(log)
            assert epoch is not None
            assert epoch.number == len(gaps) + 1
        assert observer.scans == 1
