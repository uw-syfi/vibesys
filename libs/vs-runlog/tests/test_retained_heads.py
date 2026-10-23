"""Previous-writer heads reopen without requiring their writer to run again."""

import io
import json
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


@pytest.mark.regression
@pytest.mark.parametrize("version", CASES)
def test_retained_head_has_the_previous_writers_input(version: str) -> None:
    directory = CORPUS / version
    original = (directory / "run.head").read_bytes()
    head = read_run_head(directory / "run.log")
    assert head.objective is not None
    assert head.objective.input_value == CASES[version]["input"]
    assert (directory / "run.head").read_bytes() == original


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES)
@example(value=100.0, spanless=False, fence=FULL)
@example(value=0.0, spanless=True, fence=FULL)
@example(value=0.0, spanless=False, fence=0)
@example(value=0.0, spanless=False, fence=1)
@given(
    value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
    spanless=st.booleans(),
    fence=st.integers(0, 64),
)
def test_every_retained_input_encoding_respects_the_published_prefix(
    *, case: str, value: float, spanless: bool, fence: int
) -> None:
    """Signed and zero inputs, both previous encodings, and every possible publication fence."""
    source = CORPUS / case
    records = list(RunLogReader(io.BytesIO((source / "run.log").read_bytes())))
    fence = min(fence, len(records))
    record = records[CASES[case]["measurement_seq"] - 1]
    assert record.body is not None
    assert record.body.field == "metric"
    old = record.body.value
    point = MetricPoint(series=old.series, value=value, clock=old.clock)
    if not spanless:
        point.span = old.span
    record.body = Oneof("metric", point)
    raw = RunHead.from_binary((source / "run.head").read_bytes())
    assert raw.objective is not None
    raw.objective.clear_field("input_value")
    raw.objective.input_stated = False
    raw.committed_seq = fence
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "run.log").write_bytes(
            RUN_LOG_MAGIC + b"".join(encode_frame(r) for r in records)
        )
        (directory / "run.head").write_bytes(raw.to_binary())
        head = read_run_head(directory / "run.log")
    assert head.objective is not None
    assert head.objective.has_field("input_value") == (fence >= record.seq)
    if fence >= record.seq:
        assert head.objective.input_value == value
