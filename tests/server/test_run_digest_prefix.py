"""A list row's digest costs the records after a keyframe, never the run's whole log.

Every read is a new process, so nothing remembers an earlier fold, and a launch reads the digest of
every run it lists. A digest that decoded and validated each record of each log made the first
paint grow with the history of all runs. The probe here is damage in the part of the log a keyframe
covers: a read that still decodes those records cannot return the digest.
"""

from __future__ import annotations

from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from server.run_digest_store import read_digest
from vs_proto.api import RunHead

RETAINED = Path(__file__).resolve().parents[2] / "proto/testdata/retained/0064a1bc"
"""A retained previous-writer run of 1120 records with a keyframe at its last one."""


def previous_writer_run(directory: Path) -> Path:
    """The retained run with a head that states no epoch, input or performance, and its log path."""
    (directory / "keyframes").mkdir()
    copyfile(RETAINED / "run.log", directory / "run.log")
    copyfile(RETAINED / "keyframes" / "1120.pb", directory / "keyframes" / "1120.pb")
    head = RunHead.from_binary((RETAINED / "run.head").read_bytes())
    assert head.objective is not None
    head.epoch = 0
    head.objective.input_stated = False
    head.objective.clear_field("input_value")
    head.performance_stated = False
    (directory / "run.head").write_bytes(head.to_binary())
    return directory / "run.log"


@pytest.mark.regression
@example(offset=9, noise=b"\xff")
@example(offset=300, noise=b"\x00\x01\x02")
@given(offset=st.integers(8, 4000), noise=st.binary(min_size=1, max_size=16))
def test_a_digest_read_never_decodes_the_records_a_keyframe_covers(
    offset: int, noise: bytes
) -> None:
    """Damage anywhere before the keyframe's offset leaves the digest as it was."""
    with TemporaryDirectory() as temporary:
        log = previous_writer_run(Path(temporary))
        before = read_digest(log)
        data = bytearray(log.read_bytes())
        data[offset : offset + len(noise)] = noise
        log.write_bytes(bytes(data))
        assert read_digest(log) == before
