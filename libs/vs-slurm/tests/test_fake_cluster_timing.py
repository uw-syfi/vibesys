"""The Fake cluster's job state is a function of its clock and measured timings."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Unpack

from hypothesis import given
from hypothesis import strategies as st

from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    ManualClock,
    SecondsRange,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmPhase,
    SlurmTimingProfile,
)

if TYPE_CHECKING:
    # test-isolation: necessary for type-safe script options
    from vs_slurm.fake_cluster import _ScriptOptions

_PROFILE = SlurmTimingProfile()


def _submit(cluster: FakeCluster, workspace: Path, **options: Unpack[_ScriptOptions]) -> None:
    cluster.script("job", **options)
    assert isinstance(
        cluster.submit(SlurmJobRequest(workspace=workspace, command=("true",)), operation_id="job"),
        ClusterSubmitted,
    )


def _read(cluster: FakeCluster) -> ClusterObservation:
    observed = cluster.inspect("job")
    assert isinstance(observed, ClusterObservation)
    return observed


def test_the_default_profile_matches_the_measured_cluster() -> None:
    """Defaults are production-like: 5.7 to 95 s queue wait, about 31 s in COMPLETING."""
    assert (_PROFILE.queue_wait_s.low, _PROFILE.queue_wait_s.high) == (5.7, 95.0)
    assert _PROFILE.completing_s.low >= 30.0
    assert _PROFILE.completing_s.high <= 40.0


@given(seed=st.integers(0, 50), dt=st.floats(0, 400, allow_nan=False))
def test_state_depends_only_on_the_clock(seed: int, dt: float) -> None:
    """Two inspections at the same time agree, and the state follows the drawn timeline."""
    clock = ManualClock()
    cluster = FakeCluster(clock=clock, timing=_PROFILE, seed=seed)
    with tempfile.TemporaryDirectory() as workspace:
        _submit(cluster, Path(workspace))
    clock.advance(dt)
    first, second = _read(cluster), _read(cluster)
    assert (first.status, first.phase, first.attempt) == (
        second.status,
        second.phase,
        second.attempt,
    )
    if dt < _PROFILE.queue_wait_s.low:
        assert first.phase is SlurmPhase.PENDING
    latest_end = _PROFILE.queue_wait_s.high + _PROFILE.run_s.high + _PROFILE.completing_s.high
    if dt >= latest_end:
        assert first.status is SlurmJobStatus.COMPLETED


@given(
    queue_wait=st.integers(0, 95),
    completing=st.integers(0, 40),
    after=st.integers(0, 200),
)
def test_a_cancelled_running_job_tears_down_for_the_completing_time(
    queue_wait: int, completing: int, after: int
) -> None:
    """scancel of a running job: COMPLETING (public RUNNING) until the lag, then CANCELLED."""
    clock = ManualClock()
    cluster = FakeCluster(clock=clock)
    with tempfile.TemporaryDirectory() as workspace:
        _submit(
            cluster,
            Path(workspace),
            queue_wait_s=queue_wait,
            run_s=math.inf,
            completing_s=completing,
        )
    clock.advance(queue_wait)
    cluster.cancel("job")
    clock.advance(after)
    observed = _read(cluster)
    if after < completing:
        assert (observed.status, observed.phase) == (
            SlurmJobStatus.RUNNING,
            SlurmPhase.COMPLETING,
        )
    else:
        assert observed.status is SlurmJobStatus.CANCELLED


def test_a_requeue_starts_a_new_attempt_that_waits_in_the_queue_again(tmp_path: Path) -> None:
    """RUNNING then PENDING happens only with a higher attempt number."""
    clock = ManualClock()
    cluster = FakeCluster(clock=clock, timing=SlurmTimingProfile.instant())
    _submit(cluster, tmp_path, queue_wait_s=5.0, run_s=10.0, completing_s=0.0, requeues=1)
    seen = []
    for _ in range(40):
        observed = _read(cluster)
        seen.append((observed.attempt, observed.phase))
        clock.advance(1.0)
    assert seen[0] == (0, SlurmPhase.PENDING)
    assert (0, SlurmPhase.RUNNING) in seen
    assert (1, SlurmPhase.PENDING) in seen
    assert seen == sorted(seen, key=lambda item: item[0])
    assert seen[-1][1] is SlurmPhase.ENDED


def test_a_range_can_be_pinned_to_one_value() -> None:
    """Tests pin a timing to a point to make an expectation exact."""
    pinned = SecondsRange.exactly(31.0)
    assert pinned.low == pinned.high == 31.0
