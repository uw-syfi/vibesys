"""C2: crash and replay at the durable boundaries of a run, for every registered strategy.

The host dies just before or just after the k-th commit; a new host with a new strategy
instance restarts over the store the dead one left, and the executors keep their world. The run
must reach the result an uninterrupted run reaches, end in an equal strategy state, and execute
no request identity twice. The boundaries are the commits of the subject's smallest full run.

The crash points tried are the suite's one setting (`--crash-points`): CI samples, the
nightly job tries every commit (the policy of #1491). Trying every commit of the largest subject
takes longer than one test may (`faulthandler_timeout` in `pyproject.toml`), so the exhaustive
setting splits each sweep into interleaved parts that the workers share.
"""

from __future__ import annotations

import pytest
from tests.composition._loop.dst_world import SubjectWorld
from tests.composition._loop.subjects import SUBJECTS

from vs_dst.api import EVERY_POINT, CrashKind, crash_point_policy, simulate

SUBJECT_NAMES = sorted(name for name, subject in SUBJECTS.items() if subject.sweeps_crashes)
PARTS = 4 if crash_point_policy() == EVERY_POINT else 1


@pytest.mark.parametrize("name", SUBJECT_NAMES)
@pytest.mark.parametrize("kind", list(CrashKind))
@pytest.mark.parametrize("part", range(PARTS))
def test_c2_a_restart_after_any_crash_finishes_the_same_run(
    name: str, kind: CrashKind, part: int
) -> None:
    subject = SUBJECTS[name]
    normal = simulate(SubjectWorld(subject, subject.smallest_world()))
    assert normal.finished

    for crash in normal.crash_points(kind)[part::PARTS]:
        recovered = normal.rerun_with_crash(crash)
        assert recovered.finished, crash
        assert recovered.result == normal.result, crash
        assert recovered.state == normal.state, crash
        assert recovered.no_effect_ran_twice(), crash
