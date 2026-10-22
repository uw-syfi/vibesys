"""Store projections, durability boundaries and lifetime ownership via public APIs."""

from __future__ import annotations

import io
import json
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.any_pb import Any

from vs_proto.api import (
    RUN_LOG_MAGIC,
    AgentTurnAttrs,
    Change,
    Hold,
    Keyframe,
    Label,
    Lifecycle,
    MetricPoint,
    Oneof,
    OperatorHold,
    Record,
    RunHead,
    Running,
    SpanRecord,
    Timestamp,
    TokenUsage,
    validate,
)
from vs_runlog.api import (
    FileRunLogWriter,
    RunLock,
    RunLogReader,
    WriterIdentityState,
    encode_frame,
    fold_skeleton,
    observe_writer_identity,
)
from vs_sim.api.testing import ManualClock, SimThreads

_FIXTURES = Path(__file__).resolve().parents[3] / "proto/testdata/skeleton"


def scenarios() -> dict[str, tuple[list[Record], Keyframe]]:
    return runpy.run_path(str(_FIXTURES / "generate.py"))["scenarios"]()


@pytest.mark.parametrize("name", ["core", "epochs", "legacy"])
def test_shared_conformance_streams(name: str) -> None:
    directory = _FIXTURES / name
    expected = Keyframe.from_binary((directory / "expected.pb").read_bytes())
    records = [
        Record.from_json(json.dumps(r))
        for r in json.loads((directory / "records.json").read_text())
    ]
    validate(expected)
    assert list(RunLogReader(io.BytesIO((directory / "run.log").read_bytes()))) == records
    assert fold_skeleton(records, log_offset=expected.log_offset) == expected
    # Every possible split has the same result when bootstrapping from a keyframe.
    for split in range(1, len(records)):
        seed = fold_skeleton(
            records[:split],
            log_offset=len(RUN_LOG_MAGIC) + sum(len(encode_frame(r)) for r in records[:split]),
        )
        before = seed.to_binary()
        assert fold_skeleton(records[split:], seed=seed, log_offset=expected.log_offset) == expected
        assert seed.to_binary() == before


def test_fixtures_regenerate_identically() -> None:
    generated = runpy.run_path(str(_FIXTURES / "generate.py"))["fixtures"]()
    for name, payload in generated.items():
        assert (_FIXTURES / name).read_bytes() == payload, name


def test_writer_publishes_head_and_lifecycle_keyframes(tmp_path: Path) -> None:
    records, expected = scenarios()["core"]
    with FileRunLogWriter(
        tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()
    ) as writer:
        for record in records:
            writer.append(record)
            writer.commit()
            head = RunHead.from_binary((tmp_path / "run.head").read_bytes())
            validate(head)
            assert head.committed_seq == record.seq
            if record.seq == 1:
                assert head.awaiting_lifecycle
                assert head.lifecycle is None
                assert head.epoch == record.epoch
            assert record.body is not None
            if record.body.field in {"lifecycle", "epoch_started"}:
                path = tmp_path / "keyframes" / f"{record.seq}.pb"
                checkpoint = Keyframe.from_binary(path.read_bytes())
                assert checkpoint.log_offset == (tmp_path / "run.log").stat().st_size
                assert checkpoint.through_seq == record.seq
    assert Keyframe.from_binary((tmp_path / "keyframes/10.pb").read_bytes()) == expected
    assert head.objective is not None
    assert head.objective.best_value == 4
    assert head.objective.input_value == 2
    assert [p.value for p in head.chart_points] == [2, 4]
    assert head.lifecycle is not None
    assert head.lifecycle.state is not None
    assert head.lifecycle.state.field == "ended"


@given(st.integers(min_value=0, max_value=600))
@example(count=600)
def test_record_bound_and_recovery_preserve_immutable_checkpoints(count: int) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "run.log"
        first = scenarios()["core"][0][:2]
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            for record in first:
                writer.append(record)
            for seq in range(3, count + 3):
                writer.append(
                    Record(
                        seq=seq,
                        epoch=1,
                        at=Timestamp(seconds=seq),
                        body=Oneof("metric", MetricPoint(series="progress", value=float(seq))),
                    )
                )
                # Exercise checkpoint cadence with batches rather than one
                # storage barrier for every generated record.
                if (seq - 2) % 32 == 0:
                    writer.commit()
        files = {p.name: p.read_bytes() for p in (root / "keyframes").iterdir()}
        sequences = sorted(int(name.removesuffix(".pb")) for name in files)
        assert (
            max(b - a for a, b in zip([0, *sequences], [*sequences, count + 2], strict=True)) <= 256
        )
        # A failed append's complete prefix is recovered; a torn tail is dropped.
        with path.open("ab") as stream:
            stream.write(b"\x80")
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            assert writer.sequence == count + 2
            writer.append(
                Record(
                    seq=count + 3,
                    epoch=1,
                    at=Timestamp(seconds=count + 3),
                    body=Oneof("metric", MetricPoint(series="progress", value=float(count))),
                )
            )
        for name, payload in files.items():
            assert (root / "keyframes" / name).read_bytes() == payload
        assert RunHead.from_binary((root / "run.head").read_bytes()).committed_seq == count + 3


