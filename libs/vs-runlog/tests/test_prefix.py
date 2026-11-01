"""A committed prefix is folded once from its nearest keyframe, and says what a full scan says.

``read_run_head`` decodes and validates every record of a log to recover what a previous writer left
implicit. The list rows of a launch need the head and the skeleton of every run, each read is a new
process, and so a scan per run per launch made the first paint grow with the history of all runs.
``read_committed_prefix`` folds from the keyframe instead; these tests hold it to the head and
skeleton the full scan gives, for every retained writer, fence, head and set of keyframes, and to
the cost it exists for: the records the keyframe covers are never decoded.
"""

import json
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import Record, RunHead
from vs_runlog.api import (
    HeadObserver,
    RunLogReader,
    fold_skeleton,
    read_committed_prefix,
    read_run_head,
    upgrade_skeleton,
)

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained"
CASES = json.loads((CORPUS / "manifest.json").read_text())
CASE = st.sampled_from(sorted(CASES))
FIRST = sorted(CASES)[0]
LONG = "0064a1bc"
"""The retained writer whose log is long enough to hold a keyframe far from its end."""


def run_copy(directory: Path, case: str, keyframes: set[int]) -> Path:
    """The retained run ``case`` in ``directory`` with only the keyframes named, and its log's path."""
    copyfile(CORPUS / case / "run.log", directory / "run.log")
    copyfile(CORPUS / case / "run.head", directory / "run.head")
    (directory / "keyframes").mkdir()
    for keyframe in (CORPUS / case / "keyframes").glob("*.pb"):
        if int(keyframe.stem) in keyframes:
            copyfile(keyframe, directory / "keyframes" / keyframe.name)
    return directory / "run.log"


def state_head(
    directory: Path, *, fence: int | None, input_stated: bool, epoch: bool, performance: bool
) -> None:
    """Write the head a writer of that age left: each fact stated or left to the log."""
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    if fence is not None:
        head.committed_seq = fence
    assert head.objective is not None
    head.objective.input_stated = input_stated
    if not input_stated:
        head.objective.clear_field("input_value")
    head.performance_stated = performance
    if not epoch:
        head.epoch = 0
    (directory / "run.head").write_bytes(head.to_binary())


def records_through(log: Path, fence: int) -> list[Record]:
    with log.open("rb") as stream:
        return [record for record in RunLogReader(stream) if record.seq <= fence]


@pytest.mark.regression
@example(case=FIRST, fence=18, stated=(False, False, False), keyframes={2, 18})
@example(case=LONG, fence=1120, stated=(False, True, True), keyframes={1120})
@example(case=LONG, fence=17, stated=(True, False, False), keyframes={1, 2})
@given(
    case=CASE,
    fence=st.integers(1, 18),
    stated=st.tuples(st.booleans(), st.booleans(), st.booleans()),
    keyframes=st.sets(st.sampled_from([1, 2, 18, 1120])),
)
def test_a_prefix_read_observes_the_head_and_skeleton_a_full_scan_gives(
    case: str, fence: int, stated: tuple[bool, bool, bool], keyframes: set[int]
) -> None:
    """Whichever keyframe seeds the read, or none, head and skeleton are the scan's.

    ``stated`` says which facts the head states: its input, its epoch and its performance.
    """
    input_stated, epoch, performance = stated
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        log = run_copy(directory, case, keyframes)
        state_head(
            directory, fence=fence, input_stated=input_stated, epoch=epoch, performance=performance
        )
        prefix = read_committed_prefix(log)
        assert prefix.head == HeadObserver().read(log)
        records = records_through(log, fence)
        expected = upgrade_skeleton(fold_skeleton(records, log_offset=0))
        # A keyframe an older writer wrote lacks the epoch start time and the two tracking flags a
        # fresh fold sets, and a seeded fold keeps what its seed says; everything a reader
        # derives from the prefix is compared.
        got = prefix.skeleton
        assert got.through_seq == expected.through_seq == fence
        assert got.spans == expected.spans
        assert got.metrics == expected.metrics
        assert got.lifecycles == expected.lifecycles
        assert got.streams == expected.streams
        assert [(row.seq, row.epoch) for row in got.epochs] == [
            (row.seq, row.epoch) for row in expected.epochs
        ]
        last = records[-1].at
        assert last is not None
        assert prefix.last_record_at in (None, last.to_datetime())


@pytest.mark.regression
@example(offset=9, noise=b"\xff")
@example(offset=300, noise=b"\x00\x01\x02")
@given(offset=st.integers(8, 4000), noise=st.binary(min_size=1, max_size=16))
def test_a_prefix_read_never_decodes_the_records_a_keyframe_covers(
    offset: int, noise: bytes
) -> None:
    """Damage before the keyframe's offset is invisible to it, though a full scan finds it."""
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        log = run_copy(directory, LONG, {1120})
        state_head(directory, fence=None, input_stated=False, epoch=False, performance=False)
        before = read_committed_prefix(log)
        data = bytearray(log.read_bytes())
        data[offset : offset + len(noise)] = noise
        if bytes(data) == log.read_bytes():
            return
        log.write_bytes(bytes(data))
        assert read_committed_prefix(log).head == before.head
        with pytest.raises(ValueError, match=r"run\.log"):
            read_run_head(log)
