"""C2: crash and replay at the durable boundaries of a run, for every registered strategy.

The host dies just before or just after the k-th commit; a new host with a new strategy
instance restarts over the store the dead one left, and the executors keep their world. The run
must reach the result an uninterrupted run reaches, end in an equal strategy state, and execute
no request identity twice. The boundaries are the commits of the subject's smallest full run.

Each crash re-runs the whole run, so crashing at every commit costs a full run per commit. CI
crashes at `CI_BOUNDARIES` evenly spaced commits, always including the first and the last (the
policy of #1491: CI samples, the slow tier is exhaustive); `pytest -m slow` crashes at every one,
split into interleaved chunks so the workers share it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.composition._loop import _crash as crash
from tests.composition._loop.subjects import SUBJECTS

from vs_sim.api.testing import evenly_spaced

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

CI_BOUNDARIES = 12
CHUNKS = 6
SUBJECT_NAMES = sorted(name for name, subject in SUBJECTS.items() if subject.sweeps_crashes)


def _sweep(name: str, point: crash.CrashPoint, choose: Callable[[int], Iterable[int]]) -> None:
    """Crash ``name``'s smallest run at the commits ``choose`` picks from its count, and compare."""
    subject = SUBJECTS[name]
    reference = crash.run_with_crash(subject, subject.smallest_world(), None, point)
    assert reference.crashed == 0
    for index in choose(reference.commits):
        survived = crash.run_with_crash(subject, subject.smallest_world(), index, point)
        assert survived.crashed == 1, f"commit {index} did not crash the host"
        assert survived.core.run.result == reference.core.run.result, f"commit {index}"
        assert survived.state == reference.state, f"commit {index}"
        executed = survived.effects.executed
        assert len(executed) == len(set(executed)), f"commit {index}: an effect ran twice"


@pytest.mark.parametrize("name", SUBJECT_NAMES)
@pytest.mark.parametrize("point", list(crash.CrashPoint))
def test_c2_a_crash_at_a_sample_of_the_commits_replays_to_the_same_run(
    name: str, point: crash.CrashPoint
) -> None:
    _sweep(name, point, lambda commits: evenly_spaced(commits, CI_BOUNDARIES))


@pytest.mark.slow
@pytest.mark.parametrize("name", SUBJECT_NAMES)
@pytest.mark.parametrize("point", list(crash.CrashPoint))
@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_c2_a_crash_at_every_commit_of_a_run_replays_to_the_same_run(
    name: str, point: crash.CrashPoint, chunk: int
) -> None:
    _sweep(name, point, lambda commits: range(chunk, commits, CHUNKS))
