"""C2: crash and replay at every durable boundary, for every registered strategy.

The host dies just before or just after the k-th commit; a new host with a new strategy
instance restarts over the store the dead one left, and the executors keep their world. The run
must reach the result an uninterrupted run reaches, end in an equal strategy state, and execute
no request identity twice. Every commit of the subject's smallest full run is a boundary; the
sweep is split into interleaved chunks so the workers share it.
"""

from __future__ import annotations

import pytest
from tests.composition._loop import _crash as crash
from tests.composition._loop.subjects import SUBJECTS

CHUNKS = 6
SUBJECT_NAMES = sorted(name for name, subject in SUBJECTS.items() if subject.sweeps_crashes)


@pytest.mark.parametrize("name", SUBJECT_NAMES)
@pytest.mark.parametrize("point", list(crash.CrashPoint))
@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_c2_a_crash_at_every_commit_of_a_run_replays_to_the_same_run(
    name: str, point: crash.CrashPoint, chunk: int
) -> None:
    subject = SUBJECTS[name]
    reference = crash.run_with_crash(subject, subject.smallest_world(), None, point)
    assert reference.crashed == 0
    for index in range(chunk, reference.commits, CHUNKS):
        survived = crash.run_with_crash(subject, subject.smallest_world(), index, point)
        assert survived.crashed == 1, f"commit {index} did not crash the host"
        assert survived.core.run.result == reference.core.run.result, f"commit {index}"
        assert survived.state == reference.state, f"commit {index}"
        executed = survived.effects.executed
        assert len(executed) == len(set(executed)), f"commit {index}: an effect ran twice"