def test_exclusive_lock_covers_open_recovery_and_release(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
        with pytest.raises(BlockingIOError):
            FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
        # All log names in a run share one lifetime lock.
        with pytest.raises(BlockingIOError):
            FileRunLogWriter(tmp_path / "other.log", clock=ManualClock(), threads=SimThreads())
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
        pass
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads())
    path.unlink()
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
        pass


def test_failed_publication_releases_lock_and_recovery_republishes(tmp_path: Path) -> None:
    records, _ = scenarios()["core"]
    (tmp_path / "run.head").mkdir()
    writer = FileRunLogWriter(tmp_path / "run.log", clock=ManualClock(), threads=SimThreads())
    with pytest.raises(OSError, match=r"run.head"):
        writer.append(records[0])
    (tmp_path / "run.head").rmdir()
    assert not list(tmp_path.glob(".run.head.*"))
    with FileRunLogWriter(
        tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()
    ) as recovered:
        assert recovered.sequence == 1
        head = RunHead.from_binary((tmp_path / "run.head").read_bytes())
        assert head.committed_seq == 1
        assert head.awaiting_lifecycle
        checkpoint = Keyframe.from_binary((tmp_path / "keyframes/1.pb").read_bytes())
        assert checkpoint.log_offset == (tmp_path / "run.log").stat().st_size
        recovered.append(records[1])
        recovered.append(records[2])
    assert RunHead.from_binary((tmp_path / "run.head").read_bytes()).committed_seq == 3


def test_fold_rejects_sequence_gaps_without_mutating_seed() -> None:
    seed = Keyframe(through_seq=1, log_offset=8)
    with pytest.raises(ValueError, match=r"record.seq"):
        fold_skeleton([Record(seq=3)], seed=seed, log_offset=20)
    assert seed.through_seq == 1


def test_fold_owns_nested_inputs_and_seed() -> None:
    records, _ = scenarios()["core"]
    folded = fold_skeleton(records, log_offset=100)
    original = folded.to_binary()
    assert records[0].body is not None
    assert records[0].body.field == "epoch_started"
    assert records[0].body.value.run is not None
    records[0].body.value.run.strategy = "changed"
    assert records[2].body is not None
    assert records[2].body.field == "span"
    assert records[2].body.value.span is not None
    records[2].body.value.span.name = "changed"
    assert folded.to_binary() == original


def test_usage_replacement_presence_and_operator_hold(tmp_path: Path) -> None:
    records, _ = scenarios()["core"]
    first = records[:2]
    assert records[2].body is not None
    assert records[2].body.field == "span"
    span = records[2].body.value.span
    assert span is not None
    span.label = Label(
        kind="agent_turn",
        attrs=Any(
            type_url="type.googleapis.com/vibesys.store.v1.AgentTurnAttrs",
            value=AgentTurnAttrs(
                agent="agent", role="worker", stage="coding", usage=TokenUsage(input=0, output=5)
            ).to_binary(),
        ),
    )
    with FileRunLogWriter(
        tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()
    ) as writer:
        for record in first:
            writer.append(record)
        writer.append(records[2])
        writer.commit()
        head = RunHead.from_binary((tmp_path / "run.head").read_bytes())
        assert head.span_counts == {"agent_turn": 1}
        assert head.usage is not None
        assert head.usage.has_field("input")
        assert not head.usage.has_field("cost_usd")
        assert head.usage.output == 5
        assert span.label is not None
        assert span.label.attrs is not None
        span.label.attrs.value = AgentTurnAttrs(
            agent="agent", role="worker", stage="coding", usage=TokenUsage(output=8)
        ).to_binary()
        writer.append(
            Record(
                seq=4,
                epoch=1,
                at=Timestamp(),
                body=Oneof("span", SpanRecord(change=Change.UPDATED, span=span)),
            )
        )
        held = Lifecycle(
            state=Oneof("running", Running(hold=Hold(reason=Oneof("operator", OperatorHold()))))
        )
        writer.append(Record(seq=5, epoch=1, at=Timestamp(), body=Oneof("lifecycle", held)))
        head = RunHead.from_binary((tmp_path / "run.head").read_bytes())
        assert head.needs_user
        assert head.span_counts == {"agent_turn": 1}
        assert head.usage is not None
        assert head.usage.output == 8
        assert not head.usage.has_field("input")
        assert head.process is not None
        assert head.process.epoch == 1
        assert head.process.pid > 0


