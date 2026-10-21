"""The measured owner-loop starvation regression, using the original public API."""

import runpy
from pathlib import Path

import pytest

from vs_proto.api import LegacyEvent, Oneof, Record, RunHead
from vs_runlog.api import FileRunLogWriter
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

_FIXTURES = Path(__file__).resolve().parents[3] / "proto/testdata/skeleton"


def declarations() -> list[Record]:
    return runpy.run_path(str(_FIXTURES / "generate.py"))["scenarios"]()["core"][0][:2]


def event(seq: int) -> Record:
    return Record(seq=seq, epoch=1, body=Oneof("legacy_event", LegacyEvent(json=str(seq))))


def head(root: Path) -> int:
    return RunHead.from_binary((root / "run.head").read_bytes()).committed_seq


@pytest.mark.regression
def test_owner_loop_work_cannot_starve_commit_deadline(tmp_path: Path) -> None:
    """The measured seq 3 / busy owner loop / seq 4 reproduction."""
    clock = VirtualClock()
    threads = SimThreads(clock)

    async def exercise() -> None:
        with FileRunLogWriter(tmp_path / "run.log", clock=clock, threads=threads) as writer:
            for record in declarations():
                writer.append(record)
            writer.append(event(3))
            # Model synchronous owner-loop work by advancing only the public
            # timeline, exactly as in the measured reproduction. Do not yield.
            clock.at += 1.0
            writer.append(event(4))
            assert head(tmp_path) >= 3

    run_virtual(clock, exercise())
