"""A simulated ``Platform`` keeps every interface on the simulation's own timeline."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_sim.api import DISCARD, ProcessOutcome, ProcessSpec, utc_datetime
from vs_sim.api.testing import (
    SIM_MONOTONIC_ORIGIN,
    SIM_WALL_EPOCH,
    FakeProcesses,
    FakeProcessSignaller,
    ProcessScript,
    Sim,
)


@given(
    seconds=st.floats(min_value=0, max_value=3600),
    duration=st.floats(min_value=0, max_value=3600),
    seed=st.integers(0, 2**16),
)
def test_blocking_work_on_a_simulated_platform_sleeps_in_virtual_time(
    seconds: float, duration: float, seed: int
) -> None:
    sim = Sim(seed=seed)
    threads = sim.threads()
    processes = FakeProcesses(
        lambda _spec: ProcessScript(returncode=3, duration_s=duration), pass_time=threads.sleep
    )
    platform = sim.platform(threads, processes=processes, signaller=FakeProcessSignaller(set()))
    start = platform.clock.now()

    def work() -> ProcessOutcome:
        platform.threads.sleep(seconds)
        run = platform.processes.run(ProcessSpec(("true",)), timeout=None, output=DISCARD)
        assert isinstance(run, ProcessOutcome)
        return run

    async def main() -> ProcessOutcome:
        return await platform.blocking.run(work)

    assert sim.run(main(), threads=threads) == ProcessOutcome(3, b"", b"")
    assert platform.clock.now() == pytest.approx(start + seconds + duration)


@given(seed=st.integers(0, 2**16), seconds=st.floats(min_value=0, max_value=86_400))
@example(seed=0, seconds=1e-6)
def test_a_simulated_platform_reads_wall_time_far_from_monotonic_time(
    seed: int, seconds: float
) -> None:
    """Wall and monotonic readings tick together but never agree, so mixing them shows."""
    sim = Sim(seed=seed)
    threads = sim.threads()
    platform = sim.platform(
        threads,
        processes=FakeProcesses(lambda _spec: ProcessScript(), pass_time=threads.sleep),
        signaller=FakeProcessSignaller(set()),
    )
    wall, monotonic, timeline = platform.clock.now(), threads.now(), sim.clock.now()

    async def main() -> None:
        await platform.clock.sleep(seconds)

    sim.run(main(), threads=threads)
    assert wall - monotonic == pytest.approx(SIM_WALL_EPOCH - SIM_MONOTONIC_ORIGIN)
    assert monotonic - timeline == pytest.approx(SIM_MONOTONIC_ORIGIN)
    # A reading near the epoch keeps about 0.25 microseconds of float precision, as the
    # real wall clock does, so elapsed time compares to within a microsecond.
    assert platform.clock.now() - wall == pytest.approx(seconds, abs=1e-6)
    assert threads.now() - monotonic == pytest.approx(seconds, abs=1e-6)
    assert utc_datetime(platform.clock).year >= 2023
