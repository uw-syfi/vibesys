"""Previous-writer heads reopen without requiring their writer to run again."""

import io
import json
from collections.abc import Callable
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import RUN_LOG_MAGIC, MetricPoint, Oneof, RunHead
from vs_runlog.api import RunLogReader, encode_frame, read_run_head

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained"
CASES = json.loads((CORPUS / "manifest.json").read_text())
FULL = 64  # any fence past the log reads the whole published prefix
FIRST = sorted(CASES)[0]


def pinned_for_each_case(**fixed: object) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Pin one example per retained writer, so every writer runs at every depth.

    The generated examples draw the writer along with the rest of the input; a scan validates
    every record of a log, so the search spends its examples across writers instead of repeating
    the whole search for each of them.
    """

    def pin(test: Callable[..., None]) -> Callable[..., None]:
        for case in CASES:
            test = example(case=case, **fixed)(test)
        return test

    return pin


@pytest.mark.regression
@pytest.mark.parametrize("version", CASES)
def test_retained_head_has_the_previous_writers_input(version: str) -> None:
    directory = CORPUS / version
    original = (directory / "run.head").read_bytes()
    head = read_run_head(directory / "run.log")
    assert head.objective is not None
    assert head.objective.input_value == CASES[version]["input"]
    assert head.performance_stated
    assert head.objective.best_value == CASES[version]["best"]
    facts = head.performance[0]
    assert facts.best_value == CASES[version]["best"]
    assert facts.best_value / facts.input_value == CASES[version]["ratio"]
    assert (
        sum(idea.has_field("measurement_seq") for idea in facts.ideas) == CASES[version]["measured"]
    )
    assert all(point.outcome.name == "KEPT" for point in facts.points if point.new_best)
    assert (directory / "run.head").read_bytes() == original


@cache
def retained_frames(case: str) -> tuple[bytes, ...]:
    """A retained log's records, each encoded as the frame it was written in."""
    log = (CORPUS / case / "run.log").read_bytes()
    return tuple(encode_frame(record) for record in RunLogReader(io.BytesIO(log)))


@pytest.mark.regression
@pinned_for_each_case(value=100.0, spanless=False, fence=FULL)
@example(case=FIRST, value=0.0, spanless=True, fence=FULL)
@example(case=FIRST, value=0.0, spanless=False, fence=0)
@example(case=FIRST, value=0.0, spanless=False, fence=1)
@given(
    case=st.sampled_from(sorted(CASES)),
    value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    spanless=st.booleans(),
    fence=st.integers(0, 64),
)
def test_every_retained_input_encoding_respects_the_published_prefix(
    *, case: str, value: float, spanless: bool, fence: int
) -> None:
    """Signed and zero inputs, both previous encodings, and every possible publication fence."""
    source = CORPUS / case
    frames = list(retained_frames(case))
    fence = min(fence, len(frames))
    at = CASES[case]["measurement_seq"] - 1
    record = next(iter(RunLogReader(io.BytesIO(RUN_LOG_MAGIC + frames[at]))))
    assert record.body is not None
    assert record.body.field == "metric"
    old = record.body.value
    point = MetricPoint(series=old.series, value=value, clock=old.clock)
    if not spanless:
        point.span = old.span
    record.body = Oneof("metric", point)
    frames[at] = encode_frame(record)
    raw = RunHead.from_binary((source / "run.head").read_bytes())
    assert raw.objective is not None
    raw.objective.clear_field("input_value")
    raw.objective.input_stated = False
    raw.committed_seq = fence
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "run.log").write_bytes(RUN_LOG_MAGIC + b"".join(frames))
        (directory / "run.head").write_bytes(raw.to_binary())
        head = read_run_head(directory / "run.log")
    assert head.objective is not None
    assert head.objective.has_field("input_value") == (fence >= record.seq)
    if fence >= record.seq:
        assert head.objective.input_value == value