@pytest.mark.parametrize("tail", [b"", b"\x80", b"\x03x"])
def test_recovery_publishes_the_exact_durable_prefix(tmp_path: Path, tail: bytes) -> None:
    records, _ = scenarios()["core"]
    prefix = records[:4]
    with FileRunLogWriter(
        tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()
    ) as writer:
        for record in prefix:
            writer.append(record)
    boundary = (tmp_path / "run.log").stat().st_size
    with (tmp_path / "run.log").open("ab") as stream:
        stream.write(tail)
    (tmp_path / "run.head").unlink()
    with FileRunLogWriter(tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()):
        expected = fold_skeleton(prefix, log_offset=boundary)
        actual = Keyframe.from_binary((tmp_path / "keyframes/4.pb").read_bytes())
        assert actual == expected
        assert (tmp_path / "run.log").stat().st_size == boundary
        assert RunHead.from_binary((tmp_path / "run.head").read_bytes()).committed_seq == 4


def test_conflicting_checkpoint_is_preserved_and_releases_lock(tmp_path: Path) -> None:
    records, _ = scenarios()["core"]
    with FileRunLogWriter(
        tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()
    ) as writer:
        writer.append(records[0])
    checkpoint = tmp_path / "keyframes/1.pb"
    checkpoint.write_bytes(b"conflict")
    with pytest.raises(ValueError, match="immutable keyframe conflicts"):
        FileRunLogWriter(tmp_path / "run.log", clock=ManualClock(), threads=SimThreads())
    assert checkpoint.read_bytes() == b"conflict"
    checkpoint.unlink()
    with FileRunLogWriter(tmp_path / "run.log", clock=ManualClock(), threads=SimThreads()):
        assert Keyframe.from_binary(checkpoint.read_bytes()).through_seq == 1


@given(st.lists(st.integers(min_value=2, max_value=8), max_size=32))
def test_fold_composes_at_every_cut(choices: list[int]) -> None:
    templates, _ = scenarios()["core"]
    records = [*templates[:2]]
    for choice in choices:
        record = Record.from_binary(templates[choice].to_binary())
        record.seq = len(records) + 1
        records.append(record)
    offsets = [len(RUN_LOG_MAGIC)]
    for record in records:
        offsets.append(offsets[-1] + len(encode_frame(record)))
    expected = fold_skeleton(records, log_offset=offsets[-1])
    for split in range(len(records) + 1):
        seed = fold_skeleton(records[:split], log_offset=offsets[split])
        before = seed.to_binary()
        assert fold_skeleton(records[split:], seed=seed, log_offset=offsets[-1]) == expected
        assert seed.to_binary() == before


@pytest.mark.regression
@example(offset=1)
@given(st.integers(min_value=1, max_value=1000))
def test_writer_identity_fences_every_process_field_and_survives_a_successor_lock(
    offset: int,
) -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "run.log"
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()) as writer:
            writer.append(scenarios()["core"][0][0])
            head = RunHead.from_binary((root / "run.head").read_bytes())
            process = head.process
            assert process is not None
            assert observe_writer_identity(path, process) is WriterIdentityState.HELD
            for field in ("pid", "epoch", "started_at"):
                changed = type(process).from_binary(process.to_binary())
                if field == "started_at":
                    assert changed.started_at is not None
                    changed.started_at.seconds += offset
                else:
                    setattr(changed, field, getattr(changed, field) + offset)
                assert observe_writer_identity(path, changed) is WriterIdentityState.UNKNOWN
        assert observe_writer_identity(path, process) is WriterIdentityState.RELEASED
        successor = RunLock(root / "run.lock")
        try:
            assert observe_writer_identity(path, process) is WriterIdentityState.RELEASED
        finally:
            successor.close()
