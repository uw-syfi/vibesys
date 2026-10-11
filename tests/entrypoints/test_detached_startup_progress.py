"""A detached server's record shows its run's startup stage and progress, written at a bounded rate."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from hypothesis import example, given, settings
from hypothesis import strategies as st

from entrypoints.detached_run import PROGRESS_WRITE_INTERVAL_SECONDS, RecordPublisher
from server.host_api import (
    FailureReason,
    InstanceStatus,
    LiveInstanceRecord,
    RunPhase,
    RunStatus,
    StartupFact,
    StartupProgress,
    StartupStage,
)
from server.registry import FakeInstanceMetadataStore, FakeInstanceStore, InstanceHold, LiveRegistry
from vs_sim.api.testing import ManualClock, SimThreads

INSTANCE = "0123456789ab"
_STARTING = RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING)


def _starting_record() -> LiveInstanceRecord:
    return LiveInstanceRecord(
        id=INSTANCE,
        status=InstanceStatus.SERVING,
        phase=_STARTING,
        socket_path="/run/s.sock",
        log_path="/run/server.log",
        project_root="/project",
        run_id="run-1",
        pid=4242,
        started_at=1.0,
        hostname="node",
        vibesys_version="0+test",
    )


@dataclass
class _TimedHold:
    """An ``InstanceHold`` that also notes when each record was written, on ``threads``' clock."""

    hold: InstanceHold
    threads: SimThreads
    writes: list[tuple[float, RunPhase]] = field(default_factory=list)

    def publish(self, record: LiveInstanceRecord) -> None:
        self.writes.append((self.threads.now(), record.phase))
        self.hold.publish(record)

    def release(self) -> None:
        self.hold.release()


def _building(done: int, total: int = 14) -> StartupFact:
    return StartupFact(
        stage=StartupStage.BUILDING_IMAGE,
        progress=StartupProgress(done=done, total=total, label=f"step {done}"),
    )


def test_a_starting_run_shows_each_stage_and_its_latest_progress() -> None:
    threads = SimThreads()
    registry = LiveRegistry(FakeInstanceStore(), ManualClock(100.0))

    def scenario() -> list[tuple[float, RunPhase]]:
        with registry.register(INSTANCE) as hold:
            timed = _TimedHold(hold, threads)
            publisher = RecordPublisher(
                timed,
                _starting_record(),
                ManualClock(100.0),
                metadata=FakeInstanceMetadataStore(),
                threads=threads,
            )
            threads.sleep(5)
            publisher.observe(startup=StartupFact(stage=StartupStage.BUILDING_IMAGE))
            for done in range(5):
                publisher.observe(startup=_building(done))
            threads.sleep(2)
            publisher.observe(startup=StartupFact(stage=StartupStage.STARTING_CONTAINER))
            publisher.observe(status=RunStatus.RUNNING)
            return timed.writes

    start = threads.now()
    writes = [(at - start, phase) for at, phase in threads.run(scenario)]

    def starting(fact: StartupFact) -> RunPhase:
        return RunPhase(status=RunStatus.STARTING, stage=fact.stage, progress=fact.progress)

    assert writes == [
        (0.0, _STARTING),
        (5.0, starting(StartupFact(stage=StartupStage.BUILDING_IMAGE))),
        # The steps that arrived within a second of that write land together, once.
        (5.0 + PROGRESS_WRITE_INTERVAL_SECONDS, starting(_building(4))),
        (7.0, starting(StartupFact(stage=StartupStage.STARTING_CONTAINER))),
        (7.0, RunPhase(status=RunStatus.RUNNING)),
    ]


def test_a_build_failure_leaves_a_tombstone_failed_in_building_image() -> None:
    threads = SimThreads()
    clock = ManualClock(100.0)
    registry = LiveRegistry(FakeInstanceStore(), clock)
    reason = FailureReason(
        code="run_failed",
        stage="run",
        message="Could not build agent image (exit 1): npm ERR! network",
    )

    def scenario() -> None:
        with registry.register(INSTANCE) as hold:
            publisher = RecordPublisher(
                hold,
                _starting_record(),
                clock,
                metadata=FakeInstanceMetadataStore(),
                threads=threads,
            )
            publisher.observe(startup=_building(3))
            publisher.observe(status=RunStatus.FAILED)
            publisher.observe(failure=reason)
            publisher.end()

    threads.run(scenario)

    (ended,) = registry.list().ended
    assert ended.phase == RunPhase(
        status=RunStatus.FAILED,
        reason=FailureReason(code="run_failed", stage="building_image", message=reason.message),
    )


_CLOCK_ROUNDING_SECONDS = 1e-9

_FACTS = st.one_of(
    st.builds(_building, st.integers(0, 13)),
    st.builds(StartupFact, stage=st.sampled_from(StartupStage)),
    st.sampled_from([RunStatus.RUNNING, RunStatus.STOPPING]),
)


@settings(max_examples=40, deadline=None)
# A progress write that lands as the flush worker wakes restarts the interval;
# the worker must not then write the next held phase at the same instant.
@example(steps=[(0.0, _building(0)), (0.0, _building(1)), (1.0, _building(0)), (0.0, _building(1))])
@example(steps=[(0.1, _building(1)), (0.4, _building(0)), (0.9, _building(1)), (0.9, _building(0))])
@given(steps=st.lists(st.tuples(st.sampled_from([0.0, 0.1, 0.4, 0.9, 1.0, 3.0]), _FACTS)))
def test_progress_writes_are_paced_and_the_record_ends_on_the_folded_phase(
    steps: list[tuple[float, StartupFact | RunStatus]],
) -> None:
    threads = SimThreads()
    registry = LiveRegistry(FakeInstanceStore(), ManualClock(100.0))

    def scenario() -> list[tuple[float, RunPhase]]:
        with registry.register(INSTANCE) as hold:
            timed = _TimedHold(hold, threads)
            publisher = RecordPublisher(
                timed,
                _starting_record(),
                ManualClock(100.0),
                metadata=FakeInstanceMetadataStore(),
                threads=threads,
            )
            for delay, fact in steps:
                threads.sleep(delay)
                if isinstance(fact, StartupFact):
                    publisher.observe(startup=fact)
                else:
                    publisher.observe(status=fact)
            threads.sleep(2 * PROGRESS_WRITE_INTERVAL_SECONDS)
            return timed.writes

    writes = threads.run(scenario)

    folded = _STARTING
    for _delay, fact in steps:
        folded = (
            folded.advance(startup=fact)
            if isinstance(fact, StartupFact)
            else folded.advance(status=fact)
        )
    assert writes[-1][1] == folded
    for (before_at, before), (at, phase) in pairwise(writes):
        if (phase.status, phase.stage) == (before.status, before.stage):
            # Times are sums of floats (4.1 - 3.1 is 0.9999999999999996), so allow rounding error.
            assert at - before_at >= PROGRESS_WRITE_INTERVAL_SECONDS - _CLOCK_ROUNDING_SECONDS
