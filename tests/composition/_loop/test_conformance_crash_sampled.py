"""C2 for the subjects whose runs have too many commits for CI to crash at every one.

The host dies just before or just after the k-th commit of the subject's smallest full run, and a
new host restarts over the store, exactly as in `test_conformance_crash.py`. CI crashes at every
`ci_stride`-th boundary (the chunks interleave, so together they cover a different slice of the
run each); the slow sweep (`pytest -m slow`) crashes at every boundary.
"""

from __future__ import annotations

import pytest
from tests.composition._loop import _crash as crash
from tests.composition._loop.subjects import SUBJECTS

CHUNKS = 6
SAMPLED = sorted(
    name
    for name, subject in SUBJECTS.items()
    if not subject.sweeps_crashes and subject.ci_stride > 1
)


def _sweep(name: str, point: crash.CrashPoint, chunk: int, *, stride: int) -> None:
    subject = SUBJECTS[name]
    reference = crash.run_with_crash(subject, subject.smallest_world(), None, point)
    assert reference.crashed == 0
    for index in range(chunk, reference.commits, CHUNKS * stride):
        survived = crash.run_with_crash(subject, subject.smallest_world(), index, point)
        assert survived.crashed == 1, f"commit {index} did not crash the host"
        assert survived.core.run.result == reference.core.run.result, f"commit {index}"
        assert survived.state == reference.state, f"commit {index}"
        executed = survived.effects.executed
        assert len(executed) == len(set(executed)), f"commit {index}: an effect ran twice"


@pytest.mark.parametrize("name", SAMPLED)
@pytest.mark.parametrize("point", list(crash.CrashPoint))
@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_c2_a_crash_at_a_sample_of_the_commits_replays_to_the_same_run(
    name: str, point: crash.CrashPoint, chunk: int
) -> None:
    _sweep(name, point, chunk, stride=SUBJECTS[name].ci_stride)


@pytest.mark.slow
@pytest.mark.parametrize("name", SAMPLED)
@pytest.mark.parametrize("point", list(crash.CrashPoint))
@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_c2_a_crash_at_every_commit_of_a_long_run_replays_to_the_same_run(
    name: str, point: crash.CrashPoint, chunk: int
) -> None:
    _sweep(name, point, chunk, stride=1)
