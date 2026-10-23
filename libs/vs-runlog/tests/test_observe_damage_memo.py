"""Unchanged damaged prefixes cost one scan; new identities are read again."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import RunHead
from vs_runlog.api import HeadObserver, RunLogDamagedError

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained"
CASES = sorted(path.name for path in CORPUS.iterdir() if path.is_dir())


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES)
@example(kind="cut", at=1, reads=10)
@given(
    kind=st.sampled_from(["cut", "crc", "fence"]),
    at=st.integers(min_value=1, max_value=100_000),
    reads=st.integers(min_value=2, max_value=20),
)
def test_damaged_prefix_scan_bound_and_repair(case: str, kind: str, at: int, reads: int) -> None:
    """Generate torn frames, bad checksums and fences past EOF across retained writers."""
    original = (CORPUS / case / "run.log").read_bytes()
    head = RunHead.from_binary((CORPUS / case / "run.head").read_bytes())
    damaged = original
    if kind == "cut":
        damaged = original[: -(1 + at % len(original))]
    elif kind == "crc":
        damaged = original[:9] + bytes([original[9] ^ 255]) + original[10:]
    else:
        head.committed_seq += at
    observer = HeadObserver()
    with TemporaryDirectory() as temporary:
        log = Path(temporary) / "run.log"
        log.write_bytes(damaged)
        log.with_name("run.head").write_bytes(head.to_binary())
        for _ in range(reads):
            with pytest.raises(RunLogDamagedError):
                observer.read(log)
        assert observer.scans == 1
        log.write_bytes(original)
        log.with_name("run.head").write_bytes((CORPUS / case / "run.head").read_bytes())
        assert observer.read(log).epoch == 1
        assert observer.scans == 2
